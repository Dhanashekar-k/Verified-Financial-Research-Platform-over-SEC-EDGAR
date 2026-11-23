"""Tag-chain review packet for the metric catalog (M4 step 3a).

Coverage only catches empty chains. A tag that resolves to the WRONG concept passes coverage
and also passes the companyfacts second-source check, because both sides read the same tag.
So Shekar reviews every metric at tag-chain level from this packet, and lists the metrics he
verified, each with the sha of its current definition, in
``eval/gen/numeric_metrics_verified.txt``. The numeric generator (step 3b, not written yet)
will use ``load_verified_metrics`` and accept only listed metrics whose definition still
matches the listed sha.

For each tag-based metric, and for each tag in its chain, the packet shows:
- how many companies have the tag, and how many resolve through it (it is the first chain tag
  with a value for that (cik, fiscal_year, fiscal_period), so it wins);
- sample rows WHERE THE TAG WINS (earlier chain tags absent for that key), one per company, with
  qtrs and period_end, so a fallback's real values are visible;
- the uom and qtrs distributions, with a flag when qtrs contradicts ``period_type`` (instant
  means qtrs 0; a duration FY fact means qtrs 4);
- for each pair of chain tags: how often both report the same period (same period_end and
  qtrs), and among those the rows that differ by more than 1% (count plus examples ordered by
  relative difference). Pairs that never report the same period are printed as
  NON-COMPARABLE rather than as "0 conflicts";
- the us-gaap label/definition from an optional local taxonomy JSON (no web lookups).
For a ``fallback_formula`` it shows how often the fallback fires (no chain tag, all inputs
present); for a ratio metric ``a / b`` it shows min / median / max / n of the ratio over FY rows.

Usage::

    EDGARQA_SERVING_DSN=... python -m eval.gen.catalog_review \\
        --out docs/milestones/M04-catalog-review.md

It runs read-only SELECTs against the serving Postgres ``facts`` and ``companies`` views. The
SQL builders are pure functions and portable to DuckDB, which the tests run them against.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
from collections.abc import Callable, Sequence
from itertools import combinations
from pathlib import Path
from typing import Any, Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

CATALOG_PATH = Path("app/engine/plan/metric_catalog.yaml")
VERIFIED_PATH = Path("eval/gen/numeric_metrics_verified.txt")
DSN_ENV = "EDGARQA_SERVING_DSN"
CONFLICT_REL = 0.01
SAMPLE_ROWS = 5
SHA_LEN = 12

Query = tuple[str, list[Any]]
Fetch = Callable[[str, Sequence[Any]], list[tuple[Any, ...]]]


# --- catalog model ---------------------------------------------------------------------------


FallbackCondition = Literal[
    "same_entity",  # every input fact has the same cik
    "same_period",  # same period_end, and same qtrs for durations
    "same_currency",  # same uom (USD)
    "same_scope",  # consolidated totals only (the facts view; never facts_dimensional)
    "inputs_verified",  # every input metric is on the verified allowlist and resolved cleanly
]
FALLBACK_CONDITIONS: tuple[str, ...] = get_args(FallbackCondition)


class MetricDef(BaseModel):
    """One metric_catalog.yaml entry (05_data_contracts.md 4.2)."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    unit: Literal["USD", "ratio", "USD/shares", "shares"]
    period_type: Literal["duration", "instant"] | None = None
    tags: list[str] | None = None
    formula: str | None = None
    fallback_formula: str | None = None
    synonyms: list[str] = []
    # ADR-042 (M4 catalog review). A distinct presentation basis makes a metric its own concept,
    # never a fallback of a generic one (bank revenue, tax-inclusive revenue).
    presentation_basis: Literal["net_of_interest_expense", "including_assessed_tax"] | None = None
    # Conditions under which fallback_formula may be used; a consumer that can't check every
    # listed condition must not use the fallback. A fallback without only_if is never used by
    # the numeric generator.
    only_if: list[FallbackCondition] | None = None
    # How a fallback_formula value is labelled: "derived" (computed), never "reported".
    fallback_result: Literal["derived"] | None = None

    @model_validator(mode="after")
    def _tags_xor_formula(self) -> MetricDef:
        if (self.only_if is not None or self.fallback_result is not None) and (
            self.fallback_formula is None
        ):
            raise ValueError("`only_if` / `fallback_result` need a `fallback_formula`")
        if self.only_if is not None and not self.only_if:
            raise ValueError("`only_if` must list at least one condition (omit it instead)")
        if self.only_if is not None and len(set(self.only_if)) != len(self.only_if):
            raise ValueError(f"duplicate condition in only_if {self.only_if}")
        if (self.tags is None) == (self.formula is None):
            raise ValueError("a metric needs exactly one of `tags` or `formula`")
        if self.tags is not None:
            if not self.tags:
                raise ValueError("`tags` must not be empty")
            if len(set(self.tags)) != len(self.tags):
                raise ValueError(f"duplicate tag in chain {self.tags}")
            if self.period_type is None:
                raise ValueError("a tag-based metric needs `period_type`")
        if self.fallback_formula is not None and self.tags is None:
            raise ValueError("`fallback_formula` only makes sense next to `tags`")
        return self


