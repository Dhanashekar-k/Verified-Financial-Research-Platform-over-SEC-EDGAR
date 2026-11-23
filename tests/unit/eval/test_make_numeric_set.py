"""Numeric generator (M4 3b) on a DuckDB fixture ``facts`` / ``companies`` (the generator's SQL
is portable; the ``facts`` view shape is 05 section 3). No network: companyfacts is a dict, the
LLM is not called (paraphrase guard tested directly)."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from eval.gen import make_numeric_set as mn
from eval.gen.catalog_review import (
    CATALOG_PATH,
    MetricDef,
    definition_sha,
    load_catalog,
)
from eval.gen.numeric_policy import Fact
from eval.schemas import NumericRow

REV0 = "RevenueFromContractWithCustomerExcludingAssessedTax"

CATALOG: dict[str, Any] = {
    "revenue": {
        "label": "Revenue",
        "unit": "USD",
        "period_type": "duration",
        "tags": [REV0, "Revenues"],
    },
    "cost_of_revenue": {
        "label": "Cost of revenue",
        "unit": "USD",
        "period_type": "duration",
        "tags": ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
    },
    "gross_profit": {
        "label": "Gross profit",
        "unit": "USD",
        "period_type": "duration",
        "tags": ["GrossProfit"],
        "fallback_formula": "revenue - cost_of_revenue",
        "only_if": ["same_entity", "same_period", "same_currency", "same_scope", "inputs_verified"],
        "fallback_result": "derived",
    },
    "net_income": {
        "label": "Net income",
        "unit": "USD",
        "period_type": "duration",
        "tags": ["NetIncomeLoss"],
    },
    "eps_diluted": {
        "label": "Diluted EPS",
        "unit": "USD/shares",
        "period_type": "duration",
        "tags": ["EarningsPerShareDiluted"],
    },
    "total_assets": {
        "label": "Total assets",
        "unit": "USD",
        "period_type": "instant",
        "tags": ["Assets"],
    },
    "gross_margin": {"label": "Gross margin", "unit": "ratio", "formula": "gross_profit / revenue"},
    "net_margin": {"label": "Net margin", "unit": "ratio", "formula": "net_income / revenue"},
}

_id = iter(range(1, 10_000))


def row(
    cik: int,
    tag: str,
    fy: int,
    fp: str,
    value: str,
    *,
    qtrs: int | None = None,
    uom: str = "USD",
    taxonomy: str = "us-gaap",
    end: date | None = None,
    is_latest: bool = True,
    is_original: bool = True,
    restated: bool = False,
    accession: str | None = None,
    derived: list[int] | None = None,
) -> tuple[Any, ...]:
    if qtrs is None:
        qtrs = 4 if fp == "FY" else 1
    end = end or (date(fy, 12, 31) if fp == "FY" else date(fy, 3 * int(fp[1]), 28))
    start = None if qtrs == 0 else date(end.year - (1 if fp == "FY" else 0), 1, 1)
    acc = accession or f"00000000{cik:02d}-{fy % 100 + 1:02d}-000001"
    return (
        next(_id),
        cik,
        tag,
        taxonomy,
        uom,
        qtrs,
        start,
        end,
        fy,
        fp,
        Decimal(value),
        acc,
        "10-K" if fp == "FY" else "10-Q",
        date(fy + 1, 2, 1),
        is_latest,
        is_original,
        restated,
        derived,
    )


FACTS = [
    # company 1: clean; both revenue tags agree; no GrossProfit tag -> derived gross profit
    row(1, REV0, 2022, "FY", "10000000000"),
    row(1, "Revenues", 2022, "FY", "10000000000"),
    row(1, REV0, 2023, "FY", "12000000000"),
    row(1, "Revenues", 2023, "FY", "12000000000"),
    row(1, "CostOfRevenue", 2022, "FY", "6000000000"),
    row(1, "CostOfRevenue", 2023, "FY", "7000000000"),
    row(1, "NetIncomeLoss", 2022, "FY", "1000000000", is_latest=False, restated=True),
    row(
        1,
        "NetIncomeLoss",
        2022,
        "FY",
        "900000000",
        is_original=False,
        restated=True,
        accession="0000000001-24-000009",
    ),
    row(1, "NetIncomeLoss", 2023, "FY", "1500000000"),
    row(1, "EarningsPerShareDiluted", 2023, "FY", "2.5000", uom="USD/shares"),
    row(1, "Assets", 2023, "FY", "50000000000", qtrs=0),
    row(1, REV0, 2023, "Q1", "3000000000"),
    row(1, REV0, 2023, "Q2", "6100000000", qtrs=2),  # year-to-date H1 labelled Q2: excluded
    row(1, REV0, 2023, "Q4", "3200000000", derived=[1, 2]),  # derived Q4: excluded by default
    # company 2: revenue tags conflict; gross-profit identity picks REV0 (4.0 - 3.0 = 1.0)
    row(2, REV0, 2023, "FY", "4000000000"),
    row(2, "Revenues", 2023, "FY", "4400000000"),
    row(2, "CostOfRevenue", 2023, "FY", "3000000000"),
    row(2, "GrossProfit", 2023, "FY", "1000000000"),
    row(2, "EarningsPerShareDiluted", 2023, "FY", "1.2000", uom="USD"),  # uom mismatch
    row(2, "NetIncomeLoss", 2023, "FY", "300000000"),
    # company 3: tax-inclusive-sized conflict with no evidence -> abstain; qtrs=0 on duration
    row(3, REV0, 2023, "FY", "29600000000"),
    row(3, "Revenues", 2023, "FY", "79800000000"),
    row(3, "NetIncomeLoss", 2023, "FY", "5000000000", qtrs=0),
    row(3, "Revenues", 2023, "FY", "1", taxonomy="custom"),
    # company 4: near-zero revenue -> margins undefined
    row(4, REV0, 2023, "FY", "500000"),
    row(4, "NetIncomeLoss", 2023, "FY", "-7000000"),
    row(4, REV0, 2024, "FY", "9000000"),  # growth over a near-zero prior year: undefined
]
COMPANIES = [
    (1, "ALPHA CORP", "ALP", 3571),
    (2, "BETA INC", "BET", 3572),
    (3, "GAMMA HOLDINGS", None, 2111),
    (4, "DELTA BIO", "DLT", 2834),
]


@pytest.fixture()
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect()
    c.execute(
        "CREATE TABLE facts (fact_id BIGINT, cik INT, tag TEXT, taxonomy TEXT, uom TEXT, "
        "qtrs SMALLINT, period_start DATE, period_end DATE, fiscal_year SMALLINT, "
        "fiscal_period TEXT, value DECIMAL(28,4), accession TEXT, form TEXT, filed DATE, "
        "is_latest BOOLEAN, is_original BOOLEAN, is_restated BOOLEAN, derived_from BIGINT[])"
    )
    c.executemany("INSERT INTO facts VALUES (" + ",".join("?" * 18) + ")", FACTS)
    c.execute("CREATE TABLE companies (cik INT, name TEXT, ticker TEXT, sic INT)")
    c.executemany("INSERT INTO companies VALUES (?, ?, ?, ?)", COMPANIES)
    return c


def catalog(**changes: Any) -> dict[str, MetricDef]:
    raw = json.loads(json.dumps(CATALOG))
    for k, v in changes.items():
        if v is None:
            raw.pop(k)
        else:
            raw[k] = v
    return {k: MetricDef.model_validate(v) for k, v in raw.items()}


def facts_of(
    con: duckdb.DuckDBPyConnection, cat: dict[str, MetricDef], verified: set[str]
) -> list[Fact]:
    sql, params = mn.facts_query(mn.needed_tags(cat, verified), 2020)
    return [mn.fact_from_row(r) for r in con.execute(sql.replace("%s", "?"), params).fetchall()]


ALL = {
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "net_income",
    "eps_diluted",
    "total_assets",
    "gross_margin",
    "net_margin",
}


@pytest.fixture()
def table(con: duckdb.DuckDBPyConnection) -> mn.ValueTable:
    cat = catalog()
    return mn.build_values(cat, ALL, facts_of(con, cat, ALL))


# --- query --------------------------------------------------------------------------------------


def test_facts_query_reads_us_gaap_facts_only(con: duckdb.DuckDBPyConnection) -> None:
    cat = catalog()
    facts = facts_of(con, cat, ALL)
    assert facts and all(f.taxonomy == "us-gaap" for f in facts)
    assert "facts_dimensional" not in mn.facts_query(["Revenues"], 2020)[0]


def test_needed_tags_include_reconciliation_evidence() -> None:
    tags = mn.needed_tags(catalog(), {"revenue"})
    assert {"GrossProfit", "CostOfRevenue", REV0, "Revenues"} <= set(tags)


# --- resolution, quarantine, abstention ---------------------------------------------------------


def test_agreeing_revenue_cites_preferred_tag(table: mn.ValueTable) -> None:
    v = table.get("revenue", (1, 2023, "FY"))
    assert v is not None and v.value == Decimal(12_000_000_000)
    assert v.tags == [REV0] and v.resolution == "candidates_agree"


def test_conflict_resolved_by_gross_profit_identity(table: mn.ValueTable) -> None:
    v = table.get("revenue", (2, 2023, "FY"))
    assert v is not None and v.value == Decimal(4_000_000_000)
    assert v.resolution == "reconciles_with_gross_profit"


def test_unreconciled_conflict_abstains_with_candidates(table: mn.ValueTable) -> None:
    assert table.get("revenue", (3, 2023, "FY")) is None
    (a,) = [a for a in table.abstentions if a.metric == "revenue" and a.cik == 3]
    assert a.reason == "conflicting_candidates" and len(a.candidates) == 2
    assert any(k == (3, 2023, "FY") for k, _ in table.conflicts)


def test_presentation_evidence_resolves_without_identity(con: duckdb.DuckDBPyConnection) -> None:
    cat = catalog()
    facts = facts_of(con, cat, ALL)
    acc3 = next(f.accession for f in facts if f.cik == 3 and f.tag == REV0)
    t = mn.build_values(cat, ALL, facts, presentation=lambda accs: {acc3: frozenset({"Revenues"})})
    v = t.get("revenue", (3, 2023, "FY"))
    assert v is not None and v.value == Decimal(79_800_000_000)
    assert v.resolution == "presented_on_income_statement" and t.presentation_known >= 1


def test_quarantine_and_exclusions_are_logged(table: mn.ValueTable) -> None:
    reasons = {(q.cik, q.tag, q.reason) for q in table.log.quarantined}
    assert (3, "NetIncomeLoss", "qtrs0_on_duration_metric") in reasons
    assert (2, "EarningsPerShareDiluted", "uom_mismatch") in reasons
    assert table.log.excluded["ytd_or_period_length_mismatch"] == 1
    assert table.log.excluded["derived_q4"] == 1
    assert table.get("revenue", (1, 2023, "Q2")) is None
    assert table.get("revenue", (1, 2023, "Q4")) is None
    assert table.get("revenue", (1, 2023, "Q1")) is not None


def test_restated_value_is_as_filed_with_latest_recorded(table: mn.ValueTable) -> None:
    v = table.get("net_income", (1, 2022, "FY"))
    assert v is not None and v.value == Decimal(1_000_000_000) and v.is_restated
    assert v.inputs[0].latest_value == Decimal(900_000_000)


def test_latest_policy_flips_the_gold(con: duckdb.DuckDBPyConnection) -> None:
    cat = catalog()
    t = mn.build_values(cat, ALL, facts_of(con, cat, ALL), policy="latest_restated")
    v = t.get("net_income", (1, 2022, "FY"))
    assert v is not None and v.value == Decimal(900_000_000)


def test_gross_profit_fallback_is_derived_from_both_inputs(table: mn.ValueTable) -> None:
    v = table.get("gross_profit", (1, 2023, "FY"))
    assert v is not None and v.kind == "derived" and v.value == Decimal(5_000_000_000)
    assert v.tags == [REV0, "CostOfRevenue"] and len(v.fact_ids) == 2
    reported = table.get("gross_profit", (2, 2023, "FY"))
    assert reported is not None and reported.kind == "reported"


def test_ordered_refs_follow_reading_order() -> None:
    assert mn.ordered_refs("(a - b) / c") == ["a", "b", "c"]
    assert mn.ordered_refs("revenue - cost_of_revenue") == ["revenue", "cost_of_revenue"]


def test_fallback_without_only_if_is_never_used(con: duckdb.DuckDBPyConnection) -> None:
    gp = dict(CATALOG["gross_profit"])
    gp.pop("only_if")
    gp.pop("fallback_result")
    cat = catalog(gross_profit=gp)
    t = mn.build_values(cat, ALL, facts_of(con, cat, ALL))
    assert t.get("gross_profit", (1, 2023, "FY")) is None
    assert any(a.reason == "fallback_blocked:no_only_if_declared" for a in t.abstentions)


def test_fallback_blockers_name_each_failed_condition(table: mn.ValueTable) -> None:
    rev = table.get("revenue", (1, 2023, "FY"))
    cost = table.get("cost_of_revenue", (1, 2022, "FY"))
    assert rev is not None and cost is not None
    m = catalog()["gross_profit"]
    blocked = mn.fallback_blockers("gross_profit", m, [rev, cost], {"revenue"})
    assert blocked == ["same_period", "inputs_verified"]


def test_margin_undefined_on_near_zero_revenue_not_clamped(table: mn.ValueTable) -> None:
    m = catalog()["net_margin"]
    val, why = mn.ratio_value("net_margin", m, (4, 2023, "FY"), table)
    assert val is None and why == "near_zero_denominator"
    ok, _ = mn.ratio_value("gross_margin", catalog()["gross_margin"], (1, 2023, "FY"), table)
    assert ok is not None and ok.value == Decimal("0.4166666667")


# --- items, sampling, text ----------------------------------------------------------------------


def _items(table: mn.ValueTable) -> dict[str, list[mn.Item]]:
    sic = {c[0]: c[3] for c in COMPANIES}
    return mn.build_items(catalog(), ALL, table, sic)  # type: ignore[return-value]


def test_items_cover_each_template(table: mn.ValueTable) -> None:
    items = _items(table)
    (g,) = [i for i in items["growth"] if i.metric == "revenue" and i.cik == 1]
    assert g.gold == Decimal("0.2000000000") and g.unit == "ratio"
    assert any(i.metric == "gross_margin" for i in items["ratio"])
    (c,) = [i for i in items["compare"] if i.metric == "revenue"]
    assert c.gold > 0 and c.values[0].value > c.values[1].value


def test_growth_over_near_zero_prior_is_an_abstention(table: mn.ValueTable) -> None:
    items = _items(table)
    assert not [i for i in items["growth"] if i.cik == 4]
    assert any(a.cik == 4 and a.reason == "growth:near_zero_denominator" for a in table.abstentions)


def test_sampling_is_deterministic_and_never_repeats(table: mn.ValueTable) -> None:
    items = _items(table)
    a = mn.sample(items, 12, 13)  # type: ignore[arg-type]
    b = mn.sample(items, 12, 13)  # type: ignore[arg-type]
    assert [i.uid for i in a] == [i.uid for i in b]
    keys = [(i.qtype, i.metric, i.cik, i.fiscal_year, i.fiscal_period) for i in a]
    assert len(keys) == len(set(keys))
    assert any(i.restated for i in a)  # the restated quota is filled first


def test_question_templates(table: mn.ValueTable) -> None:
    names = {c[0]: c[1] for c in COMPANIES}
    items = _items(table)
    ni = next(i for i in items["single_value"] if i.metric == "net_income" and i.restated)
    q, tid = mn.template_question(ni, catalog()["net_income"], names, "as_filed")
    assert q == "What was ALPHA CORP's net income in fiscal year 2022, as originally reported?"
    assert tid == "single_v1"
    ta = next(i for i in items["single_value"] if i.metric == "total_assets")
    q, tid = mn.template_question(ta, catalog()["total_assets"], names, "as_filed")
    assert "at the end of fiscal year 2023" in q and tid == "single_v2"
    q1 = next(i for i in items["single_value"] if i.fiscal_period == "Q1")
    assert (
        "Q1 of fiscal year 2023"
        in mn.template_question(q1, catalog()["revenue"], names, "as_filed")[0]
    )


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("How much did Alpha earn in FY2022, as originally reported?", None),
        ("How much did ALP earn in 2022 as originally reported?", None),
        ("How much did Alpha earn in 2023, as originally reported?", "year_lost:2022"),
        ("What did the company earn in 2022 as originally reported?", "company_lost:1"),
        ("How much did Alpha earn in 2022?", "version_qualifier_lost"),
    ],
)
def test_paraphrase_guard(table: mn.ValueTable, text: str, why: str | None) -> None:
    ni = next(i for i in _items(table)["single_value"] if i.metric == "net_income" and i.restated)
    names = {c[0]: c[1] for c in COMPANIES}
    tickers = {c[0]: c[2] for c in COMPANIES}
    assert mn.paraphrase_drift(ni, names, tickers, text) == why


def test_guard_ignores_leading_the_and_initials() -> None:
    assert mn.distinctive_token("The Home Depot, Inc.") == "home"
    assert mn.distinctive_token("A. O. Smith Corp") == "smith"
    v = mn.Value(
        "revenue",
        9,
        2023,
        "FY",
        Decimal(1),
        "USD",
        date(2023, 12, 31),
        "reported",
        (),
        "single_candidate",
    )
    it = mn.Item("single_value", "revenue", 9, 2023, "FY", Decimal(1), "USD", (v,), "retail")
    names, tickers = {9: "The Home Depot, Inc."}, {9: None}
    assert mn.paraphrase_drift(it, names, tickers, "What was the revenue in 2023?") == (
        "company_lost:9"
    )
    assert mn.paraphrase_drift(it, names, tickers, "Home Depot revenue for 2023?") is None
    assert mn.paraphrase_drift(it, names, tickers, "What was Home Depot's 2023 revenue?") is None
    assert mn.distinctive_token("Coca-Cola Co") == "coca"
    assert "coca" in mn._words("Coca Cola's revenue")


def test_json_keeps_decimal_digits_exactly() -> None:
    text = mn.dumps_exact({"v": Decimal("12345678901234567.25"), "r": Decimal("-0.1250")})
    assert text == '{"v": 12345678901234567.25, "r": -0.125}'
    assert json.loads(text, parse_float=Decimal)["v"] == Decimal("12345678901234567.25")


def test_style_is_seeded() -> None:
    assert mn.style_for("num-0001", 13) == mn.style_for("num-0001", 13)
    assert {mn.style_for(f"num-{i:04d}", 13) for i in range(60)} == set(mn.STYLES)


# --- second source -------------------------------------------------------------------------------


def _cf(f: Fact, val: str, accn: str | None = None) -> dict[str, Any]:
    entry = {"accn": accn or f.accession, "end": f.period_end.isoformat(), "val": val}
    if f.period_start and f.qtrs:
        entry["start"] = f.period_start.isoformat()
    return {"facts": {"us-gaap": {f.tag: {"units": {f.uom: [entry]}}}}}


def test_second_source_match_mismatch_and_missing(table: mn.ValueTable) -> None:
    v = table.get("revenue", (1, 2023, "FY"))
    assert v is not None
    f = v.inputs[0].fact
    assert mn.check_fact(f, _cf(f, "12000000000"))[0] == "match"
    res, other = mn.check_fact(f, _cf(f, "12500000000"))
    assert res == "mismatch" and other == Decimal("12500000000")
    assert mn.check_fact(f, _cf(f, "12000000000", accn="other"))[0] == "not_found"
    assert mn.check_fact(f, None)[0] == "not_found"


def test_second_source_per_share_needs_the_exact_value(table: mn.ValueTable) -> None:
    v = table.get("eps_diluted", (1, 2023, "FY"))
    assert v is not None
    f = v.inputs[0].fact
    assert mn.check_fact(f, _cf(f, "2.5"))[0] == "match"
    assert mn.check_fact(f, _cf(f, "2.67"))[0] == "mismatch"  # 17 cents off is not a match


def test_second_source_row_status_and_report(table: mn.ValueTable) -> None:
    g = table.get("gross_profit", (1, 2023, "FY"))
    assert g is not None
    rev, cost = (i.fact for i in g.inputs)
    cf = _cf(rev, "12000000000")
    cf["facts"]["us-gaap"].update(_cf(cost, "7100000000")["facts"]["us-gaap"])
    calls: list[int] = []

    def fetch(cik: int) -> dict[str, Any]:
        calls.append(cik)
        return cf

    status, mism = mn.second_source({"num-0001": [rev, cost], "num-0002": [rev]}, fetch)
    assert status == {"num-0001": "mismatch", "num-0002": "match"}
    assert len(mism) == 1 and mism[0]["tag"] == "CostOfRevenue" and calls == [1]


# --- rows -----------------------------------------------------------------------------------------


def test_rows_validate_and_carry_policy_fields(table: mn.ValueTable) -> None:
    items = _items(table)
    ni = next(i for i in items["single_value"] if i.metric == "net_income" and i.restated)
    r = mn.to_row("num-0001", ni, "q?", "single_v1", False, "as_filed", "match")
    assert r.value_basis == "as_filed" and r.is_restated and r.latest_value == Decimal(900_000_000)
    assert r.tolerance_abs == Decimal("0.5") and r.value_kind == "reported"
    data = json.loads(mn.row_json(r))
    assert data["gold_value"] == 1_000_000_000 and isinstance(data["gold_value"], int)
    assert NumericRow.model_validate(data) == r
    g = next(i for i in items["growth"])
    rg = mn.to_row("num-0002", g, "q?", "growth_v1", False, "as_filed", "unchecked")
    assert rg.value_kind == "derived" and rg.tolerance_abs == Decimal("0.0005")
    assert len(rg.gold_fact_ids) == 2
    c = next(i for i in items["compare"])
    assert (
        mn.to_row("num-0003", c, "q?", "compare_v1", False, "as_filed", "match").compare_cik
        == c.values[1].cik
    )


def test_main_refuses_an_empty_allowlist(tmp_path: Path) -> None:
    allow = tmp_path / "verified.txt"
    allow.write_text("# nothing verified yet\n")
    with pytest.raises(SystemExit, match="no verified metric"):
        mn.main(["--dsn", "postgresql://unused", "--verified", str(allow)])


def test_main_refuses_output_outside_drafts(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="under"):
        mn.main(["--dsn", "x", "--out", "eval/sets/numeric_auto.jsonl"])


# --- catalog fields (ADR-042) ---------------------------------------------------------------------


def test_only_if_needs_a_fallback_formula() -> None:
    with pytest.raises(ValueError, match="fallback_formula"):
        MetricDef.model_validate(
            {"unit": "USD", "period_type": "duration", "tags": ["A"], "only_if": ["same_entity"]}
        )


def test_unknown_only_if_condition_rejected() -> None:
    with pytest.raises(ValueError):
        MetricDef.model_validate(
            {
                "unit": "USD",
                "period_type": "duration",
                "tags": ["A"],
                "fallback_formula": "a - b",
                "only_if": ["close_enough"],
            }
        )


def test_new_fields_do_not_move_existing_shas() -> None:
    """The packet Shekar reviewed printed these; the model extension must not invalidate them."""
    cat = load_catalog(CATALOG_PATH)
    assert definition_sha(cat["total_assets"]) == "40e8f4f0b82d"
    assert definition_sha(cat["net_income"]) == "20843640661f"


def test_new_fields_enter_the_sha_when_set() -> None:
    base = {
        "unit": "USD",
        "period_type": "duration",
        "tags": ["GrossProfit"],
        "fallback_formula": "revenue - cost_of_revenue",
    }
    a = MetricDef.model_validate(base)
    b = MetricDef.model_validate({**base, "only_if": ["same_entity"]})
    assert definition_sha(a) != definition_sha(b)


def test_proposed_catalog_patch_applies_and_loads(tmp_path: Path) -> None:
    """docs/m4/metric_catalog_proposed.patch is what Shekar applies to his catalog."""
    cat = load_catalog(CATALOG_PATH)
    if "bank_revenue_net_of_interest_expense" not in cat:
        target = tmp_path / "app/engine/plan/metric_catalog.yaml"
        target.parent.mkdir(parents=True)
        shutil.copy(CATALOG_PATH, target)
        patch = Path("docs/m4/metric_catalog_proposed.patch").resolve()
        subprocess.run(["git", "apply", str(patch)], cwd=tmp_path, check=True)
        cat = load_catalog(target)
    assert cat["revenue"].tags == [REV0, "Revenues"]
    assert cat["cost_of_revenue"].tags == ["CostOfRevenue", "CostOfGoodsAndServicesSold"]
    assert cat["bank_revenue_net_of_interest_expense"].presentation_basis == (
        "net_of_interest_expense"
    )
    assert cat["revenue_including_assessed_tax"].tags == [
        "RevenueFromContractWithCustomerIncludingAssessedTax"
    ]
    gp = cat["gross_profit"]
    assert gp.fallback_result == "derived" and gp.only_if is not None
    assert set(gp.only_if) == {
        "same_entity",
        "same_period",
        "same_currency",
        "same_scope",
        "inputs_verified",
    }
    assert (cat["long_term_debt"].label or "").lower() == "noncurrent long-term debt"
    all_tags = [t for m in cat.values() for t in (m.tags or [])]
    assert "SalesRevenueNet" not in all_tags and "CostOfGoodsSold" not in all_tags
    assert yaml.safe_load(Path(CATALOG_PATH).read_text())  # the owned file is untouched here
