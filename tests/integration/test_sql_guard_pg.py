"""What the guard's column allow-list rests on, checked against a real Postgres 17.

Postgres resolves ``rel.name`` to ``name(rel)`` when ``rel`` has no column ``name``. The guard
therefore accepts a qualified name only if it is a real column of `facts` or `companies`; that is
safe only while none of those names is a function Postgres can call on a row.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.engine.sql_guard.guard import ALLOWED_COLUMNS

pytest.importorskip("testcontainers")
psycopg = pytest.importorskip("psycopg")


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # older testcontainers
        from testcontainers.postgres import PostgresContainer  # type: ignore[no-redef]
    with PostgresContainer("pgvector/pgvector:pg17", driver=None) as pg:
        with psycopg.connect(pg.get_connection_url(), autocommit=True) as c:
            yield c


def test_attribute_notation_really_calls_a_function(conn: Any) -> None:
    """The bypass the allow-list closes is real."""
    row = conn.execute("SELECT x.to_json FROM (SELECT 1 AS zz) AS x").fetchone()
    assert row[0] == {"zz": 1}


@pytest.mark.parametrize("name", sorted(ALLOWED_COLUMNS))
def test_no_allowed_column_name_is_callable_on_a_row(conn: Any, name: str) -> None:
    with pytest.raises(psycopg.errors.UndefinedColumn):
        conn.execute(f"SELECT x.{name} FROM (SELECT 1 AS zz) AS x")


def test_allowed_columns_are_the_served_columns() -> None:
    """05 section 3: the columns of the `facts` and `companies` views."""
    facts = (
        "fact_id fact_key cik tag taxonomy uom qtrs period_start period_end fiscal_year "
        "fiscal_period calendar_frame value accession form filed is_latest is_original "
        "is_restated anomaly_score derived_from"
    )
    companies = "cik name ticker sic fye_mmdd aliases"
    assert ALLOWED_COLUMNS == set(facts.split()) | set(companies.split())