def definition_sha(m: MetricDef) -> str:
    """12-hex sha of the parts of a definition that change VALUES: unit, period_type, tags (in
    order), formula and fallback_formula. Label and synonyms are excluded, so a wording change
    doesn't invalidate a verification, while any chain change does."""
    payload = m.model_dump(
        mode="json",
        exclude={"label", "synonyms", "presentation_basis", "only_if", "fallback_result"},
    )
    # ADR-042 fields enter the payload only when set, so adding them to the model left the sha
    # of every metric that doesn't use them unchanged (no verification is invalidated by it).
    for key in ("presentation_basis", "only_if", "fallback_result"):
        value = getattr(m, key)
        if value is not None:
            payload[key] = value
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:SHA_LEN]


def formula_refs(formula: str) -> set[str]:
    """Catalog keys a formula references.

    The formula must be an arithmetic expression over names and numbers using only
    + - * / and parentheses (no **, %, calls, attributes); anything else raises ValueError.
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as err:
        raise ValueError(f"bad token in formula {formula!r}: {err.msg}") from err
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            refs.add(node.id)
        elif isinstance(node, ast.Constant):
            if not isinstance(node.value, int | float) or isinstance(node.value, bool):
                raise ValueError(f"bad token in formula {formula!r}: {node.value!r}")
        elif isinstance(node, ast.BinOp):
            if not isinstance(node.op, ast.Add | ast.Sub | ast.Mult | ast.Div):
                raise ValueError(f"bad token in formula {formula!r}: {type(node.op).__name__}")
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, ast.USub):
                raise ValueError(f"bad token in formula {formula!r}: {type(node.op).__name__}")
        elif not isinstance(node, ast.Expression | ast.Load | ast.operator | ast.unaryop):
            raise ValueError(f"bad token in formula {formula!r}: {type(node).__name__}")
    return refs


def _ratio_parts(formula: str) -> tuple[str, str] | None:
    """(numerator, denominator) when the formula is exactly ``name / name``."""
    tree = ast.parse(formula, mode="eval").body
    if (
        isinstance(tree, ast.BinOp)
        and isinstance(tree.op, ast.Div)
        and isinstance(tree.left, ast.Name)
        and isinstance(tree.right, ast.Name)
    ):
        return tree.left.id, tree.right.id
    return None


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, MetricDef]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{path}: expected a non-empty mapping of metric -> definition")
    catalog = {str(name): MetricDef.model_validate(body) for name, body in raw.items()}
    for name, m in catalog.items():
        for expr in (m.formula, m.fallback_formula):
            if expr is None:
                continue
            unknown = formula_refs(expr) - catalog.keys()
            if unknown:
                raise ValueError(f"{name}: formula {expr!r} references unknown {sorted(unknown)}")
            if name in formula_refs(expr):
                raise ValueError(f"{name}: formula references itself")
    return catalog


def _dependencies(name: str, catalog: dict[str, MetricDef]) -> set[str]:
    """Every metric whose values ``name`` can be computed from (formula and fallback_formula,
    recursively), excluding ``name`` itself."""
    seen: set[str] = set()
    stack = [name]
    while stack:
        m = catalog[stack.pop()]
        for expr in (m.formula, m.fallback_formula):
            for ref in formula_refs(expr) if expr else set():
                if ref not in seen:
                    seen.add(ref)
                    stack.append(ref)
    seen.discard(name)
    return seen


_ALLOW_LINE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+sha=(?P<sha>[0-9a-f]+)$")


def load_verified_metrics(
    path: Path = VERIFIED_PATH, catalog: dict[str, MetricDef] | None = None
) -> set[str]:
    """Metrics Shekar verified at tag-chain level.

    Each line is ``<metric>  sha=<12 hex>`` where the sha is ``definition_sha`` of the metric
    as reviewed (the packet prints it). '#' starts a comment; blank lines are ignored. Fails
    when a name isn't in the catalog, when the definition changed since it was verified (sha
    mismatch), or when a metric's formula / fallback_formula inputs aren't verified too.
    """
    catalog = catalog if catalog is not None else load_catalog()
    names: set[str] = set()
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        match = _ALLOW_LINE.match(text)
        if match is None:
            raise ValueError(f"{path}:{lineno}: expected '<metric>  sha=<hex>', got {text!r}")
        name, sha = match["name"], match["sha"]
        if name not in catalog:
            raise ValueError(f"{path}:{lineno}: {name!r} is not a catalog metric")
        current = definition_sha(catalog[name])
        if sha != current:
            raise ValueError(
                f"{path}:{lineno}: {name} was verified as sha={sha} but its definition is now "
                f"sha={current}; review the chain again"
            )
        names.add(name)
    for name in sorted(names):
        missing = _dependencies(name, catalog) - names
        if missing:
            raise ValueError(f"{name} is verified but its inputs {sorted(missing)} are not")
    return names


# --- SQL builders (pure; Postgres and DuckDB) ---------------------------------------------------
#
# Chain membership and order use array_position(%s::text[], tag), which both engines support;
# NULL means "not in the chain".


def coverage_query(tag: str) -> Query:
    return (
        "SELECT count(DISTINCT cik), count(*) FROM facts WHERE tag = %s AND is_original",
        [tag],
    )


def winners_query(tags: Sequence[str]) -> Query:
    """Per tag: companies and (cik, FY, period) keys where it is the first chain tag with a
    value."""
    sql = (
        "WITH w AS ("
        "SELECT DISTINCT ON (cik, fiscal_year, fiscal_period) cik, tag FROM facts "
        "WHERE array_position(%s::text[], tag) IS NOT NULL AND is_original "
        "ORDER BY cik, fiscal_year, fiscal_period, array_position(%s::text[], tag)"
        ") SELECT tag, count(DISTINCT cik), count(*) FROM w GROUP BY tag"
    )
    return sql, [list(tags), list(tags)]


_SAMPLE_COLS = "c.name, f.fiscal_year, f.fiscal_period, f.value, f.uom, f.qtrs, f.period_end"


def samples_query(tag: str, limit: int = SAMPLE_ROWS) -> Query:
    """Deterministic FY rows of ``tag``, one per company (latest year), companies picked by md5."""
    sql = (
        "SELECT name, fiscal_year, fiscal_period, value, uom, qtrs, period_end FROM ("
        f"SELECT DISTINCT ON (f.cik) f.cik, {_SAMPLE_COLS} FROM facts f "
        "JOIN companies c ON c.cik = f.cik "
        "WHERE f.tag = %s AND f.is_original AND f.fiscal_period = 'FY' "
        "ORDER BY f.cik, f.fiscal_year DESC, f.fact_id"
        ") s ORDER BY md5(cik::text), cik LIMIT %s"
    )
    return sql, [tag, limit]


def winner_samples_query(tags: Sequence[str], index: int, limit: int = SAMPLE_ROWS) -> Query:
    """FY rows where ``tags[index]`` wins: no earlier chain tag has a value for the same
    (cik, fiscal_year, fiscal_period). One row per company."""
    earlier = list(tags[:index])
    not_exists = (
        "AND NOT EXISTS (SELECT 1 FROM facts e WHERE e.cik = f.cik "
        "AND e.fiscal_year = f.fiscal_year AND e.fiscal_period = f.fiscal_period "
        "AND e.is_original AND array_position(%s::text[], e.tag) IS NOT NULL) "
        if earlier
        else ""
    )
    sql = (
        "SELECT name, fiscal_year, fiscal_period, value, uom, qtrs, period_end FROM ("
        f"SELECT DISTINCT ON (f.cik) f.cik, {_SAMPLE_COLS} FROM facts f "
        "JOIN companies c ON c.cik = f.cik "
        "WHERE f.tag = %s AND f.is_original AND f.fiscal_period = 'FY' "
        f"{not_exists}"
        "ORDER BY f.cik, f.fiscal_year DESC, f.fact_id"
        ") s ORDER BY md5(cik::text), cik LIMIT %s"
    )
    params: list[Any] = [tags[index]] + ([earlier] if earlier else []) + [limit]
    return sql, params


_SAME_PERIOD = (
    "FROM facts a JOIN facts b ON a.cik = b.cik AND a.fiscal_year = b.fiscal_year "
    "AND a.fiscal_period = b.fiscal_period AND a.uom = b.uom AND a.qtrs = b.qtrs "
    "AND a.period_end = b.period_end "
    "WHERE a.tag = %s AND b.tag = %s AND a.is_original AND b.is_original"
)
_DIFFERS = " AND abs(a.value - b.value) > %s::numeric * greatest(abs(a.value), abs(b.value))"


def overlap_query(tag_a: str, tag_b: str) -> Query:
    """(same-period pairs, (cik, FY, period) keys where both tags report anything)."""
    sql = (
        f"SELECT (SELECT count(*) {_SAME_PERIOD}), "
        "(SELECT count(*) FROM (SELECT DISTINCT a.cik, a.fiscal_year, a.fiscal_period "
        "FROM facts a JOIN facts b ON a.cik = b.cik AND a.fiscal_year = b.fiscal_year "
        "AND a.fiscal_period = b.fiscal_period "
        "WHERE a.tag = %s AND b.tag = %s AND a.is_original AND b.is_original) k)"
    )
    return sql, [tag_a, tag_b, tag_a, tag_b]


def conflict_count_query(tag_a: str, tag_b: str, rel: float = CONFLICT_REL) -> Query:
    return f"SELECT count(*) {_SAME_PERIOD}{_DIFFERS}", [tag_a, tag_b, rel]


def conflict_examples_query(
    tag_a: str, tag_b: str, rel: float = CONFLICT_REL, limit: int = SAMPLE_ROWS
) -> Query:
    """The largest RELATIVE disagreements (not the largest companies)."""
    named = _SAME_PERIOD.replace(
        "FROM facts a", "FROM facts a JOIN companies c ON c.cik = a.cik", 1
    )
    sql = (
        "SELECT c.name, a.fiscal_year, a.fiscal_period, a.uom, a.value, b.value "
        f"{named}"
        f"{_DIFFERS} "
        "ORDER BY abs(a.value - b.value) / greatest(abs(a.value), abs(b.value)) DESC, a.fact_id "
        "LIMIT %s"
    )
    return sql, [tag_a, tag_b, rel, limit]


def uom_query(tag: str) -> Query:
    return (
        "SELECT uom, count(*) FROM facts WHERE tag = %s AND is_original "
        "GROUP BY uom ORDER BY count(*) DESC, uom",
        [tag],
    )


def qtrs_query(tag: str) -> Query:
    """(fiscal_period = 'FY', qtrs, count) for the tag."""
    return (
        "SELECT fiscal_period = 'FY', qtrs, count(*) FROM facts WHERE tag = %s AND is_original "
        "GROUP BY 1, 2 ORDER BY 1 DESC, 2",
        [tag],
    )


def _chain_exists(alias: str) -> str:
    return (
        f"EXISTS (SELECT 1 FROM facts {alias} WHERE {alias}.cik = k.cik "
        f"AND {alias}.fiscal_year = k.fiscal_year AND {alias}.fiscal_period = 'FY' "
        f"AND {alias}.is_original AND array_position(%s::text[], {alias}.tag) IS NOT NULL)"
    )


def fallback_fires_query(own_tags: Sequence[str], input_chains: Sequence[Sequence[str]]) -> Query:
    """FY (cik, year) keys where no own chain tag has a value but every input chain does."""
    all_tags = sorted({t for chain in input_chains for t in chain})
    conds = [f"NOT {_chain_exists('o')}"] + [
        _chain_exists(f"i{n}") for n in range(len(input_chains))
    ]
    sql = (
        "SELECT count(*) FROM (SELECT DISTINCT cik, fiscal_year FROM facts "
        "WHERE fiscal_period = 'FY' AND is_original "
        "AND array_position(%s::text[], tag) IS NOT NULL) k WHERE " + " AND ".join(conds)
    )
    return sql, [all_tags, list(own_tags), *[list(c) for c in input_chains]]


def _winner_cte(alias: str) -> str:
    return (
        f"{alias} AS (SELECT DISTINCT ON (cik, fiscal_year) cik, fiscal_year, value FROM facts "
        "WHERE array_position(%s::text[], tag) IS NOT NULL AND is_original "
        "AND fiscal_period = 'FY' AND qtrs = 4 AND uom = 'USD' "
        f"ORDER BY cik, fiscal_year, array_position(%s::text[], tag))"
    )


def ratio_stats_query(num_tags: Sequence[str], den_tags: Sequence[str]) -> Query:
    """min, median, max and n of num / den over FY rows (chain winners, USD, qtrs 4, den != 0)."""
    sql = (
        f"WITH {_winner_cte('n')}, {_winner_cte('d')}, "
        "r AS (SELECT (n.value / d.value)::float8 AS x FROM n JOIN d "
        "ON n.cik = d.cik AND n.fiscal_year = d.fiscal_year WHERE d.value <> 0) "
        "SELECT min(x), percentile_cont(0.5) WITHIN GROUP (ORDER BY x), max(x), count(*) FROM r"
    )
    return sql, [list(num_tags), list(num_tags), list(den_tags), list(den_tags)]


# --- rendering -------------------------------------------------------------------------------


def _definition(tag: str, labels: dict[str, dict[str, str]]) -> str:
    entry = labels.get(tag)
    if not entry:
        return "definition: look up in the us-gaap taxonomy"
    label = entry.get("label", "")
    definition = entry.get("definition", "")
    return f"label: {label}; definition: {definition}".strip()


def _sample_table(rows: list[tuple[Any, ...]]) -> list[str]:
    if not rows:
        return ["(no rows)"]
    out = [
        "| company | FY | period | value | uom | qtrs | period_end |",
        "|---|---|---|---|---|---|---|",
    ]
    out += ["| " + " | ".join(str(v) for v in r) + " |" for r in rows]
    return out


def period_type_flags(period_type: str | None, qtrs_rows: list[tuple[Any, ...]]) -> list[str]:
    """Contradictions between the declared period_type and the facts' qtrs."""
    flags: list[str] = []
    for is_fy, qtrs, count in qtrs_rows:
        if period_type == "instant" and qtrs != 0:
            flags.append(f"FLAG: instant metric but {count} facts have qtrs={qtrs}")
        elif period_type == "duration" and is_fy and qtrs != 4:
            flags.append(f"FLAG: duration metric but {count} FY facts have qtrs={qtrs}")
    return flags


