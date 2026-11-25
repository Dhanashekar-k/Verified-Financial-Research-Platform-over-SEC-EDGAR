"""Plan compiler: QueryPlan -> typed, parameterized SQL over `facts` -> NumericResults.

Shekar-owned module. Implemented by Claude at Shekar's request (2026-11-25, ADR-047) against
`tests/integration/test_compiler_pg.py`; Shekar reviews it with /interview-me. The contract is
05_data_contracts.md section 4.5 and ADR-046.

The LLM never writes SQL and never does arithmetic. SQL here is fixed text with bound values;
every number is Decimal and carries an ``expr`` over fact ids that re-evaluates to it. A number
the filings do not support becomes a ``NotFound``, never an estimate.

Reading order: ``compile_plan`` (operations) -> ``_Run.evaluate`` (one metric for one company and
period) -> ``_from_tags`` (ADR-043 resolve-or-abstain) / ``_from_formula``.
"""

from __future__ import annotations

import ast
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Context, Decimal, localcontext
from typing import Any, Literal

import psycopg

from app.engine.plan.catalog import MetricDef, load_catalog, verified_metrics_in
from app.engine.plan.schema import (
    CompileOutcome,
    FiscalPeriodName,
    Note,
    NotFound,
    NotFoundReason,
    NumericResult,
    PeriodRef,
    QueryPlan,
    Unit,
    UnsupportedPlan,
)

RANK_LIMIT = 10
TAXONOMY = "us-gaap"  # a custom tag that shares a name is a different concept (ADR-044)

# The gross-profit identity, revenue - cost of revenue = gross profit (ADR-043).
GROSS_PROFIT = "gross_profit"
IDENTITY: dict[str, tuple[str, Literal["revenue", "cost"]]] = {
    "revenue": ("cost_of_revenue", "revenue"),
    "cost_of_revenue": ("revenue", "cost"),
}

# A total under its own component is inconsistent, so no later rule may pick from the pair
# (ADR-043; the gold resolver's TOTAL_OVER_COMPONENT). metric -> (total tag, component tag).
TOTAL_OVER_COMPONENT: dict[str, tuple[str, str]] = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
}
# A full-year amount counts only from an annual report: a 10-Q's trailing-twelve-month figure
# is also labelled FY with qtrs = 4 in the lake.
ANNUAL_FORM_PREFIX = "10-K"

# "The same number" for two candidate tags: the gold generator's tolerances (ADR-044). Kept here
# so app/ does not import eval/; a property test holds the two functions equal.
SIG_FIG_REL = Decimal("0.0005")
AMOUNT_ABS = Decimal("0.5")
PER_SHARE_ABS = Decimal("0.005")
RATIO_ABS = Decimal("0.0005")
_TOLERANCES: dict[str, tuple[Decimal, Decimal]] = {  # unit -> (relative, absolute)
    "USD": (SIG_FIG_REL, AMOUNT_ABS),
    "shares": (SIG_FIG_REL, AMOUNT_ABS),
    "USD/shares": (Decimal(0), PER_SHARE_ABS),
    "ratio": (Decimal(0), RATIO_ABS),
}


def agree(a: Decimal, b: Decimal, unit: str) -> bool:
    """True when either value is within the other's tolerance."""
    rel, floor = _TOLERANCES[unit]
    return any(abs(x - y) <= max(floor, rel * abs(y)) for x, y in ((a, b), (b, a)))


# --- rows and intermediate values ---------------------------------------------------------------

_SELECT_FACTS = """
SELECT fact_id, fact_key, cik, tag, uom, qtrs, period_end, value, form, is_restated,
       derived_from
FROM facts
WHERE cik = ANY(%s) AND tag = ANY(%s) AND fiscal_year = %s AND fiscal_period = %s
  AND taxonomy = %s AND is_original
ORDER BY cik, tag, fact_id
"""
_SELECT_LATEST = """
SELECT fact_id, value
FROM facts
WHERE fact_key = %s AND cik = %s AND is_latest AND fact_id <> %s
ORDER BY fact_id
"""


@dataclass(frozen=True)
class _Row:
    fact_id: int
    fact_key: int
    cik: int
    tag: str
    uom: str
    qtrs: int
    period_end: object  # a date; only ever compared for equality
    value: Decimal
    form: str
    is_restated: bool
    derived: bool  # a derived Q4 (FY - 9M)
    metric: str = ""  # the catalog metric this fact stands for, set when it is picked


