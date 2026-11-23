"""make_numeric_set's facts query and value resolution against a real Postgres 17 (Testcontainers).

The unit tests run the same SQL on DuckDB; this checks the Postgres dialect (``%s::text[]``,
``numeric`` -> Decimal, ``bigint[]`` -> list) on a ``facts`` table shaped like 05 section 3, with
a dimensional twin table that must never be read.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest

from eval.gen import make_numeric_set as mn
from tests.unit.eval.test_make_numeric_set import ALL, FACTS, catalog

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


DDL = """
CREATE TABLE facts (
  fact_id bigint PRIMARY KEY, cik int NOT NULL, tag text NOT NULL, taxonomy text NOT NULL,
  uom text NOT NULL, qtrs smallint NOT NULL, period_start date, period_end date NOT NULL,
  fiscal_year smallint NOT NULL, fiscal_period text NOT NULL, value numeric NOT NULL,
  accession text NOT NULL, form text NOT NULL, filed date NOT NULL, is_latest boolean NOT NULL,
  is_original boolean NOT NULL, is_restated boolean NOT NULL, derived_from bigint[]);
CREATE TABLE facts_dimensional (LIKE facts);
"""


def test_facts_query_and_resolution_on_postgres(pg_url: str) -> None:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(DDL)
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO facts VALUES (" + ",".join(["%s"] * 18) + ")", FACTS)
            # a segment value that would win if the dimensional table leaked in
            cur.execute(
                "INSERT INTO facts_dimensional SELECT fact_id + 100000, cik, tag, taxonomy, uom, "
                "qtrs, period_start, period_end, fiscal_year, fiscal_period, 1, accession, form, "
                "filed, is_latest, is_original, is_restated, derived_from FROM facts"
            )
        cat = catalog()
        sql, params = mn.facts_query(mn.needed_tags(cat, ALL), 2020)
        rows = conn.execute(sql, params).fetchall()
    facts = [mn.fact_from_row(r) for r in rows]
    assert facts and all(f.taxonomy == "us-gaap" and f.fact_id < 100000 for f in facts)
    assert any(f.derived_from == (1, 2) for f in facts)
    table = mn.build_values(cat, ALL, facts)
    v = table.get("revenue", (2, 2023, "FY"))
    assert v is not None and v.value == Decimal(4_000_000_000)
    assert v.resolution == "reconciles_with_gross_profit"
    assert table.get("revenue", (3, 2023, "FY")) is None