def _render_tags(
    name: str, m: MetricDef, fetch: Fetch, labels: dict[str, dict[str, str]]
) -> list[str]:
    assert m.tags is not None
    lines = [f"- chain: {' -> '.join(m.tags)}"]
    if m.fallback_formula:
        lines.append(f"- fallback_formula: `{m.fallback_formula}`")
    lines.append("")

    wins = {row[0]: (row[1], row[2]) for row in fetch(*winners_query(m.tags))}
    lines += [
        "| # | tag | companies with tag | facts | companies it wins | keys it wins |",
        "|---|---|---|---|---|---|",
    ]
    for i, tag in enumerate(m.tags, start=1):
        cov = fetch(*coverage_query(tag))
        companies, facts = cov[0] if cov else (0, 0)
        won_c, won_k = wins.get(tag, (0, 0))
        lines.append(f"| {i} | `{tag}` | {companies} | {facts} | {won_c} | {won_k} |")
    lines.append("")

    for i, tag in enumerate(m.tags):
        lines += [f"### `{tag}`", "", f"- {_definition(tag, labels)}"]
        uoms = fetch(*uom_query(tag))
        lines.append("- uom: " + (", ".join(f"{u} ({n})" for u, n in uoms) or "(no facts)"))
        qtrs = fetch(*qtrs_query(tag))
        lines.append(
            "- qtrs: "
            + (", ".join(f"{'FY' if fy else 'non-FY'} qtrs={q} ({n})" for fy, q, n in qtrs) or "-")
        )
        lines += [f"- {f}" for f in period_type_flags(m.period_type, qtrs)]
        if i == 0:
            lines += ["", "Sample FY rows (one per company):", ""]
            lines += _sample_table(fetch(*samples_query(tag)))
        else:
            lines += [
                "",
                f"Rows where `{tag}` WINS (no earlier chain tag for that key):",
                "",
            ]
            lines += _sample_table(fetch(*winner_samples_query(m.tags, i)))
        lines.append("")

    for a, b in combinations(m.tags, 2):
        same_period, shared_keys = fetch(*overlap_query(a, b))[0]
        if not same_period:
            lines.append(
                f"- `{a}` vs `{b}`: NON-COMPARABLE: no same-period overlap "
                f"({shared_keys} (cik, FY, period) keys have both tags at different "
                "period_end/qtrs); conflicts cannot be checked"
            )
            continue
        count_rows = fetch(*conflict_count_query(a, b))
        count = count_rows[0][0] if count_rows else 0
        lines.append(
            f"- conflicts `{a}` vs `{b}` (>{CONFLICT_REL:.0%}): {count} of {same_period} "
            "same-period pairs"
        )
        if count:
            lines += [
                "",
                f"| company | FY | period | uom | {a} | {b} |",
                "|---|---|---|---|---|---|",
            ]
            examples = fetch(*conflict_examples_query(a, b))
            lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in examples]
            lines.append("")
    return lines