@dataclass(frozen=True)
class _Value:
    metric: str
    value: Decimal
    expr: str  # 05 4.3 grammar
    rows: tuple[_Row, ...]  # the leaf facts, in the order the expression names them
    derived: bool  # arithmetic, or a fallback formula
    resolution: Literal["single_candidate", "candidates_agree", "reconciles_with_gross_profit"] | (
        None
    ) = None
    fallbacks: tuple[str, ...] = ()  # metrics on the way here that used their fallback formula


@dataclass(frozen=True)
class _Missing:
    reason: NotFoundReason
    detail: str = ""
    missing_inputs: tuple[str, ...] = ()


_Outcome = _Value | _Missing


def _wrap(expr: str) -> str:
    """Parenthesize anything that is not a single ``f:<id>`` token or number."""
    return f"({expr})" if " " in expr else expr


def _fact_ids(rows: Sequence[_Row]) -> list[int]:
    return list(dict.fromkeys(r.fact_id for r in rows))


def _period_label(period: PeriodRef) -> str:
    if period.fiscal_period == "FY":
        return f"FY{period.fiscal_year}"
    return f"{period.fiscal_period} FY{period.fiscal_year}"


class _Run:
    """One compilation: the catalog, the connection and the rows read so far."""

    def __init__(
        self,
        catalog: Mapping[str, MetricDef],
        conn: psycopg.Connection[Any],
        tags: set[str],
        verified: Collection[str] | None = None,
    ) -> None:
        self.catalog = catalog
        self._verified = None if verified is None else frozenset(verified)
        self.conn = conn
        self.tags = sorted(tags)  # every tag any requested metric can read
        # (cik, fiscal_year, fiscal_period) -> tag -> its is_original rows
        self.cache: dict[tuple[int, int, str], dict[str, list[_Row]]] = {}
        self.values: dict[tuple[str, int, int, str], _Outcome] = {}
        self.latest: dict[int, tuple[int, Decimal] | None] = {}

    def verified(self) -> frozenset[str]:
        """The verified list, read only when a fallback needs it."""
        if self._verified is None:
            self._verified = verified_metrics_in(self.catalog)
        return self._verified

    # --- reading --------------------------------------------------------------------------

    def load(self, ciks: Collection[int], period: PeriodRef) -> None:
        """Read one period for many companies in one query (a rank covers hundreds)."""
        key = (period.fiscal_year, period.fiscal_period)
        wanted = sorted({c for c in ciks if (c, *key) not in self.cache})
        if not wanted:
            return
        for cik in wanted:
            self.cache[(cik, *key)] = {}
        params = (wanted, self.tags, period.fiscal_year, period.fiscal_period, TAXONOMY)
        for rec in self.conn.execute(_SELECT_FACTS, params).fetchall():
            fact_id, fact_key, cik, tag, uom, qtrs, period_end, value, *rest = rec
            form, restated, derived = rest
            row = _Row(
                fact_id=fact_id,
                fact_key=fact_key,
                cik=cik,
                tag=tag,
                uom=uom,
                qtrs=qtrs,
                period_end=period_end,
                value=value,
                form=form,
                is_restated=restated,
                derived=bool(derived),
            )
            self.cache[(cik, *key)].setdefault(tag, []).append(row)

    def rows(self, metric: MetricDef, tag: str, cik: int, period: PeriodRef) -> list[_Row]:
        """The facts of one tag that fit the metric: unit, period length and the per-share rule."""
        self.load([cik], period)
        if metric.period_type == "instant":
            qtrs = 0
        else:
            qtrs = 4 if period.fiscal_period == "FY" else 1
        per_share = metric.unit == "USD/shares"
        # FSDS records per-share amounts with uom 'USD' (ADR-044).
        uoms = {"USD/shares", "USD"} if per_share else {metric.unit}
        found = self.cache[(cik, period.fiscal_year, period.fiscal_period)].get(tag, [])
        return [
            r
            for r in found
            if r.qtrs == qtrs
            and r.uom in uoms
            and (qtrs != 4 or r.form.startswith(ANNUAL_FORM_PREFIX))
            # FY - 9M is not valid for a per-share amount (05 4.5)
            and not (per_share and r.derived)
        ]

    def latest_version(self, row: _Row) -> tuple[int, Decimal] | None:
        """The restated latest version of an original fact, when its value differs."""
        if row.fact_id not in self.latest:
            found = self.conn.execute(
                _SELECT_LATEST, (row.fact_key, row.cik, row.fact_id)
            ).fetchall()
            differs = len(found) == 1 and found[0][1] != row.value
            self.latest[row.fact_id] = (found[0][0], found[0][1]) if differs else None
        return self.latest[row.fact_id]

    # --- one metric, one company, one period ------------------------------------------------

    def evaluate(
        self, name: str, cik: int, period: PeriodRef, visiting: frozenset[str] = frozenset()
    ) -> _Outcome:
        key = (name, cik, period.fiscal_year, period.fiscal_period)
        if key not in self.values:
            if name in visiting:
                raise UnsupportedPlan(f"the catalog formula for {name!r} refers back to itself")
            metric = self.catalog[name]
            inside = visiting | {name}
            if metric.formula is not None:
                self.values[key] = self._from_formula(name, metric.formula, cik, period, inside)
            else:
                self.values[key] = self._from_tags(name, metric, cik, period, inside)
        return self.values[key]

    def _candidates(self, metric: MetricDef, cik: int, period: PeriodRef) -> list[_Row] | _Missing:
        """At most one fact per tag, in chain order, all ending on the same date."""
        out: list[_Row] = []
        for tag in metric.tags or []:
            found = self.rows(metric, tag, cik, period)
            if len(found) > 1:
                return _Missing(
                    "conflicting_candidates",
                    f"{tag} has {len(found)} facts for this period (different period ends)",
                )
            out.extend(found)
        if len({r.period_end for r in out}) > 1:
            return _Missing("conflicting_candidates", "the candidate tags end on different dates")
        return out

    def _from_tags(
        self,
        name: str,
        metric: MetricDef,
        cik: int,
        period: PeriodRef,
        visiting: frozenset[str],
    ) -> _Outcome:
        """ADR-043 as ADR-046 narrows it: one tag, or tags that agree, or the one candidate that
        closes the gross-profit identity; otherwise abstain. Never "first tag wins"."""
        found = self._candidates(metric, cik, period)
        if isinstance(found, _Missing):
            return found
        if not found:
            if metric.fallback_formula is None:
                return _Missing("no_fact", f"no {' or '.join(metric.tags or [])} fact")
            return self._from_fallback(name, metric, cik, period, visiting)

        groups: list[_Row] = []  # one representative per distinct number, first in chain order
        for row in found:
            if not any(agree(row.value, g.value, metric.unit) for g in groups):
                groups.append(row)
        if len(groups) == 1:
            how: Literal["single_candidate", "candidates_agree"] = (
                "single_candidate" if len(found) == 1 else "candidates_agree"
            )
            return self._picked(name, groups[0], how)
        shown = ", ".join(f"{r.tag}={r.value:f}" for r in found)
        pair = TOTAL_OVER_COMPONENT.get(name)
        by_tag = {r.tag: r.value for r in found}
        if pair is not None and set(by_tag) == set(pair) and by_tag[pair[0]] < by_tag[pair[1]]:
            return _Missing("conflicting_candidates", f"{pair[0]} is below {pair[1]}: {shown}")
        closing = [g for g in groups if self._closes_identity(name, g, cik, period)]
        if len(closing) == 1:
            return self._picked(name, closing[0], "reconciles_with_gross_profit")
        return _Missing("conflicting_candidates", f"the candidate tags disagree: {shown}")

    @staticmethod
    def _picked(
        name: str,
        row: _Row,
        how: Literal["single_candidate", "candidates_agree", "reconciles_with_gross_profit"],
    ) -> _Value:
        row = replace(row, metric=name)
        return _Value(name, row.value, f"f:{row.fact_id}", (row,), False, how)

    def _closes_identity(self, name: str, row: _Row, cik: int, period: PeriodRef) -> bool:
        """True when ``row`` satisfies revenue - cost = reported gross profit for some reported
        value of the other side. Uses reported facts only, never the fallback."""
        if name not in IDENTITY or GROSS_PROFIT not in self.catalog:
            return False
        other_name, role = IDENTITY[name]
        other = self.catalog.get(other_name)
        gp_metric = self.catalog[GROSS_PROFIT]
        if other is None or other.tags is None or gp_metric.tags is None:
            return False

        def same_period(metric: MetricDef) -> list[_Row]:
            return [
                r
                for tag in metric.tags or []
                for r in self.rows(metric, tag, cik, period)
                if r.period_end == row.period_end
            ]

        gross = same_period(gp_metric)
        if len(gross) != 1:
            return False
        for counterpart in same_period(other):
            if role == "revenue":
                implied = row.value - counterpart.value
            else:
                implied = counterpart.value - row.value
            if agree(implied, gross[0].value, "USD"):
                return True
        return False

    def _from_fallback(
        self,
        name: str,
        metric: MetricDef,
        cik: int,
        period: PeriodRef,
        visiting: frozenset[str],
    ) -> _Outcome:
        """The fallback formula, only when every ``only_if`` condition can be shown to hold."""
        assert metric.fallback_formula is not None
        if metric.only_if is None:
            return _Missing("fallback_not_allowed", "the fallback formula declares no conditions")
        out = self._from_formula(name, metric.fallback_formula, cik, period, visiting)
        if isinstance(out, _Missing):
            return out
        refs = _formula_names(metric.fallback_formula)
        inputs = [self.evaluate(ref, cik, period, visiting) for ref in refs]
        holds = {
            "same_entity": len({r.cik for r in out.rows}) == 1,
            "same_period": len({r.period_end for r in out.rows}) == 1
            and len({r.qtrs for r in out.rows}) == 1,
            "same_currency": {r.uom for r in out.rows} == {metric.unit},
            # the consolidated `facts` view only; every input is a filed fact, not a computation
            "same_scope": all(isinstance(v, _Value) and not v.derived for v in inputs),
            # on the verified list (the gold generator's), and out of the tag rule above
            "inputs_verified": all(ref in self.verified() for ref in refs)
            and all(isinstance(v, _Value) and v.resolution for v in inputs),
        }
        broken = [c for c in metric.only_if if not holds[c]]
        if broken:
            return _Missing("fallback_not_allowed", f"conditions not met: {', '.join(broken)}")
        return _Value(name, out.value, out.expr, out.rows, True, None, (*out.fallbacks, name))

    def _from_formula(
        self, name: str, formula: str, cik: int, period: PeriodRef, visiting: frozenset[str]
    ) -> _Outcome:
        inputs: dict[str, _Value] = {}
        missing: list[str] = []
        why: list[str] = []
        for ref in _formula_names(formula):
            got = self.evaluate(ref, cik, period, visiting)
            if isinstance(got, _Value):
                inputs[ref] = got
            else:
                missing.append(ref)
                why.append(f"{ref}: {got.reason}")
        if missing:
            return _Missing("missing_input", "; ".join(why), tuple(missing))
        if len({r.period_end for v in inputs.values() for r in v.rows}) > 1:
            return _Missing("conflicting_candidates", "the inputs end on different dates")

        rows: list[_Row] = []
        fallbacks: list[str] = []

        def walk(node: ast.expr) -> tuple[Decimal, str] | None:
            """(value, expr text), or None on a zero divisor. The expression is built beside the
            arithmetic, in the same order, so re-evaluating it gives exactly this value."""
            if isinstance(node, ast.Name):
                operand = inputs[node.id]
                rows.extend(operand.rows)
                fallbacks.extend(operand.fallbacks)
                return operand.value, operand.expr
            if isinstance(node, ast.Constant):
                return Decimal(str(node.value)), str(node.value)
            if isinstance(node, ast.UnaryOp):
                inner = walk(node.operand)
                return None if inner is None else (-inner[0], f"-{_wrap(inner[1])}")
            assert isinstance(node, ast.BinOp)  # formula_refs allows nothing else
            left, right = walk(node.left), walk(node.right)
            if left is None or right is None:
                return None
            a, b = left[0], right[0]
            if isinstance(node.op, ast.Add):
                result, symbol = a + b, "+"
            elif isinstance(node.op, ast.Sub):
                result, symbol = a - b, "-"
            elif isinstance(node.op, ast.Mult):
                result, symbol = a * b, "*"
            else:
                if b == 0:
                    return None
                result, symbol = a / b, "/"
            return result, f"{_wrap(left[1])} {symbol} {_wrap(right[1])}"

        computed = walk(ast.parse(formula, mode="eval").body)
        if computed is None:
            return _Missing("division_by_zero", f"a divisor in {formula!r} is zero")
        return _Value(
            name, computed[0], computed[1], tuple(rows), True, None, tuple(dict.fromkeys(fallbacks))
        )

    # --- results ---------------------------------------------------------------------------

    def notes(
        self, parts: Sequence[tuple[_Value, PeriodRef]], *, two_companies: bool = False
    ) -> list[Note]:
        """What the composer must say about the facts a result cites. Each note names the metric
        and period it is about: on a ratio or a growth rate that is an input, not the result."""
        notes: list[Note] = []
        whose = (
            " This applies to at least one of the two companies; see each company's own result."
            if two_companies
            else ""
        )
        for name in dict.fromkeys(n for value, _ in parts for n in value.fallbacks):
            formula = self.catalog[name].fallback_formula
            when = _labels(period for value, period in parts if name in value.fallbacks)
            notes.append(
                Note(
                    code="derived_fallback",
                    text=(
                        f"{self._label(name)} for {when} was not reported; it is computed as "
                        f"{formula}.{whose}"
                    ),
                )
            )
        derived_in = [(r.metric, p) for value, p in parts for r in value.rows if r.derived]
        if derived_in:
            named = " and ".join(dict.fromkeys(self._label(m) for m, _ in derived_in))
            notes.append(
                Note(
                    code="q4_derived",
                    text=(
                        f"{named} for {_labels(p for _, p in derived_in)}: no fourth quarter "
                        f"was reported; it is the full year minus the first nine months.{whose}"
                    ),
                )
            )
        seen: set[int] = set()
        for value, period in parts:
            for row in value.rows:
                if not row.is_restated or row.fact_id in seen:
                    continue
                seen.add(row.fact_id)
                latest = self.latest_version(row)
                if latest is None:
                    continue
                notes.append(
                    Note(
                        code="restated",
                        text=(
                            f"{self._label(row.metric)} for {_period_label(period)} was later "
                            f"restated; the figure used is as originally filed.{whose}"
                        ),
                        latest_fact_id=latest[0],
                        latest_value=latest[1],
                    )
                )
        return notes

    def _label(self, name: str) -> str:
        return self.catalog[name].label or name.replace("_", " ")

    def value_result(
        self, value: _Value, cik: int, period: PeriodRef, rank: int | None = None
    ) -> NumericResult:
        unit: Unit = self.catalog[value.metric].unit
        return NumericResult(
            metric=value.metric,
            cik=cik,
            fiscal_year=period.fiscal_year,
            fiscal_period=period.fiscal_period,
            operation="value",
            value=value.value,
            unit=unit,
            expr=value.expr,
            fact_ids=_fact_ids(value.rows),
            kind="derived" if value.derived else "reported",
            resolution=value.resolution,
            rank=rank,
            notes=self.notes([(value, period)]),
        )

    def difference_result(
        self, metric: str, first: tuple[int, _Value], second: tuple[int, _Value], period: PeriodRef
    ) -> NumericResult:
        (cik_a, a), (cik_b, b) = first, second
        return NumericResult(
            metric=metric,
            cik=cik_a,
            compare_cik=cik_b,
            fiscal_year=period.fiscal_year,
            fiscal_period=period.fiscal_period,
            operation="difference",
            value=a.value - b.value,
            unit=self.catalog[metric].unit,
            expr=f"{_wrap(a.expr)} - {_wrap(b.expr)}",
            fact_ids=_fact_ids((*a.rows, *b.rows)),
            kind="derived",
            notes=self.notes([(a, period), (b, period)], two_companies=True),
        )

    def growth(
        self, metric: str, cik: int, current: PeriodRef, base: PeriodRef
    ) -> NumericResult | NotFound:
        now = self.evaluate(metric, cik, current)
        if isinstance(now, _Missing):
            return self.not_found(metric, cik, current, now)
        before = self.evaluate(metric, cik, base)
        if isinstance(before, _Missing):
            return self.not_found(
                metric,
                cik,
                current,
                _Missing(
                    "growth_base_missing",
                    f"no usable {_period_label(base)} value ({before.reason})",
                ),
            )
        why_not = self._incomparable(cik, now, before, (current, base))
        if why_not is not None:
            return self.not_found(metric, cik, current, _Missing("conflicting_candidates", why_not))
        if before.value <= 0:
            return self.not_found(
                metric,
                cik,
                current,
                _Missing(
                    "growth_base_nonpositive",
                    f"the {_period_label(base)} value is zero or negative",
                ),
            )
        base_expr = _wrap(before.expr)
        return NumericResult(
            metric=metric,
            cik=cik,
            fiscal_year=current.fiscal_year,
            fiscal_period=current.fiscal_period,
            operation="growth",
            value=(now.value - before.value) / before.value,
            unit="ratio",
            expr=f"({_wrap(now.expr)} - {base_expr}) / {base_expr}",
            fact_ids=_fact_ids((*now.rows, *before.rows)),
            kind="derived",
            notes=self.notes([(now, current), (before, base)]),
        )

    def _incomparable(
        self, cik: int, now: _Value, before: _Value, periods: tuple[PeriodRef, PeriodRef]
    ) -> str | None:
        """Why the two sides of a growth rate are not one quantity, or None when they are. Both
        must be obtained the same way (reported, or the same computation), and each input must use
        one tag, or two tags that reported the same number in at least one of the two periods."""
        mine = {r.metric: r.tag for r in now.rows}
        theirs = {r.metric: r.tag for r in before.rows}
        if set(mine) != set(theirs):
            return "one period's figure is reported and the other is computed"
        for name, first in mine.items():
            second = theirs[name]
            if first == second:
                continue
            metric = self.catalog[name]
            for period in periods:
                a = self.rows(metric, first, cik, period)
                b = self.rows(metric, second, cik, period)
                if len(a) == 1 and len(b) == 1 and agree(a[0].value, b[0].value, metric.unit):
                    break
            else:
                return (
                    f"the two periods resolve to different tags ({first}, {second}) that agree "
                    "in neither period"
                )
        return None

    def not_found(self, metric: str, cik: int, period: PeriodRef, why: _Missing) -> NotFound:
        return NotFound(
            metric=metric,
            cik=cik,
            fiscal_year=period.fiscal_year,
            fiscal_period=period.fiscal_period,
            reason=why.reason,
            detail=why.detail,
            missing_inputs=list(why.missing_inputs),
        )


