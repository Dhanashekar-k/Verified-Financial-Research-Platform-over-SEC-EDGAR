"""The two read-only M5 scripts that run against the serving database, on a real Postgres 17
loaded with the compiler's fixture facts (156 real rows, five companies)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from app.engine.plan.catalog import load_catalog
from eval.coverage_report import by_metric, coverage, filers_by_year
from eval.gold_form_check import check
from tests.integration.test_compiler_pg import (
    CIKS,
    DT,
    RAW,
    TI,
    TI_NI_FY22,
    load_into,
)

pytest.importorskip("testcontainers")
psycopg = pytest.importorskip("psycopg")


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # older testcontainers
        from testcontainers.postgres import PostgresContainer  # type: ignore[no-redef]
    with PostgresContainer("pgvector/pgvector:pg17", driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="module")
def conn(pg_url: str) -> Iterator[Any]:
    ttm = dict(next(r for r in RAW if r["fact_id"] == TI_NI_FY22))
    ttm.update(fact_id=8_800_000_001, fact_key=8_800_000_001, form="10-Q", cik=9_999_999)
    with psycopg.connect(pg_url, autocommit=True) as c:
        load_into(c, [*RAW, ttm])
        yield c


def gold(rid: str, *ids: int) -> dict[str, Any]:
    return {"id": rid, "gold_fact_ids": list(ids)}


def test_gold_on_10k_facts_is_clean(conn: Any) -> None:
    out = check([gold("a", TI_NI_FY22)], conn)
    assert (out["found"], out["missing"], out["affected_gold_rows"]) == (1, 0, [])


def test_a_gold_row_on_a_10q_full_year_fact_is_flagged_with_its_id(conn: Any) -> None:
    out = check([gold("a", TI_NI_FY22), gold("b", TI_NI_FY22, 8_800_000_001)], conn)
    assert out["affected_gold_rows"] == ["b"]
    assert [f["form"] for f in out["fy_facts_from_a_non_10k_form"]] == ["10-Q"]


def test_an_id_that_is_not_served_is_reported_not_assumed_clean(conn: Any) -> None:
    out = check([gold("a", TI_NI_FY22), gold("c", 123)], conn)
    assert out["missing_fact_ids"] == [123] and out["gold_rows_with_a_missing_id"] == ["c"]


def test_coverage_gives_one_outcome_per_company_and_names_the_reasons(conn: Any) -> None:
    catalog = load_catalog()
    some = {n: catalog[n] for n in ("net_income", "r_and_d", "revenue", "gross_profit")}
    some["cost_of_revenue"] = catalog["cost_of_revenue"]
    filers = filers_by_year(conn, CIKS, min_companies=2)
    assert 2022 in filers and set(filers[2022]) <= set(CIKS) and {TI, DT} <= set(filers[2022])
    cells = coverage(conn, some, filers, log=lambda _: None)
    cell = next(c for c in cells if c["metric"] == "r_and_d" and c["fiscal_year"] == 2022)
    assert cell["found"] + sum(cell["not_found_reasons"].values()) == cell["companies"]
    assert cell["coverage"] == cell["found"] / cell["companies"]
    pooled = {row["metric"]: row for row in by_metric(cells)}
    assert set(pooled) == set(some)
    assert pooled["net_income"]["pairs"] == sum(len(c) for c in filers.values())
    json.dumps(pooled)  # the file must serialize


def test_a_10q_full_year_row_does_not_make_a_company_a_filer(conn: Any) -> None:
    """The synthetic 10-Q row's company is asked about, so the form filter is really tested."""
    filers = filers_by_year(conn, [*CIKS, 9_999_999], min_companies=1, min_year=2000)
    assert all(9_999_999 not in c for c in filers.values())


def test_years_before_the_floor_are_left_out(conn: Any) -> None:
    """Earlier years exist only as comparatives in later 10-Ks."""
    assert min(filers_by_year(conn, CIKS, min_companies=1, min_year=2022)) == 2022


def test_a_year_with_too_few_filers_is_left_out(conn: Any) -> None:
    assert filers_by_year(conn, CIKS, min_companies=99) == {}