def render_metric(
    name: str,
    m: MetricDef,
    fetch: Fetch,
    labels: dict[str, dict[str, str]] | None = None,
    catalog: dict[str, MetricDef] | None = None,
) -> str:
    labels = labels or {}
    catalog = catalog or {}
    lines = [f"## {name}", ""]
    lines.append(f"- label: {m.label or '(none)'}; unit: {m.unit}; period_type: {m.period_type}")
    lines.append(f"- definition sha (copy into the allowlist): `{name}  sha={definition_sha(m)}`")
    if m.presentation_basis:
        lines.append(f"- presentation_basis: {m.presentation_basis} (its own concept, ADR-042)")
    if m.fallback_formula:
        conds = ", ".join(m.only_if) if m.only_if else "NONE (the generator never uses it)"
        lines.append(f"- fallback only_if: {conds}; result: {m.fallback_result or 'unlabelled'}")
    if m.tags is not None and len(m.tags) > 1:
        lines.append(
            "- multi-tag: candidates are resolved (statement presentation, gross-profit "
            "identity) or abstained, never first-populated-wins (ADR-043)"
        )
    if m.tags is not None:
        lines += _render_tags(name, m, fetch, labels)
        if m.fallback_formula and catalog:
            refs = sorted(formula_refs(m.fallback_formula))
            chains = [catalog[r].tags for r in refs]
            if all(c is not None for c in chains):
                fires = fetch(*fallback_fires_query(m.tags, [c for c in chains if c]))
                lines.append(
                    f"- fallback `{m.fallback_formula}` fires for {fires[0][0]} FY (cik, year) "
                    "keys (no chain tag, every input present)"
                )
            else:
                lines.append("- fallback inputs include derived metrics: fire count not computed")
    else:
        assert m.formula is not None
        lines.append(f"- formula: `{m.formula}` (derived; its inputs must be verified too)")
        parts = _ratio_parts(m.formula)
        num = catalog.get(parts[0]) if parts else None
        den = catalog.get(parts[1]) if parts else None
        if m.unit == "ratio" and num and den and num.tags and den.tags:
            lo, med, hi, n = fetch(*ratio_stats_query(num.tags, den.tags))[0]
            lines.append(
                f"- ratio over FY rows (chain winners, USD): min {lo}, median {med}, max {hi}, "
                f"n {n} (the fallback_formula of an input is not applied here)"
            )
        elif m.unit == "ratio":
            lines.append("- ratio stats not computed (formula is not `tag_metric / tag_metric`)")
    lines.append("")
    return "\n".join(lines)