def _labels(periods: Iterable[PeriodRef]) -> str:
    return " and ".join(dict.fromkeys(_period_label(p) for p in periods))


def _formula_names(formula: str) -> list[str]:
    """The catalog keys a formula names, in reading order (the catalog validates the grammar)."""
    names: dict[str, None] = {}

    class _Names(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            names[node.id] = None

    _Names().visit(ast.parse(formula, mode="eval"))
    return list(names)


def _tags_reachable(catalog: Mapping[str, MetricDef], metrics: Sequence[str]) -> set[str]:
    """Every tag the requested metrics can read: their own, their formulas' and the identity's."""
    todo, seen, tags = list(metrics), set[str](), set[str]()
    while todo:
        name = todo.pop()
        if name in seen or name not in catalog:
            continue
        seen.add(name)
        metric = catalog[name]
        tags.update(metric.tags or [])
        for formula in (metric.formula, metric.fallback_formula):
            if formula is not None:
                todo.extend(_formula_names(formula))
        if name in IDENTITY and metric.tags and len(metric.tags) > 1:
            todo.extend((IDENTITY[name][0], GROSS_PROFIT))
    return tags


# --- the plan -----------------------------------------------------------------------------------


def _chronological(period: PeriodRef) -> tuple[int, int]:
    order: dict[FiscalPeriodName, int] = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5}
    return period.fiscal_year, order[period.fiscal_period]


