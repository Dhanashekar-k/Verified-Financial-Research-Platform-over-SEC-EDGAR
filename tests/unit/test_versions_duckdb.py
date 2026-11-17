"""DuckDB twin of `tests/integration/test_versions_spark.py`: `select_versions` over a DuckDB table.

The same hand-derived scenarios (`tests/fiscal_cases.py::VERSION_SCENARIOS`) are run through the
DuckDB function and compared with the same expected rows as the pure-Python reference. NULL
semantics follow `select_versions_ref` (Python `==`): a NULL value is a distinct value, NULL ==
NULL collapses latest and original, and a NULL filing_period is never 'native'.

No JVM and no network: an in-memory DuckDB connection per test, explicit column types.
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import duckdb
import pytest

from pipelines.common.fiscal_ref import FactVersion
from pipelines.duckdb.normalize_facts_duckdb import select_versions
from tests.fiscal_cases import VERSION_SCENARIOS, VersionScenario, sorted_version_tuples

VERSION_COLUMNS = [
    ("fact_key", "BIGINT"),
    ("fact_id", "BIGINT"),
    ("period_end", "DATE"),
    ("value", "DECIMAL(28,4)"),
    ("accession", "VARCHAR"),
    ("form", "VARCHAR"),
    ("filed", "DATE"),
    ("is_amendment", "BOOLEAN"),
    ("filing_period", "DATE"),
]
INPUT_COLUMNS = [c for c, _ in VERSION_COLUMNS]


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    """A fresh in-memory DuckDB connection per test."""
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def to_table(
    con: duckdb.DuckDBPyConnection,
    versions: Sequence[FactVersion],
    extras: Sequence[tuple[Any, ...]] | None = None,
    name: str = "versions",
) -> str:
    """Create table `name` with explicit types; `extras[i]` is the pass-through `note` of row i."""
    columns = [*VERSION_COLUMNS, *([("note", "VARCHAR")] if extras is not None else [])]
    con.execute(f"CREATE TABLE {name} ({', '.join(f'{c} {t}' for c, t in columns)})")
    rows = []
    for i, v in enumerate(versions):
        base = tuple(getattr(v, c) for c in INPUT_COLUMNS)
        rows.append(base + (tuple(extras[i]) if extras is not None else ()))
    if rows:
        con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in columns)})", rows)
    return name


def fetch(rel: duckdb.DuckDBPyRelation) -> list[dict[str, Any]]:
    names = list(rel.columns)
    return [dict(zip(names, r, strict=True)) for r in rel.fetchall()]


def as_tuples(
    rows: list[dict[str, Any]],
) -> list[tuple[int, str, Decimal | None, bool, bool, int, bool]]:
    return sorted_version_tuples(
        [
            (
                r["fact_key"],
                r["accession"],
                r["value"],
                r["is_latest"],
                r["is_original"],
                r["n_versions"],
                r["is_restated"],
            )
            for r in rows
        ]
    )


def run(con: duckdb.DuckDBPyConnection, versions: Sequence[FactVersion]) -> list[dict[str, Any]]:
    return fetch(select_versions(con, to_table(con, versions)))


@pytest.mark.parametrize("scenario", VERSION_SCENARIOS, ids=repr)
def test_duckdb_version_selection_matches_hand_derived_rows(
    con: duckdb.DuckDBPyConnection, scenario: VersionScenario
) -> None:
    """Latest/original selection in DuckDB: single version, comparative with the same value, a
    restatement, 10-K then 10-K/A, no filing_period match, a non-match filed earlier, the
    accession tie-breaks on both sides, and 100 -> 90 -> 100. Each yields exactly the expected
    rows with the expected flags, n_versions and is_restated.
    """
    out = run(con, scenario.versions)
    assert as_tuples(out) == sorted_version_tuples([e.as_tuple() for e in scenario.expect])


NULL_SCENARIOS = [
    s for s in VERSION_SCENARIOS if s.name.startswith(("null_", "all_filing_periods_null"))
]


def test_null_scenarios_are_present() -> None:
    """Guard: the NULL-value / NULL-filing_period scenarios exist, so the DuckDB test below can
    never pass vacuously over an empty parameter list.
    """
    assert len(NULL_SCENARIOS) == 5


@pytest.mark.parametrize("scenario", NULL_SCENARIOS, ids=repr)
def test_duckdb_null_value_and_null_filing_period_follow_python_semantics(
    con: duckdb.DuckDBPyConnection, scenario: VersionScenario
) -> None:
    """NULL handling matches the reference: a NULL value is a distinct value for is_restated
    (SQL's count(DISTINCT) ignores NULLs) and NULL == NULL collapses latest/original (SQL `=`
    yields NULL, so a null-safe comparison, IS NOT DISTINCT FROM, is needed); a NULL
    filing_period is never native and does not poison the original selection. Rows keep their
    NULL value through the selection.
    """
    out = run(con, scenario.versions)
    assert as_tuples(out) == sorted_version_tuples([e.as_tuple() for e in scenario.expect])


@pytest.mark.parametrize("seed", [None, 1, 2, 3])
def test_duckdb_independent_fact_keys_and_input_order(
    con: duckdb.DuckDBPyConnection, seed: int | None
) -> None:
    """All scenarios in one table (each with its own fact_key): the output is the union of the
    per-scenario outputs whatever the row order (reversed, three shuffles). Window or aggregate
    code that depended on insertion order would fail here.
    """
    versions = [v for s in VERSION_SCENARIOS for v in s.versions]
    expected = sorted_version_tuples([e.as_tuple() for s in VERSION_SCENARIOS for e in s.expect])
    if seed is None:
        versions = versions[::-1]
    else:
        random.Random(seed).shuffle(versions)
    assert as_tuples(run(con, versions)) == expected


def test_duckdb_output_schema_and_pass_through_columns(con: duckdb.DuckDBPyConnection) -> None:
    """The output has every input column with its input type (value stays DECIMAL(28,4), so the
    Parquet write needs no cast), the pass-through `note` with its value intact on the selected
    rows, plus is_latest/is_original (BOOLEAN), n_versions (INTEGER), is_restated (BOOLEAN), in
    that order after the input columns.
    """
    sc = next(s for s in VERSION_SCENARIOS if s.name == "restated_100_to_90")
    notes = [(f"note-{v.accession}",) for v in sc.versions]
    rel = select_versions(con, to_table(con, sc.versions, notes))
    types = dict(zip(rel.columns, (str(t) for t in rel.types), strict=True))
    assert list(rel.columns) == [
        *INPUT_COLUMNS,
        "note",
        "is_latest",
        "is_original",
        "n_versions",
        "is_restated",
    ]
    for col, typ in VERSION_COLUMNS:
        assert types[col] == typ, col
    assert types["note"] == "VARCHAR"
    assert types["is_latest"] == "BOOLEAN"
    assert types["is_original"] == "BOOLEAN"
    assert types["n_versions"] == "INTEGER"
    assert types["is_restated"] == "BOOLEAN"
    assert {r["accession"]: r["note"] for r in fetch(rel)} == {
        v.accession: f"note-{v.accession}" for v in sc.versions
    }


def test_duckdb_output_rows_are_selected_input_rows(con: duckdb.DuckDBPyConnection) -> None:
    """Each output row is an input row carried through whole (same fact_id, value, filed, form):
    selecting a version never synthesises or mixes columns from different versions.
    """
    sc = next(s for s in VERSION_SCENARIOS if s.name == "ten_k_then_ten_k_a_with_different_value")
    out = run(con, sc.versions)
    by_id = {v.fact_id: v for v in sc.versions}
    for r in out:
        v = by_id[r["fact_id"]]
        assert (r["accession"], r["value"], r["filed"], r["form"], r["is_amendment"]) == (
            v.accession,
            v.value,
            v.filed,
            v.form,
            v.is_amendment,
        )
        assert (r["period_end"], r["filing_period"]) == (v.period_end, v.filing_period)
    assert date(2023, 4, 20) in {r["filed"] for r in out}


def test_duckdb_empty_input_gives_an_empty_relation_with_the_full_schema(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """No history rows (for example an empty dimensional branch) gives zero rows with the output
    columns and types, so the Parquet file still has the contract schema.
    """
    rel = select_versions(con, to_table(con, []))
    assert list(rel.columns)[-4:] == ["is_latest", "is_original", "n_versions", "is_restated"]
    assert [str(t) for t in rel.types][-4:] == ["BOOLEAN", "BOOLEAN", "INTEGER", "BOOLEAN"]
    assert rel.fetchall() == []


def test_duckdb_accepts_a_view_as_the_source(con: duckdb.DuckDBPyConnection) -> None:
    """`source` is the NAME of a table or a view."""
    sc = next(s for s in VERSION_SCENARIOS if s.name == "restated_100_to_90")
    to_table(con, sc.versions, name="base")
    con.execute("CREATE VIEW versions_view AS SELECT * FROM base")
    out = fetch(select_versions(con, "versions_view"))
    assert as_tuples(out) == sorted_version_tuples([e.as_tuple() for e in sc.expect])


def test_duckdb_does_not_modify_the_source_table(con: duckdb.DuckDBPyConnection) -> None:
    """The function returns a relation; the source keeps its columns and all of its rows (the
    non-selected versions are dropped from the OUTPUT only).
    """
    sc = next(s for s in VERSION_SCENARIOS if s.name == "latest_tie_on_filed_takes_max_accession")
    name = to_table(con, sc.versions)
    fetch(select_versions(con, name))
    assert [r[0] for r in con.execute(f"DESCRIBE {name}").fetchall()] == INPUT_COLUMNS
    assert con.execute(f"SELECT count(*) FROM {name}").fetchone() == (len(sc.versions),)


def test_duckdb_same_fact_key_is_grouped_even_with_other_columns_differing(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Groups are by fact_key alone: versions of one key that differ in form, is_amendment and
    fact_id (all of them do) are still one group, so n_versions counts all of them. A grouping
    that also used another column would report n_versions 1 for each.
    """
    sc = next(s for s in VERSION_SCENARIOS if s.name == "ten_k_then_ten_k_a_with_different_value")
    assert {v.form for v in sc.versions} == {"10-K", "10-K/A"}
    out = run(con, sc.versions)
    assert {r["n_versions"] for r in out} == {2}