def render_packet(
    catalog: dict[str, MetricDef],
    fetch: Fetch,
    labels: dict[str, dict[str, str]] | None = None,
) -> str:
    header = [
        "# Metric catalog: tag-chain review packet",
        "",
        "Generated by `eval/gen/catalog_review.py`. For every metric, check that each tag in the "
        "chain is the same concept as the metric (not merely populated), that fallback tags don't "
        "change scope (for example NCI, restricted cash, current portion), that the rows where a "
        "fallback wins look right, and that conflicts and qtrs flags are explained. List each "
        "verified metric with its sha in `eval/gen/numeric_metrics_verified.txt`.",
        "",
    ]
    body = [render_metric(name, m, fetch, labels, catalog) for name, m in catalog.items()]
    return "\n".join(header + body)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--dsn",
        default=os.environ.get(DSN_ENV),
        help=f"serving Postgres DSN (default: ${DSN_ENV}; keeps it out of ps and shell history)",
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    parser.add_argument(
        "--taxonomy-labels",
        type=Path,
        default=None,
        help="optional local JSON {tag: {label, definition}}; no web lookups",
    )
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error(f"no DSN: set ${DSN_ENV} or pass --dsn")

    catalog = load_catalog(args.catalog)
    labels: dict[str, dict[str, str]] = {}
    if args.taxonomy_labels is not None:
        if not args.taxonomy_labels.exists():
            parser.error(f"--taxonomy-labels {args.taxonomy_labels} does not exist")
        labels = json.loads(args.taxonomy_labels.read_text())

    import psycopg

    with psycopg.connect(args.dsn) as conn:
        conn.read_only = True

        def fetch(sql: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
            return list(conn.execute(sql, list(params)).fetchall())

        packet = render_packet(catalog, fetch, labels)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(packet)
    print(f"wrote {args.out} ({len(catalog)} metrics)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