def _check_plan(
    plan: QueryPlan, catalog: Mapping[str, MetricDef], universe: Collection[int] | None
) -> tuple[str, list[str], list[int], list[PeriodRef]]:
    """(operation, metrics, ciks, periods), or UnsupportedPlan. Nothing is defaulted here."""
    op = plan.operation
    if op is None:
        raise UnsupportedPlan("the plan names no operation")
    metrics = list(dict.fromkeys(plan.metrics))
    if not metrics:
        raise UnsupportedPlan("the plan names no catalog metric")
    unknown = [m for m in metrics if m not in catalog]
    if unknown:
        raise UnsupportedPlan(f"not in the catalog: {', '.join(unknown)}")
    if any(c.cik is None for c in plan.companies):
        raise UnsupportedPlan("a company in the plan was not resolved to a CIK")
    ciks = list(dict.fromkeys(c.cik for c in plan.companies if c.cik is not None))
    if op == "rank":
        if len(ciks) == 1:
            raise UnsupportedPlan("a ranking needs two or more companies, or none (all tracked)")
        if not ciks and not universe:
            raise UnsupportedPlan("a ranking with no company needs a set of companies to rank")
    elif not ciks:
        raise UnsupportedPlan("the plan names no company")
    unique = {(p.fiscal_year, p.fiscal_period): p for p in plan.periods}
    periods = sorted(unique.values(), key=_chronological)
    if not periods:
        raise UnsupportedPlan("the plan names no period")
    if op == "ratio" and any(catalog[m].unit != "ratio" for m in metrics):
        raise UnsupportedPlan("'ratio' applies only to a ratio metric in the catalog")
    if op == "growth":
        if any(catalog[m].unit == "ratio" for m in metrics):
            raise UnsupportedPlan("growth of a ratio metric is not defined")
        if len(periods) > 2:
            raise UnsupportedPlan("growth takes one or two periods; use a trend for more")
        if len({p.fiscal_period for p in periods}) > 1:
            raise UnsupportedPlan("growth compares like periods (two years, or the same quarter)")
    return op, metrics, ciks, periods


