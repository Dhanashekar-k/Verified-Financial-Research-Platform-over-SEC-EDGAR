"""Auto-generated numeric eval set (M4 step 3b; 05_data_contracts.md section 6).

    EDGARQA_SERVING_DSN=... uv run python -m eval.gen.make_numeric_set --n 300 --seed 13 \\
        --paraphrase batch --second-source companyfacts

Writes DRAFTS only (``eval/sets/drafts/numeric_auto.jsonl`` and its side files); Shekar reviews
and promotes. Built around the M4 catalog review (``docs/m4/m4_catalog_decisions.md``):

- **Verified metrics only.** ``eval/gen/numeric_metrics_verified.txt`` (Shekar's allowlist,
  sha-pinned per metric definition) gates every metric; an empty list refuses to run.
- **No first-populated-tag-wins.** A metric with two or more candidate tags (revenue, cost of
  revenue) goes through ``revenue_resolver``: consolidated income-statement presentation
  (FSDS ``pre``) and the gross-profit identity decide; an unreconciled material conflict
  ABSTAINS and is logged with its candidates (ADR-043).
- **Data-quality policies** (``numeric_policy``, ADR-044): us-gaap consolidated facts only
  (the ``facts`` view, never ``facts_dimensional``); ``qtrs = 0`` on a duration metric is
  quarantined and logged; year-to-date amounts never stand in for a quarter; derived Q4 rows
  are excluded by default; units must match, except the scoped FSDS ``USD`` ->
  ``USD/shares`` alias for per-share metrics; one declared version policy (default
  ``as_filed``: gold = the originally filed value, latest restated value recorded next to it,
  and a restated row's question says "as originally reported"); metric-aware tolerances;
  undefined ratios/growth abstain instead of being clamped.
- **Derived gross profit** only under every ``only_if`` condition of the catalog entry and
  labelled ``value_kind = derived`` (ADR-042); a fallback without ``only_if`` is never used.
- Templates single_value / growth / ratio / compare (120/80/60/40 at n=300), stratified by SIC
  division x fiscal year x metric x annual/quarterly, about 20 restated facts.
- Paraphrase via the LLM client (Haiku, seeded style variants since temperature is not
  accepted; ADR-045); a paraphrase that loses the company, the period, or "as originally
  reported" falls back to the template text.
- Second-source check of EVERY input fact against SEC companyfacts (through ``sec_client``),
  matched by accession, period and unit; every mismatch is listed in a report.

The 300 rows are NOT independent of the engine if both use the same mapping (the review's
point): they measure resolution and mapping, and the hand-audited sanity set is separate.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import random
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from eval.gen.catalog_review import (
    CATALOG_PATH,
    FALLBACK_CONDITIONS,
    VERIFIED_PATH,
    MetricDef,
    formula_refs,
    load_catalog,
    load_verified_metrics,
)
from eval.gen.draft_narrative import check_out_path, sic_division
from eval.gen.numeric_policy import (
    DEFAULT_VERSION_POLICY,
    TAXONOMY,
    Fact,
    ScreenLog,
    Versioned,
    VersionPolicy,
    denominator_ok,
    normalize_uom,
    screen,
    select_versions,
    tolerance_for,
)
from eval.gen.revenue_resolver import (
    SELECTED,
    Candidate,
    Resolution,
    gross_profit_check,
    resolve,
)
from eval.schemas import NumericRow

DRAFTS_DIR = Path("eval/sets/drafts")
OUT = DRAFTS_DIR / "numeric_auto.jsonl"
CASSETTES = Path("eval/cassettes/numeric_paraphrase")
DSN_ENV = "EDGARQA_SERVING_DSN"
SEED = 13
MIN_FY = 2020
TEMPLATE_SHARES = {"single_value": 120, "growth": 80, "ratio": 60, "compare": 40}
RESTATED_TARGET = 20  # at n = 300
RATIO_PLACES = Decimal("1e-10")
PARAPHRASE_MODEL_ROLE = "planner"  # Haiku 5.5 in the registry
PARAPHRASE_MAX_TOKENS = 256
Key = tuple[int, int, str]  # (cik, fiscal_year, fiscal_period)

# Gross-profit identity used to validate revenue / cost candidates (ADR-043).
RECONCILE_ROLE: dict[str, tuple[str, Literal["revenue", "cost"]]] = {
    "revenue": ("cost_of_revenue", "revenue"),
    "cost_of_revenue": ("revenue", "cost"),
}


# --- resolved values ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Value:
    metric: str
    cik: int
    fiscal_year: int
    fiscal_period: str
    value: Decimal
    unit: str
    period_end: date
    kind: Literal["reported", "derived"]
    inputs: tuple[Versioned, ...]  # the facts the value is computed from
    resolution: str

    @property
    def key(self) -> Key:
        return (self.cik, self.fiscal_year, self.fiscal_period)

    @property
    def fact_ids(self) -> list[int]:
        return [v.fact.fact_id for v in self.inputs]

    @property
    def tags(self) -> list[str]:
        return [v.fact.tag for v in self.inputs]

    @property
    def is_restated(self) -> bool:
        return any(v.is_restated for v in self.inputs)


@dataclass(frozen=True)
class Abstention:
    metric: str
    cik: int
    fiscal_year: int
    fiscal_period: str
    reason: str
    candidates: tuple[Candidate, ...] = ()


@dataclass
class ValueTable:
    values: dict[tuple[str, int, int, str], Value] = field(default_factory=dict)
    abstentions: list[Abstention] = field(default_factory=list)
    # resolver cases whose candidates disagree materially, with their (cik, fy, fp)
    conflicts: list[tuple[Key, Resolution]] = field(default_factory=list)
    log: ScreenLog = field(default_factory=ScreenLog)
    presentation_known: int = 0
    presentation_unknown: int = 0

    def get(self, metric: str, key: Key) -> Value | None:
        return self.values.get((metric, *key))


Presentation = Callable[[set[str]], dict[str, frozenset[str]]]


def _evaluate(expr: str, env: dict[str, Decimal]) -> Decimal:
    """Evaluate a catalog formula (validated by ``formula_refs``: names, numbers, + - * /)."""

    def ev(node: ast.AST) -> Decimal:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.Constant):
            return Decimal(str(node.value))
        if isinstance(node, ast.UnaryOp):
            return -ev(node.operand)
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            return a / b
        raise ValueError(f"unsupported node {type(node).__name__}")

    formula_refs(expr)  # raises on anything outside the grammar
    return ev(ast.parse(expr, mode="eval"))


def ordered_refs(expr: str) -> list[str]:
    """Catalog keys in the order the formula names them (``revenue - cost_of_revenue`` ->
    [revenue, cost_of_revenue]), so provenance lists inputs as the formula reads."""
    formula_refs(expr)  # validates the grammar
    out: list[str] = []

    class _Names(ast.NodeVisitor):  # depth-first, left before right: reading order
        def visit_Name(self, node: ast.Name) -> None:
            if node.id not in out:
                out.append(node.id)

    _Names().visit(ast.parse(expr, mode="eval"))
    return out


def fallback_blockers(
    metric: str, m: MetricDef, inputs: Sequence[Value], verified: set[str]
) -> list[str]:
    """The ``only_if`` conditions the inputs violate (empty = the fallback may be used).

    A fallback with no ``only_if`` is never used; a condition the generator can't check would
    be unknown to Literal validation already."""
    if m.only_if is None:
        return ["no_only_if_declared"]
    out: list[str] = []
    checks: dict[str, Callable[[], bool]] = {
        "same_entity": lambda: len({v.cik for v in inputs}) == 1,
        "same_period": lambda: (
            len({v.period_end for v in inputs}) == 1
            and len({f.fact.qtrs for v in inputs for f in v.inputs}) == 1
        ),
        "same_currency": lambda: {f.fact.uom for v in inputs for f in v.inputs} == {m.unit},
        "same_scope": lambda: (
            {f.fact.taxonomy for v in inputs for f in v.inputs} == {TAXONOMY}
            and all(v.kind == "reported" for v in inputs)
        ),
        "inputs_verified": lambda: (
            all(v.metric in verified for v in inputs)
            and all(v.resolution in SELECTED for v in inputs)
        ),
    }
    for cond in m.only_if:
        if cond not in FALLBACK_CONDITIONS or not checks[cond]():
            out.append(cond)
    return out


def build_values(
    catalog: dict[str, MetricDef],
    verified: set[str],
    facts: Iterable[Fact],
    *,
    presentation: Presentation | None = None,
    policy: VersionPolicy = DEFAULT_VERSION_POLICY,
    allow_derived_q4: bool = False,
) -> ValueTable:
    """Screen, version and resolve every tag-based metric; then apply fallbacks.

    Tags of unverified metrics may still be read as EVIDENCE (the gross-profit identity), but
    only verified metrics produce values."""
    table = ValueTable()
    tag_metric: dict[str, str] = {}
    for name, m in catalog.items():
        for t in m.tags or []:
            tag_metric.setdefault(t, name)

    grouped: dict[tuple[str, int, int, str], list[Fact]] = defaultdict(list)
    for f in facts:
        owner = tag_metric.get(f.tag)
        if owner is None:
            continue
        m = catalog[owner]
        assert m.period_type is not None  # tag-based metrics always have one
        if screen(
            f,
            unit=m.unit,
            period_type=m.period_type,
            log=table.log,
            allow_derived_q4=allow_derived_q4,
        ):
            grouped[(owner, f.cik, f.fiscal_year, f.fiscal_period)].append(normalize_uom(f, m.unit))

    versioned = {k: select_versions(v, policy) for k, v in grouped.items()}
    accessions = {v.fact.accession for vs in versioned.values() for v in vs}
    pre = presentation(accessions) if presentation is not None else {}

    def presented(vs: Sequence[Versioned]) -> frozenset[str] | None:
        # evidence only when EVERY candidate's filing is in the pre snapshot: an unknown
        # filing must not read as "not presented"
        if not all(v.fact.accession in pre for v in vs):
            table.presentation_unknown += 1
            return None
        table.presentation_known += 1
        return frozenset(v.fact.tag for v in vs if v.fact.tag in pre[v.fact.accession])

    for (name, cik, fy, fp), vs in sorted(versioned.items(), key=lambda kv: kv[0]):
        m = catalog[name]
        assert m.tags is not None
        if name not in verified and name not in RECONCILE_ROLE and name != "gross_profit":
            continue
        reconciles = None
        if name in RECONCILE_ROLE and "gross_profit" in catalog:
            other, role = RECONCILE_ROLE[name]
            gp = versioned.get(("gross_profit", cik, fy, fp), [])
            gp_value = gp[0].value if len(gp) == 1 else None
            reconciles = gross_profit_check(
                gp_value, [v.value for v in versioned.get((other, cik, fy, fp), [])], role=role
            )
        res = resolve(
            name,
            m.tags,
            vs,
            unit=m.unit,
            presented_tags=presented(vs) if len(m.tags) > 1 else None,
            reconciles=reconciles,
        )
        if len({c.value for c in res.candidates}) > 1 and res.reason not in (
            "single_candidate",
            "candidates_agree",
            "multiple_periods",  # not a tag disagreement: kept out of the resolver audit
        ):
            table.conflicts.append(((cik, fy, fp), res))
        if name not in verified:
            continue
        if res.chosen is None:
            table.abstentions.append(Abstention(name, cik, fy, fp, res.reason, res.candidates))
            continue
        table.values[(name, cik, fy, fp)] = Value(
            name,
            cik,
            fy,
            fp,
            res.chosen.value,
            m.unit,
            res.chosen.fact.period_end,
            "reported",
            (res.chosen,),
            res.reason,
        )

    # fallback formulas (gross profit = revenue - cost of revenue), never over a reported value
    # or over an abstention of the metric's own tags
    for name, m in catalog.items():
        if m.fallback_formula is None or name not in verified:
            continue
        own_abstained = {
            (a.cik, a.fiscal_year, a.fiscal_period) for a in table.abstentions if a.metric == name
        }
        refs = ordered_refs(m.fallback_formula)
        keys = {(c, y, p) for (mm, c, y, p) in table.values if mm == refs[0]}
        for key in sorted(keys):
            if table.get(name, key) is not None or key in own_abstained:
                continue
            inputs = [table.get(r, key) for r in refs]
            if any(v is None for v in inputs):
                continue
            ins = [v for v in inputs if v is not None]
            blockers = fallback_blockers(name, m, ins, verified)
            if blockers:
                table.abstentions.append(
                    Abstention(name, *key, "fallback_blocked:" + ",".join(blockers))
                )
                continue
            val = _evaluate(m.fallback_formula, {v.metric: v.value for v in ins})
            table.values[(name, *key)] = Value(
                name,
                key[0],
                key[1],
                key[2],
                val,
                m.unit,
                ins[0].period_end,
                "derived",
                tuple(f for v in ins for f in v.inputs),
                "fallback_formula",
            )
    return table


def ratio_value(
    name: str, m: MetricDef, key: Key, table: ValueTable
) -> tuple[Value | None, str | None]:
    """A ``formula`` metric (ratio) for one key, or the abstain reason."""
    assert m.formula is not None
    refs = ordered_refs(m.formula)
    inputs = {r: table.get(r, key) for r in refs}
    if any(v is None for v in inputs.values()):
        return None, "missing_input"
    ins = {r: v for r, v in inputs.items() if v is not None}
    tree = ast.parse(m.formula, mode="eval").body
    if isinstance(tree, ast.BinOp) and isinstance(tree.op, ast.Div):
        den = tree.right
        if isinstance(den, ast.Name):
            why = denominator_ok(ins[den.id].value, ins[den.id].unit)
            if why:
                return None, why
    if len({v.period_end for v in ins.values()}) != 1:
        return None, "inputs_period_mismatch"
    val = _evaluate(m.formula, {r: v.value for r, v in ins.items()}).quantize(RATIO_PLACES)
    first = next(iter(ins.values()))
    kind: Literal["reported", "derived"] = "derived"
    return (
        Value(
            name,
            key[0],
            key[1],
            key[2],
            val,
            m.unit,
            first.period_end,
            kind,
            tuple(f for v in ins.values() for f in v.inputs),
            "formula",
        ),
        None,
    )


# --- question items -----------------------------------------------------------------------------

QType = Literal["single_value", "growth", "ratio", "compare"]


@dataclass(frozen=True)
class Item:
    qtype: QType
    metric: str
    cik: int
    fiscal_year: int
    fiscal_period: str
    gold: Decimal
    unit: str
    values: tuple[Value, ...]  # primary first; growth: (current, prior); compare: (a, b)
    division: str

    @property
    def stratum(self) -> tuple[str, int, str, str]:
        cadence = "annual" if self.fiscal_period == "FY" else "quarterly"
        return (self.division, self.fiscal_year, self.metric, cadence)

    @property
    def restated(self) -> bool:
        return any(v.is_restated for v in self.values)

    @property
    def uid(self) -> str:
        parts = [self.qtype, self.metric, str(self.fiscal_year), self.fiscal_period]
        parts += [str(v.cik) for v in self.values]
        return "|".join(parts)


def build_items(
    catalog: dict[str, MetricDef],
    verified: set[str],
    table: ValueTable,
    sic: dict[int, int | None],
    seed: int = SEED,
) -> dict[QType, list[Item]]:
    items: dict[QType, list[Item]] = {k: [] for k in ("single_value", "growth", "ratio", "compare")}
    by_metric: dict[str, list[Value]] = defaultdict(list)
    for v in table.values.values():
        by_metric[v.metric].append(v)

    for name in sorted(verified):
        m = catalog[name]
        if m.formula is not None:
            keys = sorted({(c, y, p) for (_, c, y, p) in table.values})
            for key in keys:
                val, why = ratio_value(name, m, key, table)
                if val is None:
                    if why not in ("missing_input",):
                        table.abstentions.append(Abstention(name, *key, why or "unknown"))
                    continue
                items["ratio"].append(
                    Item(
                        "ratio",
                        name,
                        key[0],
                        key[1],
                        key[2],
                        val.value,
                        m.unit,
                        (val,),
                        sic_division(sic.get(key[0])),
                    )
                )
            continue
        vals = sorted(by_metric.get(name, []), key=lambda v: v.key)
        for v in vals:
            items["single_value"].append(
                Item(
                    "single_value",
                    name,
                    v.cik,
                    v.fiscal_year,
                    v.fiscal_period,
                    v.value,
                    m.unit,
                    (v,),
                    sic_division(sic.get(v.cik)),
                )
            )
            if m.period_type == "duration" and m.unit == "USD":
                prior = table.get(name, (v.cik, v.fiscal_year - 1, v.fiscal_period))
                why = denominator_ok(prior.value) if prior is not None else None
                if why is not None:  # undefined growth is counted, not silently dropped
                    table.abstentions.append(Abstention(name, *v.key, f"growth:{why}"))
                elif prior is not None:
                    g = ((v.value - prior.value) / prior.value).quantize(RATIO_PLACES)
                    items["growth"].append(
                        Item(
                            "growth",
                            name,
                            v.cik,
                            v.fiscal_year,
                            v.fiscal_period,
                            g,
                            "ratio",
                            (v, prior),
                            sic_division(sic.get(v.cik)),
                        )
                    )
        # compare: two companies, same metric, FY, same SIC division; larger first
        if m.unit in ("USD", "shares"):
            pools: dict[tuple[int, str], list[Value]] = defaultdict(list)
            for v in vals:
                if v.fiscal_period == "FY":
                    pools[(v.fiscal_year, sic_division(sic.get(v.cik)))].append(v)
            for (fy, d), pool in sorted(pools.items()):
                rng = random.Random(f"{seed}|{name}|{fy}|{d}")
                pool = sorted(pool, key=lambda v: v.cik)
                rng.shuffle(pool)
                for a, b in zip(pool[0::2], pool[1::2], strict=False):
                    hi, lo = (a, b) if a.value >= b.value else (b, a)
                    if hi.value == lo.value:
                        continue
                    items["compare"].append(
                        Item(
                            "compare",
                            name,
                            hi.cik,
                            fy,
                            "FY",
                            hi.value - lo.value,
                            m.unit,
                            (hi, lo),
                            d,
                        )
                    )
    return items


def _rank(seed: int, uid: str) -> str:
    return hashlib.sha256(f"{seed}|{uid}".encode()).hexdigest()


def stratified_pick(items: Sequence[Item], n: int, seed: int, taken: set[str]) -> list[Item]:
    """Round-robin over strata (SIC division x FY x metric x annual/quarterly) in a seeded
    order, one item per stratum per round, never reusing a (company, metric, period)."""
    strata: dict[tuple[str, int, str, str], list[Item]] = defaultdict(list)
    for it in items:
        strata[it.stratum].append(it)
    order = sorted(strata, key=lambda k: _rank(seed, repr(k)))
    queues = {k: sorted(strata[k], key=lambda it: _rank(seed, it.uid)) for k in order}
    out: list[Item] = []
    while len(out) < n and any(queues.values()):
        for k in order:
            q = queues[k]
            while q:
                it = q.pop(0)
                fact_key = f"{it.qtype}|{it.metric}|{it.cik}|{it.fiscal_year}|{it.fiscal_period}"
                if fact_key in taken:
                    continue
                taken.add(fact_key)
                out.append(it)
                break
            if len(out) >= n:
                break
    return out


def sample(items: dict[QType, list[Item]], n: int, seed: int) -> list[Item]:
    total = sum(TEMPLATE_SHARES.values())
    counts = {k: round(n * v / total) for k, v in TEMPLATE_SHARES.items()}
    counts["single_value"] += n - sum(counts.values())
    restated_n = round(n * RESTATED_TARGET / total)
    taken: set[str] = set()
    picked: list[Item] = []
    single = items["single_value"]
    restated = stratified_pick([i for i in single if i.restated], restated_n, seed, taken)
    picked += restated
    picked += stratified_pick(single, counts["single_value"] - len(restated), seed, taken)
    others: tuple[QType, ...] = ("growth", "ratio", "compare")
    for qt in others:
        picked += stratified_pick(items[qt], counts[qt], seed, taken)
    return picked


# --- question text ------------------------------------------------------------------------------


def _label(m: MetricDef, name: str) -> str:
    label = m.label or name.replace("_", " ")
    return label if label[:2].isupper() else label[0].lower() + label[1:]


def period_phrase(fy: int, fp: str) -> str:
    return f"fiscal year {fy}" if fp == "FY" else f"{fp} of fiscal year {fy}"


def template_question(
    item: Item, m: MetricDef, names: dict[int, str], policy: str
) -> tuple[str, str]:
    label = _label(m, item.metric)
    co = names.get(item.cik, f"CIK {item.cik}")
    per = period_phrase(item.fiscal_year, item.fiscal_period)
    instant = m.period_type == "instant"
    if item.qtype == "single_value":
        q = (
            f"What was {co}'s {label} at the end of {per}?"
            if instant
            else f"What was {co}'s {label} in {per}?"
        )
        tid = "single_v2" if instant else "single_v1"
    elif item.qtype == "growth":
        prev = period_phrase(item.fiscal_year - 1, item.fiscal_period)
        q = f"By what percentage did {co}'s {label} change from {prev} to {per}?"
        tid = "growth_v1"
    elif item.qtype == "ratio":
        q = f"What was {co}'s {label} in {per}?"
        tid = "ratio_v1"
    else:
        other = names.get(item.values[1].cik, f"CIK {item.values[1].cik}")
        q = (
            f"How much higher was {co}'s {label} than {other}'s at the end of {per}?"
            if instant
            else f"How much higher was {co}'s {label} than {other}'s in {per}?"
        )
        tid = "compare_v1"
    if item.restated:
        suffix = "as originally reported" if policy == "as_filed" else "as most recently restated"
        q = q[:-1] + f", {suffix}?"
    return q, tid


class Paraphrase(BaseModel):
    question: str


PARAPHRASE_SYSTEM = (
    "You rewrite financial questions about SEC filings so they read like a real user's "
    "question, in the requested style. Keep the meaning EXACTLY: the same company (you may use "
    "its common name), the same fiscal year(s) written as four-digit numbers, the same quarter "
    "if one is named (write it as Q1, Q2, Q3 or Q4), the same metric (never swap e.g. 'net "
    "income' for 'operating income' or 'profit'), and keep 'as originally reported' or 'as "
    "most recently restated' if present. Never add a number, an answer, or a new condition. "
    "Return only the rewritten question."
)
STYLES = (
    "plain and direct",
    "a busy analyst's terse shorthand",
    "a retail investor's conversational tone",
    "formal and precise",
    "starting with 'Can you tell me'",
    "a short question a student might ask",
)


def style_for(row_id: str, seed: int) -> str:
    return STYLES[int(_rank(seed, row_id), 16) % len(STYLES)]


def _words(text: str) -> list[str]:
    """Lower-case alphanumeric words; apostrophes and hyphens split ("Apple's" -> apple, s;
    "Coca-Cola" -> coca, cola), so possessives and hyphen spellings compare equal."""
    return re.findall(r"[a-z0-9&]+", text.lower())


NAME_STOPWORDS = frozenset({"the", "a", "an", "of", "and", "&", "inc", "corp", "co", "company"})


def distinctive_token(name: str) -> str:
    """First word of a company name that identifies it ("The Home Depot" -> "home",
    "A. O. Smith" -> "smith"); a paraphrase must keep it as a whole word."""
    for w in _words(name):
        if len(w) > 1 and w not in NAME_STOPWORDS:
            return str(w)
    return ""


def paraphrase_drift(
    item: Item, names: dict[int, str], tickers: dict[int, str | None], text: str
) -> str | None:
    """Why a paraphrase is rejected (None = it keeps the facts the question depends on)."""
    low = text.lower()
    words = set(_words(text))
    for v in item.values:
        token = distinctive_token(names.get(v.cik, ""))
        ticker = _words(tickers.get(v.cik) or "")
        if not ((token and token in words) or (ticker and set(ticker) <= words)):
            return f"company_lost:{v.cik}"
    years = {item.fiscal_year} | ({item.fiscal_year - 1} if item.qtype == "growth" else set())
    for y in years:
        if str(y) not in text:
            return f"year_lost:{y}"
    if item.fiscal_period != "FY" and item.fiscal_period.lower() not in low:
        return "quarter_lost"
    if item.restated and "originally" not in low and "restated" not in low:
        return "version_qualifier_lost"
    return None


# --- second source (SEC companyfacts) -----------------------------------------------------------

CompanyFacts = Callable[[int], dict[str, Any] | None]
SecondSource = Literal["match", "mismatch", "unchecked"]


def check_fact(f: Fact, cf: dict[str, Any] | None) -> tuple[str, Decimal | None]:
    """'match' / 'mismatch' / 'not_found' for one fact against the company's companyfacts,
    matched on taxonomy, tag, unit, accession and period (start and end for durations)."""
    if cf is None:
        return "not_found", None
    entries = cf.get("facts", {}).get(f.taxonomy, {}).get(f.tag, {}).get("units", {}).get(f.uom, [])
    end = f.period_end.isoformat()
    start = f.period_start.isoformat() if f.period_start and f.qtrs else None
    hits = [
        e
        for e in entries
        if e.get("accn") == f.accession
        and e.get("end") == end
        and (start is None or e.get("start") == start)
    ]
    if not hits:
        return "not_found", None
    vals = {Decimal(str(e["val"])) for e in hits}
    if f.value in vals:  # companyfacts carries the filed value: exact, for every unit
        return "match", None
    return "mismatch", sorted(vals)[0]


def second_source(
    item_facts: dict[str, list[Fact]], companyfacts: CompanyFacts
) -> tuple[dict[str, SecondSource], list[dict[str, Any]]]:
    # one company's JSON at a time (tens of MB each when parsed), released after its facts
    by_cik: dict[int, list[tuple[str, Fact]]] = defaultdict(list)
    for row_id, facts in item_facts.items():
        for f in facts:
            by_cik[f.cik].append((row_id, f))
    per_fact: dict[tuple[str, int], tuple[str, Decimal | None]] = {}
    for cik in sorted(by_cik):
        cf = companyfacts(cik)
        for row_id, f in by_cik[cik]:
            per_fact[(row_id, f.fact_id)] = check_fact(f, cf)
        del cf
    status: dict[str, SecondSource] = {}
    mismatches: list[dict[str, Any]] = []
    for row_id, facts in item_facts.items():
        results = []
        for f in facts:
            res, other = per_fact[(row_id, f.fact_id)]
            results.append(res)
            if res == "mismatch":
                mismatches.append(
                    {
                        "row": row_id,
                        "fact_id": f.fact_id,
                        "cik": f.cik,
                        "tag": f.tag,
                        "accession": f.accession,
                        "period_end": f.period_end.isoformat(),
                        "qtrs": f.qtrs,
                        "ours": str(f.value),
                        "companyfacts": str(other),
                    }
                )
        status[row_id] = (
            "mismatch"
            if "mismatch" in results
            else "unchecked"
            if "not_found" in results
            else "match"
        )
    return status, mismatches


# --- rows ---------------------------------------------------------------------------------------


def to_row(
    row_id: str,
    item: Item,
    question: str,
    template_id: str,
    paraphrased: bool,
    policy: VersionPolicy,
    check: SecondSource,
) -> NumericRow:
    tol = tolerance_for(item.unit, item.gold)
    primary = item.values[0]
    latest: Decimal | None = None
    if item.qtype == "single_value" and primary.kind == "reported" and primary.is_restated:
        latest = primary.inputs[0].latest_value
    return NumericRow(
        id=row_id,
        question=question.replace("\x00", ""),  # NUL is the exact-Decimal JSON sentinel
        qtype=item.qtype,
        cik=item.cik,
        metric=item.metric,
        fiscal_year=item.fiscal_year,
        fiscal_period=item.fiscal_period,
        gold_value=item.gold,
        unit=item.unit,
        tolerance_rel=tol.rel,
        tolerance_abs=tol.abs_,
        tolerance_basis=tol.basis,
        gold_fact_ids=[i for v in item.values for i in v.fact_ids],
        template_id=template_id,
        paraphrased=paraphrased,
        second_source_check=check,
        value_basis=policy,
        latest_value=latest,
        is_restated=item.restated,
        value_kind="derived"
        if item.qtype != "single_value" or primary.kind == "derived"
        else "reported",
        source_tags=[t for v in item.values for t in v.tags],
        resolution=primary.resolution,
        compare_cik=item.values[1].cik if item.qtype == "compare" else None,
        period_end=primary.period_end,
    )


_DEC = "\x00dec:"


def _json_number(v: Any) -> Any:
    if isinstance(v, Decimal):  # exact: written as a raw JSON number by row_json, never a float
        return _DEC + (str(int(v)) if v == v.to_integral_value() else format(v.normalize(), "f"))
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _json_number(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_json_number(x) for x in v]
    return v


def dumps_exact(obj: Any) -> str:
    """JSON with every Decimal written as an exact raw number (no float round trip)."""
    text = json.dumps(_json_number(obj), ensure_ascii=False)
    return re.sub(r'"\\u0000dec:(-?[0-9.]+)"', r"\1", text)


def row_json(row: NumericRow) -> str:
    return dumps_exact(row.model_dump(mode="python"))


def stratification_table(items: Sequence[Item]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for dim, f in (
        ("qtype", lambda i: i.qtype),
        ("sic_division", lambda i: i.division),
        ("fiscal_year", lambda i: str(i.fiscal_year)),
        ("metric", lambda i: i.metric),
        ("cadence", lambda i: i.stratum[3]),
        ("restated", lambda i: str(i.restated)),
    ):
        out[dim] = dict(sorted(Counter(f(i) for i in items).items()))
    return out


def resolver_audit(
    conflicts: Sequence[tuple[Key, Resolution]], names: dict[int, str], k: int, seed: int
) -> str:
    """Markdown sample of resolver decisions on conflicting filings (the review's final step:
    test the resolver on conflicting filings BEFORE generating revenue gold)."""
    by_reason: dict[str, list[tuple[Key, Resolution]]] = defaultdict(list)
    for key, r in conflicts:
        by_reason[r.reason].append((key, r))
    lines = [
        "# Revenue / cost-of-revenue resolver audit",
        "",
        f"{len(conflicts)} materially conflicting (cik, period) cases; decisions by reason:",
        "",
    ]
    for reason, rs in sorted(by_reason.items()):
        lines.append(f"- {reason}: {len(rs)}")
    for reason, rs in sorted(by_reason.items()):
        lines += [
            "",
            f"## {reason}",
            "",
            "| company | FY | period | metric | candidates (tag = value, presented) | chosen |",
            "|---|---|---|---|---|---|",
        ]
        rs = sorted(rs, key=lambda kr: _rank(seed, f"{kr[1].metric}|{kr[0]}"))
        for (cik, fy, fp), r in rs[: max(1, k // len(by_reason))]:
            cands = "; ".join(f"`{c.tag}` = {c.value:,} ({c.presented})" for c in r.candidates)
            chosen = r.tag or "ABSTAIN"
            lines.append(
                f"| {names.get(cik, cik)} | {fy} | {fp} | {r.metric} | {cands} | {chosen} |"
            )
    return "\n".join(lines) + "\n"


# --- I/O ----------------------------------------------------------------------------------------

FACT_COLUMNS = (
    "fact_id, cik, tag, taxonomy, uom, qtrs, period_start, period_end, fiscal_year, "
    "fiscal_period, value, accession, form, filed, is_latest, is_original, is_restated, "
    "derived_from"
)


def facts_query(tags: Sequence[str], min_fy: int) -> tuple[str, list[Any]]:
    """Consolidated us-gaap facts for the given tags (the ``facts`` view only: facts_dimensional
    is never read, so a segment value can't leak into a company total)."""
    return (
        f"SELECT {FACT_COLUMNS} FROM facts WHERE array_position(%s::text[], tag) IS NOT NULL "
        "AND taxonomy = %s AND fiscal_year >= %s",
        [list(tags), TAXONOMY, min_fy],
    )


def fact_from_row(r: Sequence[Any]) -> Fact:
    return Fact(
        fact_id=int(r[0]),
        cik=int(r[1]),
        tag=r[2],
        taxonomy=r[3],
        uom=r[4],
        qtrs=int(r[5]),
        period_start=r[6],
        period_end=r[7],
        fiscal_year=int(r[8]),
        fiscal_period=r[9],
        value=Decimal(str(r[10])),
        accession=r[11],
        form=r[12],
        filed=r[13],
        is_latest=bool(r[14]),
        is_original=bool(r[15]),
        is_restated=bool(r[16]),
        derived_from=tuple(r[17]) if r[17] else None,
    )


def needed_tags(catalog: dict[str, MetricDef], verified: set[str]) -> list[str]:
    names = set(verified)
    for n in list(verified):
        if n in RECONCILE_ROLE:
            names |= {"revenue", "cost_of_revenue", "gross_profit"} & catalog.keys()
        m = catalog[n]
        for expr in (m.formula, m.fallback_formula):
            if expr:
                names |= formula_refs(expr)
    return sorted({t for n in names for t in (catalog[n].tags or [])})


def fsds_pre_presentation(table_dir: Path) -> Presentation:
    """Income-statement tags per accession from the local FSDS ``pre`` table (current Iceberg
    snapshot): stmt = 'IS' and not parenthetical."""

    def load(accessions: set[str]) -> dict[str, frozenset[str]]:
        import duckdb

        from eval.gen.iceberg_files import snapshot_files

        snap = snapshot_files(table_dir)
        rows = (
            duckdb.connect()
            .execute(
                "SELECT adsh, list(DISTINCT tag) FROM read_parquet(?) "
                "WHERE stmt = 'IS' AND inpth = 0 "
                "AND adsh IN (SELECT unnest(?::VARCHAR[])) GROUP BY adsh",
                [snap.files, sorted(accessions)],
            )
            .fetchall()
        )
        return {r[0]: frozenset(r[1]) for r in rows}

    return load


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=os.environ.get(DSN_ENV))
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--min-fy", type=int, default=MIN_FY)
    ap.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    ap.add_argument("--verified", type=Path, default=VERIFIED_PATH)
    ap.add_argument(
        "--policy", choices=["as_filed", "latest_restated"], default=DEFAULT_VERSION_POLICY
    )
    ap.add_argument("--allow-derived-q4", action="store_true")
    ap.add_argument(
        "--pre-table",
        type=Path,
        default=Path("warehouse/raw/fsds_pre"),
        help="local FSDS pre Iceberg table; 'none' disables presentation evidence",
    )
    ap.add_argument("--paraphrase", choices=["off", "batch", "record", "replay"], default="off")
    ap.add_argument("--max-usd", type=Decimal, default=Decimal("1"))
    ap.add_argument("--second-source", choices=["companyfacts", "off"], default="companyfacts")
    ap.add_argument(
        "--resolver-audit",
        type=int,
        default=0,
        metavar="K",
        help="only write drafts/revenue_resolver_audit.md with K conflicting cases",
    )
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args(argv)
    if not args.resolver_audit:
        check_out_path(args.out, overwrite=args.overwrite)
    if not args.dsn:
        ap.error(f"set ${DSN_ENV} or pass --dsn")

    catalog = load_catalog(args.catalog)
    verified = load_verified_metrics(args.verified, catalog)
    if not verified and not args.resolver_audit:
        raise SystemExit(f"{args.verified} lists no verified metric; nothing may be generated")
    audit_metrics = {"revenue", "cost_of_revenue"} & catalog.keys()
    # the audit reads only the resolved metrics and their identity evidence (no gold is built)
    use = audit_metrics if args.resolver_audit else verified

    import psycopg

    with psycopg.connect(args.dsn, options="-c default_transaction_read_only=on") as conn:
        sql, params = facts_query(needed_tags(catalog, use), args.min_fy)
        facts = [fact_from_row(r) for r in conn.execute(sql, params).fetchall()]
        comp = conn.execute("SELECT cik, name, ticker, sic FROM companies").fetchall()
    names = {int(r[0]): str(r[1]) for r in comp}
    tickers = {int(r[0]): r[2] for r in comp}
    sic = {int(r[0]): r[3] for r in comp}
    pres = None if str(args.pre_table) == "none" else fsds_pre_presentation(args.pre_table)
    table = build_values(
        catalog,
        use,
        facts,
        presentation=pres,
        policy=args.policy,
        allow_derived_q4=args.allow_derived_q4,
    )
    print(
        f"{len(facts)} facts; {len(table.values)} resolved values; "
        f"{len(table.abstentions)} abstentions; {len(table.log.quarantined)} quarantined"
    )

    if args.resolver_audit:
        path = DRAFTS_DIR / "revenue_resolver_audit.md"
        path.write_text(resolver_audit(table.conflicts, names, args.resolver_audit, args.seed))
        print(f"wrote {path}")
        return 0

    items = build_items(catalog, verified, table, sic, args.seed)
    picked = sample(items, args.n, args.seed)
    ids = [f"num-{i:04d}" for i in range(1, len(picked) + 1)]
    texts = {
        rid: template_question(it, catalog[it.metric], names, args.policy)
        for rid, it in zip(ids, picked, strict=True)
    }

    spent = Decimal(0)
    para: dict[str, str] = {}
    drift: dict[str, str] = {}
    if args.paraphrase != "off":
        para, drift, spent = run_paraphrase(args, picked, ids, texts, names, tickers)

    item_facts = {
        rid: [f.fact for v in it.values for f in v.inputs]
        for rid, it in zip(ids, picked, strict=True)
    }
    status: dict[str, SecondSource] = dict.fromkeys(ids, "unchecked")
    mismatches: list[dict[str, Any]] = []
    if args.second_source == "companyfacts":
        status, mismatches = second_source(item_facts, sec_companyfacts())

    rows = []
    for rid, it in zip(ids, picked, strict=True):
        q, tid = texts[rid]
        rows.append(to_row(rid, it, para.get(rid, q), tid, rid in para, args.policy, status[rid]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(row_json(r) + "\n" for r in rows))
    side = args.out.with_suffix("")
    Path(f"{side}.quarantine.jsonl").write_text(
        "".join(json.dumps(asdict(q)) + "\n" for q in table.log.quarantined)
    )
    Path(f"{side}.abstentions.jsonl").write_text(
        "".join(
            dumps_exact(
                {
                    "metric": a.metric,
                    "cik": a.cik,
                    "fiscal_year": a.fiscal_year,
                    "fiscal_period": a.fiscal_period,
                    "reason": a.reason,
                    "candidates": [asdict(c) for c in a.candidates],
                }
            )
            + "\n"
            for a in table.abstentions
        )
    )
    summary = {
        "rows": len(rows),
        "seed": args.seed,
        "version_policy": args.policy,
        "verified_metrics": sorted(verified),
        "stratification": stratification_table(picked),
        "quarantined": len(table.log.quarantined),
        "quarantined_by_reason": dict(Counter(q.reason for q in table.log.quarantined)),
        "excluded_by_reason": dict(table.log.excluded),
        "abstentions_by_reason": dict(Counter(a.reason for a in table.abstentions)),
        "presentation_known": table.presentation_known,
        "presentation_unknown": table.presentation_unknown,
        "second_source": dict(Counter(status.values())),
        "second_source_mismatches": mismatches,
        "paraphrased": len(para),
        "paraphrase_drift": drift,
        "paraphrase_cost_usd": str(spent),
    }
    Path(f"{side}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps({k: v for k, v in summary.items() if k != "second_source_mismatches"}, indent=2)
    )
    return 0


def sec_companyfacts() -> CompanyFacts:
    """companyfacts JSON per CIK through the shared rate-limited SEC client (None on 404)."""
    from pipelines.fetch.sec_client import SecClient, SecRequestError
    from pipelines.spark.spotcheck_companyfacts import companyfacts_url

    client = SecClient()

    def get(cik: int) -> dict[str, Any] | None:
        try:
            return client.get_json(companyfacts_url(cik))
        except SecRequestError as err:
            if err.status_code == 404:
                return None
            raise

    return get


def run_paraphrase(
    args: argparse.Namespace,
    picked: Sequence[Item],
    ids: Sequence[str],
    texts: dict[str, tuple[str, str]],
    names: dict[int, str],
    tickers: dict[int, str | None],
) -> tuple[dict[str, str], dict[str, str], Decimal]:
    from app.llm.client import BatchPending, LLMClient
    from app.llm.prices import cost_usd
    from app.llm.types import LLMMode

    client = LLMClient(mode=LLMMode(args.paraphrase), cassette_dir=CASSETTES)
    model = client.resolve(PARAPHRASE_MODEL_ROLE)

    def message(rid: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": f"Style: {style_for(rid, args.seed)}\nQuestion: {texts[rid][0]}",
            }
        ]

    worst = sum(
        (
            cost_usd(
                model,
                (len(PARAPHRASE_SYSTEM) + len(texts[r][0])) // 4 + 300,
                0,
                0,
                PARAPHRASE_MAX_TOKENS,
                batch=args.paraphrase == "batch",
            )
            for r in ids
        ),
        Decimal(0),
    )
    print(f"paraphrase worst case ${worst:.4f} (cap ${args.max_usd})")
    if worst > args.max_usd:
        raise SystemExit(f"paraphrase worst case ${worst:.4f} exceeds --max-usd {args.max_usd}")
    out: dict[str, str] = {}
    drift: dict[str, str] = {}
    spent = Decimal(0)
    for attempt in range(2):
        for rid, it in zip(ids, picked, strict=True):
            if rid in out or rid in drift:
                continue
            try:
                p, usage = client.structured(
                    PARAPHRASE_MODEL_ROLE,
                    PARAPHRASE_SYSTEM,
                    message(rid),
                    Paraphrase,
                    max_tokens=PARAPHRASE_MAX_TOKENS,
                )
            except BatchPending:
                if attempt == 1:
                    drift[rid] = "no_result"
                continue
            spent += usage.cost_usd
            why = paraphrase_drift(it, names, tickers, p.question)
            if why:
                drift[rid] = why
            else:
                out[rid] = p.question.strip()
        if client.mode is LLMMode.batch and client.pending and attempt == 0:
            client.run_pending_batch(poll_interval_s=20)
        else:
            break
    return out, drift, spent + client.failed_usage_usd


if __name__ == "__main__":
    raise SystemExit(main())