# --------------------------------------------------------------------------------------
# A derived Q4 that takes its FY parent's accession can share (fact_key, accession, fact_id)
# with a real quarter row. The versions step must still be deterministic: the real row wins.
# --------------------------------------------------------------------------------------

COLLISION_COLUMNS = (
    "fact_key BIGINT, fact_id BIGINT, period_end DATE, value DECIMAL(28,4), accession VARCHAR, "
    "form VARCHAR, filed DATE, is_amendment BOOLEAN, filing_period DATE, derived_from BIGINT[]"
)
REAL_Q3 = (1, 77, date(2023, 9, 30), Decimal("12131.0000"), "0001-23-000018", "10-Q",
           date(2023, 10, 27), False, date(2023, 9, 30), None)  # fmt: skip
DERIVED_AS_Q4 = (1, 77, date(2023, 9, 30), Decimal("12685.0000"), "0001-23-000018", "10-Q",
                 date(2023, 10, 27), False, date(2023, 9, 30), [8, 9])  # fmt: skip


@pytest.mark.parametrize("derived_value", ["100.0000", "12685.0000"])  # lower and higher
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_a_derived_row_sharing_the_identity_of_a_real_row_never_displaces_it(
    con: duckdb.DuckDBPyConnection, threads: int, order: tuple[int, int], derived_value: str
):
    con.execute(f"SET threads = {threads}")
    con.execute(f"CREATE TABLE v ({COLLISION_COLUMNS})")
    derived = (*DERIVED_AS_Q4[:3], Decimal(derived_value), *DERIVED_AS_Q4[4:])
    rows = [REAL_Q3, derived]
    con.executemany("INSERT INTO v VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [rows[i] for i in order])

    out = fetch(select_versions(con, "v"))

    assert len(out) == 1
    (row,) = out
    assert row["value"] == Decimal("12131.0000") and row["derived_from"] is None
    assert (row["is_latest"], row["is_original"], row["n_versions"]) == (True, True, 2)


def test_the_choice_between_identical_keys_is_repeatable_across_many_shuffles(
    con: duckdb.DuckDBPyConnection,
):
    con.execute(f"CREATE TABLE v ({COLLISION_COLUMNS})")
    rng = random.Random(3)
    seen = set()
    for _ in range(25):
        con.execute("DELETE FROM v")
        rows = [REAL_Q3, DERIVED_AS_Q4] * 3
        rng.shuffle(rows)
        con.executemany("INSERT INTO v VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        seen.add(tuple((r["value"], r["n_versions"]) for r in fetch(select_versions(con, "v"))))

    assert len(seen) == 1


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_rows_tied_on_every_key_resolve_to_the_non_null_value_first(
    con: duckdb.DuckDBPyConnection, order: tuple[int, int]
):
    """NULLS LAST in the tie-break, the same as the Spark twin: a NULL value never wins a tie."""
    con.execute(f"CREATE TABLE v ({COLLISION_COLUMNS})")
    with_null = (*REAL_Q3[:3], None, *REAL_Q3[4:])
    rows = [REAL_Q3, with_null]
    con.executemany("INSERT INTO v VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [rows[i] for i in order])

    out = fetch(select_versions(con, "v"))

    assert [r["value"] for r in out if r["is_latest"]] == [Decimal("12131.0000")]