def compile_plan(
    plan: QueryPlan,
    conn: psycopg.Connection[Any],
    *,
    catalog: Mapping[str, MetricDef] | None = None,
    universe: Collection[int] | None = None,
    verified: Collection[str] | None = None,
) -> CompileOutcome:
    """Compile ``plan`` against the ``facts`` view on ``conn`` (read-only use).

    ``catalog`` defaults to the shipped metric_catalog.yaml; ``universe`` is the CIK set a
    ``rank`` over "S&P 500" ranges over (the engine passes the tracked CIKs). ``verified`` is the
    set a fallback's ``inputs_verified`` condition accepts; by default the reviewed list in
    ``eval/gen/numeric_metrics_verified.txt``.
    Raises ``UnsupportedPlan`` when the plan cannot be compiled at all; any missing number is a
    ``NotFound`` inside the outcome.
    """
    catalog = load_catalog() if catalog is None else catalog
    op, metrics, ciks, periods = _check_plan(plan, catalog, universe)
    run = _Run(catalog, conn, _tags_reachable(catalog, metrics), verified)
    out = CompileOutcome()
    with localcontext(Context()):  # the default context, whatever the caller changed
        for metric in metrics:
            if op == "growth":
                current = periods[-1]
                base = (
                    periods[0]
                    if len(periods) == 2
                    else current.model_copy(update={"fiscal_year": current.fiscal_year - 1})
                )
                run.load(ciks, current)
                run.load(ciks, base)
                for cik in ciks:
                    got = run.growth(metric, cik, current, base)
                    if isinstance(got, NumericResult):
                        out.results.append(got)
                    else:
                        out.not_found.append(got)
                continue
            for period in periods:
                if op == "rank":
                    _rank(run, out, metric, period, ciks, sorted(set(universe or ())))
                    continue
                run.load(ciks, period)
                found: list[tuple[int, _Value]] = []
                for cik in ciks:
                    value = run.evaluate(metric, cik, period)
                    if isinstance(value, _Value):
                        found.append((cik, value))
                        out.results.append(run.value_result(value, cik, period))
                    else:
                        out.not_found.append(run.not_found(metric, cik, period, value))
                if op == "compare" and len(ciks) == 2 and len(found) == 2:
                    out.results.append(run.difference_result(metric, found[0], found[1], period))
    return out


def _rank(
    run: _Run,
    out: CompileOutcome,
    metric: str,
    period: PeriodRef,
    named: list[int],
    universe: list[int],
) -> None:
    """Highest value first. Named companies are all listed, with a NotFound for each one that has
    no value. Over the universe only the top ten are returned and the companies without a usable
    value are left out silently (a known limit, ADR-047)."""
    ciks = named or universe
    run.load(ciks, period)
    scored: list[tuple[int, _Value]] = []
    for cik in ciks:
        value = run.evaluate(metric, cik, period)
        if isinstance(value, _Value):
            scored.append((cik, value))
        elif named:
            out.not_found.append(run.not_found(metric, cik, period, value))
    scored.sort(key=lambda item: (-item[1].value, item[0]))
    if not named:
        scored = scored[:RANK_LIMIT]
    for position, (cik, value) in enumerate(scored, start=1):
        out.results.append(run.value_result(value, cik, period, rank=position))
