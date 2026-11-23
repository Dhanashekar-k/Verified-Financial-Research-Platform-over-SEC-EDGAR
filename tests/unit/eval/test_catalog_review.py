"""Metric catalog loader, verified-metrics allowlist and the tag-chain review packet (M4 3a).

The SQL builders are executed for real against an in-memory DuckDB with a toy ``facts`` /
``companies`` table (the builders are written to run on both Postgres and DuckDB), so the
queries are checked by results, not by their text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError

from eval.gen import catalog_review as cr
from eval.gen.catalog_review import (
    CATALOG_PATH,
    MetricDef,
    conflict_count_query,
    conflict_examples_query,
    coverage_query,
    definition_sha,
    fallback_fires_query,
    formula_refs,
    load_catalog,
    load_verified_metrics,
    overlap_query,
    period_type_flags,
    qtrs_query,
    ratio_stats_query,
    render_metric,
    render_packet,
    samples_query,
    uom_query,
    winner_samples_query,
    winners_query,
)

# --- the real catalog --------------------------------------------------------------------


def test_real_catalog_loads_and_has_about_twenty_metrics() -> None:
    catalog = load_catalog(CATALOG_PATH)
    assert 18 <= len(catalog) <= 30
    for required in ("revenue", "operating_income", "operating_margin", "total_assets"):
        assert required in catalog


def test_real_catalog_revenue_chain_keeps_contract_order_and_bank_fallback() -> None:
    """05 4.2: the two contract-revenue tags lead the chain. After the M4 catalog review
    (ADR-042, docs/m4/metric_catalog_proposed.patch) tax-inclusive and bank revenue are their
    own metrics; before Shekar applies it they are still chain members. Either state passes;
    a mix (moved out but still in the chain) fails."""
    cat = load_catalog(CATALOG_PATH)
    rev = cat["revenue"]
    assert rev.tags is not None
    assert rev.tags[:2] == ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"]
    if "bank_revenue_net_of_interest_expense" in cat:
        assert rev.tags == rev.tags[:2]
        assert cat["bank_revenue_net_of_interest_expense"].tags == ["RevenuesNetOfInterestExpense"]
    else:
        assert rev.tags == [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "RevenuesNetOfInterestExpense",
        ]


def test_real_catalog_derived_metrics_are_ratios_over_catalog_keys() -> None:
    catalog = load_catalog(CATALOG_PATH)
    for name in ("gross_margin", "operating_margin", "net_margin"):
        m = catalog[name]
        assert m.unit == "ratio" and m.formula is not None and m.tags is None
        assert formula_refs(m.formula) <= catalog.keys()


def test_real_catalog_instant_metrics_are_balance_sheet() -> None:
    catalog = load_catalog(CATALOG_PATH)
    instants = {n for n, m in catalog.items() if m.period_type == "instant"}
    assert {"total_assets", "cash", "stockholders_equity"} <= instants
    assert "revenue" not in instants


# --- MetricDef validation ----------------------------------------------------------------


def test_tags_xor_formula() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        MetricDef(unit="USD", period_type="duration", tags=["Revenues"], formula="a / b")
    with pytest.raises(ValidationError, match="exactly one"):
        MetricDef(unit="USD", period_type="duration")


def test_unknown_unit_rejected() -> None:
    with pytest.raises(ValidationError):
        MetricDef(unit="EUR", period_type="duration", tags=["Revenues"])  # type: ignore[arg-type]


def test_tag_metric_needs_period_type_and_nonempty_unique_chain() -> None:
    with pytest.raises(ValidationError, match="period_type"):
        MetricDef(unit="USD", tags=["Revenues"])
    with pytest.raises(ValidationError, match="empty"):
        MetricDef(unit="USD", period_type="duration", tags=[])
    with pytest.raises(ValidationError, match="duplicate"):
        MetricDef(unit="USD", period_type="duration", tags=["Revenues", "Revenues"])


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        MetricDef.model_validate(
            {"unit": "USD", "period_type": "duration", "tags": ["Assets"], "tag": "x"}
        )


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "cat.yaml"
    p.write_text(text)
    return p


def test_formula_must_reference_catalog_keys(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "revenue: {unit: USD, period_type: duration, tags: [Revenues]}\n"
        "margin: {unit: ratio, formula: operating_income / revenue}\n",
    )
    with pytest.raises(ValueError, match="operating_income"):
        load_catalog(path)


def test_fallback_formula_is_checked_too(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "gp: {unit: USD, period_type: duration, tags: [GrossProfit],"
        " fallback_formula: rev - cogs}\n",
    )
    with pytest.raises(ValueError, match="unknown"):
        load_catalog(path)


def test_self_referencing_formula_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "x: {unit: ratio, formula: x / 2}\n")
    with pytest.raises(ValueError, match="itself"):
        load_catalog(path)


@pytest.mark.parametrize("bad", ["revenue ** 2", "revenue; drop", "revenue % 3", "f(revenue)x$"])
def test_formula_grammar_rejects_other_tokens(bad: str) -> None:
    with pytest.raises(ValueError, match="bad token"):
        formula_refs(bad)


def test_formula_refs_parses_names_numbers_and_parens() -> None:
    assert formula_refs("(gross_profit - 0.5) / revenue") == {"gross_profit", "revenue"}


def test_empty_catalog_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        load_catalog(_write(tmp_path, ""))


def test_real_catalog_chains_are_concept_pure() -> None:
    """The scope-changing fallbacks dropped after review stay out of the chains."""
    catalog = load_catalog(CATALOG_PATH)
    dropped = {
        "ProfitLoss",
        "LongTermDebt",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "PaymentsOfDividends",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        "InterestExpenseNonoperating",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "EntityCommonStockSharesOutstanding",
    }
    used = {t for m in catalog.values() for t in (m.tags or [])}
    assert not used & dropped
    assert "EBIT" not in catalog["operating_income"].synonyms
    assert "book value" not in catalog["stockholders_equity"].synonyms


# --- verified allowlist ------------------------------------------------------------------


CAT = {
    "revenue": MetricDef(unit="USD", period_type="duration", tags=["Revenues"]),
    "cost_of_revenue": MetricDef(unit="USD", period_type="duration", tags=["CostOfRevenue"]),
    "gross_profit": MetricDef(
        unit="USD",
        period_type="duration",
        tags=["GrossProfit"],
        fallback_formula="revenue - cost_of_revenue",
    ),
    "gross_margin": MetricDef(unit="ratio", formula="gross_profit / revenue"),
}


def _allow(tmp_path: Path, *names: str, extra: str = "") -> Path:
    p = tmp_path / "v.txt"
    p.write_text(
        "# header\n\n"
        + "".join(f"{n}  sha={definition_sha(CAT[n])}  # ok\n" for n in names)
        + extra
    )
    return p


def test_shipped_allowlist_parses() -> None:
    """The shipped file is valid (whatever Shekar has listed in it)."""
    load_verified_metrics(Path("eval/gen/numeric_metrics_verified.txt"))


def test_allowlist_reads_name_sha_lines_and_ignores_comments(tmp_path: Path) -> None:
    assert load_verified_metrics(_allow(tmp_path, "revenue"), CAT) == {"revenue"}


def test_allowlist_rejects_lines_without_sha(tmp_path: Path) -> None:
    p = tmp_path / "v.txt"
    p.write_text("revenue\n")
    with pytest.raises(ValueError, match="sha="):
        load_verified_metrics(p, CAT)


def test_allowlist_rejects_names_outside_the_catalog(tmp_path: Path) -> None:
    p = tmp_path / "v.txt"
    p.write_text("ebitda  sha=0123456789ab\n")
    with pytest.raises(ValueError, match="ebitda"):
        load_verified_metrics(p, CAT)


def test_allowlist_fails_when_the_chain_changed_after_verification(tmp_path: Path) -> None:
    path = _allow(tmp_path, "revenue")
    changed = dict(CAT)
    changed["revenue"] = MetricDef(
        unit="USD", period_type="duration", tags=["Revenues", "SalesRevenueNet"]
    )
    with pytest.raises(ValueError, match="review the chain again"):
        load_verified_metrics(path, changed)


def test_sha_ignores_label_and_synonyms_but_not_tag_order() -> None:
    a = MetricDef(unit="USD", period_type="duration", tags=["A", "B"], label="x", synonyms=["s"])
    b = MetricDef(unit="USD", period_type="duration", tags=["A", "B"], label="y")
    c = MetricDef(unit="USD", period_type="duration", tags=["B", "A"])
    assert definition_sha(a) == definition_sha(b) != definition_sha(c)
    assert len(definition_sha(a)) == 12


def test_derived_metric_needs_its_inputs_verified(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inputs"):
        load_verified_metrics(_allow(tmp_path, "gross_margin", "revenue"), CAT)


def test_fallback_formula_inputs_must_be_verified_too(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cost_of_revenue"):
        load_verified_metrics(_allow(tmp_path, "gross_profit", "revenue"), CAT)
    ok = _allow(tmp_path, "gross_margin", "gross_profit", "revenue", "cost_of_revenue")
    assert load_verified_metrics(ok, CAT) == set(CAT)


# --- SQL builders: shape ------------------------------------------------------------------


def _placeholders(sql: str) -> int:
    return sql.count("%s")


ALL_QUERIES = [
    coverage_query("Revenues"),
    winners_query(["A", "B"]),
    samples_query("Revenues"),
    winner_samples_query(["A", "B"], 1),
    winner_samples_query(["A", "B"], 0),
    overlap_query("A", "B"),
    conflict_count_query("A", "B"),
    conflict_examples_query("A", "B"),
    uom_query("Revenues"),
    qtrs_query("Revenues"),
    fallback_fires_query(["GrossProfit"], [["Revenues"], ["CostOfRevenue"]]),
    ratio_stats_query(["GrossProfit"], ["Revenues"]),
]


@pytest.mark.parametrize("query", ALL_QUERIES)
def test_every_builder_is_parameterized_read_only_and_original_only(
    query: tuple[str, list[Any]],
) -> None:
    sql, params = query
    assert _placeholders(sql) == len(params)
    assert "Revenues" not in sql and "'A'" not in sql
    assert sql.lstrip().split(None, 1)[0].upper() in {"SELECT", "WITH"}
    assert not any(w in sql.upper() for w in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER"))
    assert "is_original" in sql  # gold is the as-originally-reported value (ADR-017)


# --- SQL executed against DuckDB -----------------------------------------------------------

FACTS = [
    # fact_id, cik, tag, uom, qtrs, period_end, fy, fp, value
    # cik 1: has both A and B for FY2022 at the same period, values differ by 50%
    (1, 1, "A", "USD", 4, "2022-12-31", 2022, "FY", 100),
    (2, 1, "B", "USD", 4, "2022-12-31", 2022, "FY", 150),
    # cik 2: only B (B wins), FY2022 and FY2021
    (3, 2, "B", "USD", 4, "2022-12-31", 2022, "FY", 70),
    (4, 2, "B", "USD", 4, "2021-12-31", 2021, "FY", 60),
    # cik 3: A and C report FY2022 at different period_end (non-comparable)
    (5, 3, "A", "USD", 4, "2022-12-31", 2022, "FY", 10),
    (6, 3, "C", "USD", 0, "2023-02-15", 2022, "FY", 11),
    # cik 4: GrossProfit missing, Revenues + CostOfRevenue present (fallback fires)
    (7, 4, "Revenues", "USD", 4, "2022-12-31", 2022, "FY", 200),
    (8, 4, "CostOfRevenue", "USD", 4, "2022-12-31", 2022, "FY", 120),
    # cik 5: GrossProfit present
    (9, 5, "Revenues", "USD", 4, "2022-12-31", 2022, "FY", 100),
    (10, 5, "CostOfRevenue", "USD", 4, "2022-12-31", 2022, "FY", 60),
    (11, 5, "GrossProfit", "USD", 4, "2022-12-31", 2022, "FY", 40),
    # a quarterly A fact for cik 1 (non-FY)
    (12, 1, "A", "USD", 1, "2022-03-31", 2022, "Q1", 25),
]


@pytest.fixture
def fetch() -> Any:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE facts (fact_id BIGINT, cik INT, tag TEXT, uom TEXT, qtrs SMALLINT, "
        "period_end DATE, fiscal_year SMALLINT, fiscal_period TEXT, value DECIMAL(28,4), "
        "is_original BOOLEAN)"
    )
    con.executemany("INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, true)", FACTS)
    con.execute("CREATE TABLE companies (cik INT, name TEXT)")
    con.executemany(
        "INSERT INTO companies VALUES (?, ?)",
        [(1, "One"), (2, "Two"), (3, "Three"), (4, "Four"), (5, "Five")],
    )

    def run(sql: str, params: Any) -> list[tuple[Any, ...]]:
        return con.execute(sql.replace("%s", "?"), list(params)).fetchall()

    return run


def test_winners_follow_chain_order(fetch: Any) -> None:
    rows = {r[0]: (r[1], r[2]) for r in fetch(*winners_query(["A", "B"]))}
    # A wins cik1 FY2022, cik1 Q1 2022, cik3 FY2022; B wins cik2 FY2022 + FY2021
    assert rows == {"A": (2, 3), "B": (1, 2)}


def test_winner_samples_show_only_rows_where_the_fallback_wins(fetch: Any) -> None:
    rows = fetch(*winner_samples_query(["A", "B"], 1))
    assert [r[0] for r in rows] == ["Two"]  # cik 1 has A, so its B row is not a winner
    assert rows[0][1] == 2022 and rows[0][5] == 4  # latest year, qtrs shown


def test_samples_are_one_row_per_company(fetch: Any) -> None:
    rows = fetch(*samples_query("B"))
    assert sorted(r[0] for r in rows) == ["One", "Two"]


def test_overlap_and_conflicts(fetch: Any) -> None:
    assert fetch(*overlap_query("A", "B"))[0] == (1, 1)
    assert fetch(*conflict_count_query("A", "B"))[0][0] == 1
    ex = fetch(*conflict_examples_query("A", "B"))
    assert ex[0][0] == "One" and float(ex[0][4]) == 100 and float(ex[0][5]) == 150
    # A and C share a key but never the same period
    assert fetch(*overlap_query("A", "C"))[0] == (0, 1)


def test_qtrs_distribution_and_flags(fetch: Any) -> None:
    rows = fetch(*qtrs_query("A"))
    assert (True, 4, 2) in rows and (False, 1, 1) in rows
    assert period_type_flags("duration", rows) == []
    assert period_type_flags("instant", rows)  # instant with qtrs 4 is flagged
    c_rows = fetch(*qtrs_query("C"))
    assert period_type_flags("duration", c_rows) == [
        "FLAG: duration metric but 1 FY facts have qtrs=0"
    ]


def test_fallback_fire_count(fetch: Any) -> None:
    q = fallback_fires_query(["GrossProfit"], [["Revenues"], ["CostOfRevenue"]])
    assert fetch(*q)[0][0] == 1  # cik 4 only


def test_ratio_stats(fetch: Any) -> None:
    lo, med, hi, n = fetch(*ratio_stats_query(["CostOfRevenue"], ["Revenues"]))[0]
    assert n == 2 and lo == pytest.approx(0.6) and hi == pytest.approx(0.6)
    assert med == pytest.approx(0.6)


TWO_TAG = MetricDef(label="X", unit="USD", period_type="duration", tags=["A", "B"])


def test_render_metric_against_duckdb(fetch: Any) -> None:
    out = render_metric("x", TWO_TAG, fetch)
    assert "## x" in out and "A -> B" in out
    assert "| 1 | `A` | 2 | 3 | 2 | 3 |" in out
    assert "| 2 | `B` | 2 | 3 | 1 | 2 |" in out
    assert "Rows where `B` WINS" in out and "| Two | 2022 |" in out
    assert "conflicts `A` vs `B` (>1%): 1 of 1 same-period pairs" in out
    assert f"x  sha={definition_sha(TWO_TAG)}" in out
    assert "definition: look up in the us-gaap taxonomy" in out


def test_render_metric_prints_non_comparable_pairs(fetch: Any) -> None:
    m = MetricDef(unit="shares", period_type="duration", tags=["A", "C"])
    out = render_metric("x", m, fetch)
    assert "NON-COMPARABLE: no same-period overlap (1 (cik, FY, period) keys" in out
    assert "conflicts `A` vs `C`" not in out


def test_render_metric_uses_local_taxonomy_labels(fetch: Any) -> None:
    labels = {"A": {"label": "Alpha", "definition": "The portion ..."}}
    out = render_metric("x", TWO_TAG, fetch, labels)
    assert "label: Alpha; definition: The portion ..." in out


def test_render_fallback_and_ratio_sections(fetch: Any) -> None:
    out_gp = render_metric("gross_profit", CAT["gross_profit"], fetch, catalog=CAT)
    assert "fires for 1 FY (cik, year) keys" in out_gp
    m = MetricDef(unit="ratio", formula="cost_of_revenue / revenue")
    out = render_metric("cogs_ratio", m, fetch, catalog=CAT)
    assert "min 0.6" in out and "n 2" in out


def test_render_packet_covers_every_metric_in_the_real_catalog(fetch: Any) -> None:
    catalog = load_catalog(CATALOG_PATH)
    packet = render_packet(catalog, fetch)
    for name in catalog:
        assert f"## {name}\n" in packet
    assert "numeric_metrics_verified.txt" in packet


def test_cli_errors_on_missing_taxonomy_labels(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cr.main(
            [
                "--dsn",
                "postgresql://unused",
                "--out",
                str(tmp_path / "o.md"),
                "--taxonomy-labels",
                str(tmp_path / "nope.json"),
            ]
        )


def test_cli_requires_a_dsn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cr.DSN_ENV, raising=False)
    with pytest.raises(SystemExit):
        cr.main(["--out", str(tmp_path / "o.md")])


def test_render_shows_review_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-042 fields are visible in the packet; a fallback without only_if says it's unused."""
    monkeypatch.setattr(cr, "_render_tags", lambda *a, **k: [])
    no_fetch: Any = lambda *a: [(0,)]  # noqa: E731
    bare = MetricDef.model_validate(
        {
            "unit": "USD",
            "period_type": "duration",
            "tags": ["GrossProfit"],
            "fallback_formula": "revenue - cost",
        }
    )
    out = render_metric("gross_profit", bare, no_fetch)
    assert "fallback only_if: NONE (the generator never uses it)" in out
    guarded = bare.model_copy(update={"only_if": ["same_entity"], "fallback_result": "derived"})
    assert "fallback only_if: same_entity; result: derived" in render_metric(
        "gp", guarded, no_fetch
    )
    multi = MetricDef.model_validate(
        {
            "unit": "USD",
            "period_type": "duration",
            "tags": ["A", "B"],
            "presentation_basis": "net_of_interest_expense",
        }
    )
    out = render_metric("bank", multi, no_fetch)
    assert "presentation_basis: net_of_interest_expense" in out and "multi-tag" in out


def test_empty_only_if_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        MetricDef.model_validate(
            {
                "unit": "USD",
                "period_type": "duration",
                "tags": ["A"],
                "fallback_formula": "a - b",
                "only_if": [],
            }
        )
