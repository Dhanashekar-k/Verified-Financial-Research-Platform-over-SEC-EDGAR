"""DuckDB twin of `tests/integration/test_fiscal_spark.py`: `derive_fiscal` over a DuckDB table.

The same hand-derived scenarios (`tests/fiscal_cases.py`) are run through the DuckDB function and
checked by the same checkers as the pure-Python reference and the Spark version. `derived_from`
ids are resolved back to natural keys by looking them up in the output's own `fact_id` column.
DuckDB's `hash` gives different VALUES from Spark's `xxhash64`, so nothing here compares hash
values with any other engine, only grouping and uniqueness properties.

No JVM and no network: an in-memory DuckDB connection per test. The source tables are built with
explicit DuckDB types (no inference), exactly as `pipelines/duckdb/normalize_facts_duckdb.py`
documents its input.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import duckdb
import pytest
from pipelines.duckdb.normalize_facts_duckdb import derive_fiscal

from pipelines.common.fiscal_ref import FactInput
from tests.fiscal_cases import (
    FISCAL_YEAR_SCENARIOS,
    INPUT_FIELDS,
    K23,
    NO_DERIVE_SCENARIOS,
    OT_K,
    Q3_23,
    Q4_SCENARIOS,
    FiscalScenario,
    Sub,
    check_fiscal_scenario,
    check_no_derivation,
)

FACT_COLUMNS = [
    ("cik", "INTEGER"),
    ("adsh", "VARCHAR"),
    ("tag", "VARCHAR"),
    ("taxonomy", "VARCHAR"),
    ("uom", "VARCHAR"),
    ("qtrs", "INTEGER"),
    ("period_end", "DATE"),
    ("value", "DECIMAL(28,4)"),
    ("form", "VARCHAR"),
    ("filed", "DATE"),
    ("fye_mmdd", "VARCHAR"),
    ("sub_fy", "INTEGER"),
    ("sub_period", "DATE"),
]

EXPECTED_TYPES = {
    "cik": "INTEGER",
    "adsh": "VARCHAR",
    "tag": "VARCHAR",
    "taxonomy": "VARCHAR",
    "uom": "VARCHAR",
    "qtrs": "INTEGER",
    "period_end": "DATE",
    "value": "DECIMAL(28,4)",
    "form": "VARCHAR",
    "filed": "DATE",
    "fye_mmdd": "VARCHAR",
    "sub_fy": "INTEGER",
    "sub_period": "DATE",
    "period_start": "DATE",
    "fiscal_year": "INTEGER",
    "fiscal_period": "VARCHAR",
    "calendar_frame": "VARCHAR",
    "dq_flags": "VARCHAR[]",
    "fact_key": "BIGINT",
    "fact_id": "BIGINT",
    "derived_from": "BIGINT[]",
}


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    """A fresh in-memory DuckDB connection per test."""
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def to_table(
    con: duckdb.DuckDBPyConnection,
    facts: Sequence[FactInput],
    extras: Sequence[tuple[Any, ...]] | None = None,
    extra_columns: Sequence[tuple[str, str]] = (),
    name: str = "src",
) -> str:
    """Create table `name` with the explicit input types; `extras[i]` extends row i."""
    columns = [*FACT_COLUMNS, *extra_columns]
    ddl = ", ".join(f"{c} {t}" for c, t in columns)
    con.execute(f"CREATE TABLE {name} ({ddl})")
    rows = []
    for i, f in enumerate(facts):
        base = tuple(getattr(f, k) for k in INPUT_FIELDS)
        rows.append(base + (tuple(extras[i]) if extras is not None else ()))
    if rows:
        marks = ", ".join("?" for _ in columns)
        con.executemany(f"INSERT INTO {name} VALUES ({marks})", rows)
    return name


def fetch(rel: duckdb.DuckDBPyRelation) -> list[dict[str, Any]]:
    """Execute the relation once and return its rows as dicts."""
    names = list(rel.columns)
    return [dict(zip(names, r, strict=True)) for r in rel.fetchall()]


def normalise(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """dq_flags -> frozenset (and never NULL); `derived_from` ids resolved via the output's own
    `fact_id` to (period_end, qtrs, adsh); fact_id must be unique."""
    by_id = {r["fact_id"]: r for r in raw}
    assert len(by_id) == len(raw), "fact_id must be unique across the output"
    rows = []
    for r in raw:
        assert r["dq_flags"] is not None, "dq_flags must be an empty list, never NULL"
        resolved = None
        if r["derived_from"] is not None:
            resolved = tuple(
                (by_id[i]["period_end"], by_id[i]["qtrs"], by_id[i]["adsh"])
                for i in r["derived_from"]
            )
        rows.append({**r, "dq_flags": frozenset(r["dq_flags"]), "derived_from": resolved})
    return rows


def run(
    con: duckdb.DuckDBPyConnection, facts: Sequence[FactInput], extra_key_cols: Sequence[str] = ()
) -> list[dict[str, Any]]:
    name = to_table(con, facts)
    return normalise(fetch(derive_fiscal(con, name, extra_key_cols)))


@pytest.mark.parametrize("scenario", FISCAL_YEAR_SCENARIOS, ids=repr)
def test_duckdb_fiscal_year_period_and_frame(
    con: duckdb.DuckDBPyConnection, scenario: FiscalScenario
) -> None:
    """Rules 1-4 and 6 in DuckDB: fiscal_year (y_end + per-company label offset), fiscal_period,
    calendar_frame and dq_flags are as hand-derived; input rows pass through unchanged.
    """
    check_fiscal_scenario(scenario, run(con, list(scenario.facts)))


@pytest.mark.parametrize("scenario", Q4_SCENARIOS, ids=repr)
def test_duckdb_derives_q4_as_fy_minus_9m(
    con: duckdb.DuckDBPyConnection, scenario: FiscalScenario
) -> None:
    """Rule 5 in DuckDB: one derived Q4 row per provenance tier (exact 91-day and FSDS-rounded
    92-day pairings, two fiscal years, an offset -1 retailer, a restated FY, equal comparatives, a
    tie between 9M candidates); each row carries its FY parent's form and accession; derived_from
    ids resolve to the retained parents' natural keys (FY first, then 9M); parents survive.
    """
    check_fiscal_scenario(scenario, run(con, list(scenario.facts)))


@pytest.mark.parametrize("scenario", NO_DERIVE_SCENARIOS, ids=repr)
def test_duckdb_does_not_derive_q4_when_it_would_be_wrong(
    con: duckdb.DuckDBPyConnection, scenario: FiscalScenario
) -> None:
    """Rule 5 negatives in DuckDB: per-share, 'pure' and 'shares' uoms, instants, unpaired FY or
    9M, mismatched uom/tag/taxonomy/cik, a 9M from another year, and a direct Q4 in the same
    accession.
    """
    rows = run(con, list(scenario.facts))
    check_no_derivation(rows, len(scenario.facts))
    check_fiscal_scenario(scenario, rows)


def test_duckdb_output_schema_types(con: duckdb.DuckDBPyConnection) -> None:
    """Docstring contract: input columns keep their types (value stays DECIMAL(28,4), so the
    Parquet write needs no cast), the new columns have the documented types, and the output
    column order is the input columns followed by the new ones.
    """
    name = to_table(con, [K23.fact("Revenues", date(2023, 9, 30), 4)])
    rel = derive_fiscal(con, name)
    assert dict(zip(rel.columns, (str(t) for t in rel.types), strict=True)) == EXPECTED_TYPES
    assert list(rel.columns) == list(EXPECTED_TYPES)


def test_duckdb_value_type_stays_decimal_for_a_derived_q4_with_huge_values(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """FY - 9M is computed in exact decimal arithmetic at the full DECIMAL(28,4) width: no float
    rounding and no widening of the column type (DuckDB widens decimal subtraction unless cast).
    """
    fy = Decimal("999999999999999999999999.0001")  # 24 integer digits + 4 decimals = 28 digits
    nm = Decimal("1.0001")
    facts = [
        K23.fact("Revenues", date(2023, 9, 30), 4, fy),
        Q3_23.fact("Revenues", date(2023, 7, 1), 3, nm),
    ]
    name = to_table(con, facts)
    rel = derive_fiscal(con, name)
    types = dict(zip(rel.columns, (str(t) for t in rel.types), strict=True))
    assert types["value"] == "DECIMAL(28,4)"
    derived = [r for r in normalise(fetch(rel)) if r["derived_from"] is not None]
    assert [r["value"] for r in derived] == [Decimal("999999999999999999999998.0000")]


def test_duckdb_dq_flags_is_an_empty_list_not_null(con: duckdb.DuckDBPyConnection) -> None:
    """Docstring: dq_flags is an empty list (not NULL) when there are no flags, so
    `list_contains` and `len` work downstream without NULL handling; derived_from is NULL on a
    non-derived row.
    """
    name = to_table(con, [K23.fact("Revenues", date(2023, 9, 30), 4)])
    (row,) = fetch(derive_fiscal(con, name))
    assert row["dq_flags"] == []
    assert row["derived_from"] is None


def test_duckdb_empty_input_gives_an_empty_relation_with_the_full_schema(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """A run with no facts (for example no dimensional rows in a quarter) must still produce the
    documented columns and types, because the Parquet writer takes its schema from them.
    """
    name = to_table(con, [])
    rel = derive_fiscal(con, name)
    assert dict(zip(rel.columns, (str(t) for t in rel.types), strict=True)) == EXPECTED_TYPES
    assert rel.fetchall() == []


def test_duckdb_accepts_a_view_as_the_source(con: duckdb.DuckDBPyConnection) -> None:
    """`source` is the NAME of a table or a view: the run pipeline passes a view over the joined
    FSDS files, not a materialised table.
    """
    to_table(con, [K23.fact("Revenues", date(2023, 9, 30), 4)], name="base")
    con.execute("CREATE VIEW src_view AS SELECT * FROM base")
    rows = fetch(derive_fiscal(con, "src_view"))
    assert len(rows) == 1
    assert rows[0]["fiscal_period"] == "FY"


def test_duckdb_does_not_modify_the_source_table(con: duckdb.DuckDBPyConnection) -> None:
    """The function returns a relation; it must not add columns or rows to the source table."""
    name = to_table(
        con,
        [
            K23.fact("Revenues", date(2023, 9, 30), 4, "10.0000"),
            Q3_23.fact("Revenues", date(2023, 7, 1), 3, "7.0000"),
        ],
    )
    fetch(derive_fiscal(con, name))
    cols = [r[0] for r in con.execute(f"DESCRIBE {name}").fetchall()]
    assert cols == [c for c, _ in FACT_COLUMNS]
    assert con.execute(f"SELECT count(*) FROM {name}").fetchone() == (2,)


def test_duckdb_is_deterministic_across_calls(con: duckdb.DuckDBPyConnection) -> None:
    """Two calls over the same input give the same fact_key and fact_id for the same rows (the
    hash is a pure function of the key fields), so a rerun of the pipeline is idempotent.
    """
    name = to_table(
        con,
        [
            K23.fact("Revenues", date(2023, 9, 30), 4, "10.0000"),
            Q3_23.fact("Revenues", date(2023, 7, 1), 3, "7.0000"),
        ],
    )

    def ids() -> set[tuple[Any, ...]]:
        return {
            (r["adsh"], r["qtrs"], r["fact_key"], r["fact_id"])
            for r in fetch(derive_fiscal(con, name))
        }

    assert ids() == ids()


def test_duckdb_preserves_pass_through_columns(con: duckdb.DuckDBPyConnection) -> None:
    """Docstring: extra input columns are preserved on every input row."""
    facts = [
        K23.fact("Revenues", date(2023, 9, 30), 4, "5.0000"),
        Q3_23.fact("Revenues", date(2023, 7, 1), 3, "3.0000"),
    ]
    name = to_table(
        con,
        facts,
        [("2023q4", 11), ("2023q3", 22)],
        [("src_quarter", "VARCHAR"), ("batch", "INTEGER")],
    )
    out = fetch(derive_fiscal(con, name))
    assert {"src_quarter", "batch"} <= set(out[0])
    kept = {
        (r["adsh"], r["qtrs"]): (r["src_quarter"], r["batch"])
        for r in out
        if r["derived_from"] is None
    }
    assert kept == {(K23.adsh, 4): ("2023q4", 11), (Q3_23.adsh, 3): ("2023q3", 22)}


def test_duckdb_derived_q4_is_its_fy_parents_row_with_qtrs_and_value_replaced(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Docstring: a derived Q4 row is its FY parent's row carried through whole, pass-through
    columns included, with only qtrs (1) and value (FY - 9M) replaced. The 9M row's pass-through
    values must not leak into it.
    """
    facts = [
        K23.fact("Revenues", date(2023, 9, 30), 4, "10.0000"),
        Q3_23.fact("Revenues", date(2023, 7, 1), 3, "7.0000"),
    ]
    name = to_table(
        con,
        facts,
        [("2023q4", 11), ("2023q3", 22)],
        [("src_quarter", "VARCHAR"), ("batch", "INTEGER")],
    )
    out = fetch(derive_fiscal(con, name))
    (q4,) = [r for r in out if r["derived_from"] is not None]
    assert (q4["src_quarter"], q4["batch"]) == ("2023q4", 11)
    assert (q4["adsh"], q4["form"], q4["filed"]) == (K23.adsh, K23.form, K23.filed)
    assert (q4["qtrs"], q4["value"]) == (1, Decimal("3.0000"))


def test_duckdb_fact_key_groups_versions_and_fact_id_is_unique(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Contract section 2.2: fact_key hashes (cik, tag, taxonomy, period_end, qtrs, uom) and
    nothing else, so the same fact in two accessions shares a fact_key but not a fact_id, and
    changing any one of the six fields gives a different fact_key.
    """
    k22 = Sub(K23.cik, "0000320193-22-000108", "10-K", date(2022, 10, 28), "0930", 2022,
              date(2022, 9, 24))  # fmt: skip
    pe = date(2023, 9, 30)
    facts = [
        K23.fact("Revenues", pe, 4),  # 0 base
        k22.fact("Revenues", pe, 4),  # 1 same natural key, other accession
        K23.fact("OtherTag", pe, 4),  # 2 tag differs
        K23.fact("Revenues", pe, 4, taxonomy="custom"),  # 3 taxonomy differs
        K23.fact("Revenues", pe, 4, uom="EUR"),  # 4 uom differs
        K23.fact("Revenues", pe, 1),  # 5 qtrs differs
        K23.fact("Revenues", date(2023, 9, 29), 4),  # 6 period_end differs
        OT_K.fact("Revenues", pe, 4),  # 7 cik differs
    ]
    rows = run(con, facts)
    by_input = {}
    for r in rows:
        by_input[(r["adsh"], r["cik"], r["tag"], r["taxonomy"], r["uom"], r["qtrs"],
                  r["period_end"])] = r  # fmt: skip

    def find(f: FactInput) -> dict[str, Any]:
        return by_input[(f.adsh, f.cik, f.tag, f.taxonomy, f.uom, f.qtrs, f.period_end)]

    keys = [find(f)["fact_key"] for f in facts]
    ids = [find(f)["fact_id"] for f in facts]
    assert keys[0] == keys[1]
    assert ids[0] != ids[1]
    assert len(set(keys)) == len(facts) - 1  # only 0 and 1 share a key
    assert len(set(ids)) == len(facts)


def test_duckdb_fact_key_does_not_confuse_adjacent_string_fields(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Hashing must keep field boundaries: ('AB', 'C') and ('A', 'BC') for (tag, taxonomy) are
    different series. A key built by naive string concatenation would collide here.
    """
    pe = date(2023, 9, 30)
    facts = [
        K23.fact("AB", pe, 4, taxonomy="C"),
        K23.fact("A", pe, 4, taxonomy="BC"),
    ]
    rows = run(con, facts)
    assert len({r["fact_key"] for r in rows}) == 2


def test_duckdb_derived_q4_has_its_own_fact_key_and_id(con: duckdb.DuckDBPyConnection) -> None:
    """Contract section 2.2: the derived Q4 (qtrs 1) has a fact_key different from the FY fact's
    (qtrs 4), and a fact_id of its own; derived_from points at the FY and 9M fact_ids, in that
    order, and is a two-element BIGINT list.
    """
    name = to_table(
        con,
        [
            K23.fact("Revenues", date(2023, 9, 30), 4, "10.0000"),
            Q3_23.fact("Revenues", date(2023, 7, 1), 3, "7.0000"),
        ],
    )
    raw = fetch(derive_fiscal(con, name))
    fy = next(r for r in raw if r["qtrs"] == 4)
    nine = next(r for r in raw if r["qtrs"] == 3)
    q4 = next(r for r in raw if r["derived_from"] is not None)
    assert q4["fact_key"] not in {fy["fact_key"], nine["fact_key"]}
    assert q4["fact_id"] not in {fy["fact_id"], nine["fact_id"]}
    assert q4["derived_from"] == [fy["fact_id"], nine["fact_id"]]
    assert q4["value"] == Decimal("3.0000")


@pytest.mark.parametrize(
    ("period_end", "qtrs", "expected"),
    [
        (date(2023, 9, 30), 4, date(2022, 10, 1)),  # Sep 30 minus 12 months = 2022-09-30, +1 day
        (date(2023, 6, 30), 1, date(2023, 4, 1)),  # Jun 30 minus 3 months = Mar 31, +1 day
        (date(2022, 12, 31), 2, date(2022, 7, 1)),  # Dec 31 minus 6 months = Jun 30, +1 day
        (date(2023, 7, 1), 3, date(2022, 10, 2)),  # Jul 1 minus 9 months = Oct 1, +1 day
        (date(2024, 3, 31), 1, date(2024, 1, 1)),  # Mar 31 minus 3 months = Dec 31, +1 day
        (date(2024, 2, 29), 4, date(2023, 3, 1)),  # leap day: Feb 29 + 1 = Mar 1, minus 12 months
        (date(2023, 9, 30), 0, None),  # instants have no start
    ],
)
def test_duckdb_period_start_is_period_end_minus_qtrs_quarters_plus_a_day(
    con: duckdb.DuckDBPyConnection, period_end: date, qtrs: int, expected: date | None
) -> None:
    """Docstring: period_start ~ period_end - qtrs quarters + 1 day, NULL for instants. The
    month arithmetic clamps like the reference (add the day first, then subtract whole months).
    """
    (row,) = run(con, [K23.fact("OneOffTag", period_end, qtrs)])
    assert row["period_start"] == expected


def _seg_table(con: duckdb.DuckDBPyConnection, spec: list[tuple[Sub, int, date, str, str]]) -> str:
    """spec items: (sub, qtrs, period_end, value, segments) for tag 'Revenues'."""
    facts = [s.fact("Revenues", pe, q, v) for s, q, pe, v, _ in spec]
    return to_table(con, facts, [(seg,) for *_, seg in spec], [("segments", "VARCHAR")])


def test_duckdb_extra_key_cols_keep_segment_series_apart_in_q4_pairing(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """With extra_key_cols=['segments'], FY and 9M pair only within the same segments value:
    A (100 - 60) and B (200 - 150) each derive their own Q4, and the derived rows carry their
    series' segments value.
    """
    fy, nm = date(2023, 9, 30), date(2023, 7, 1)
    name = _seg_table(
        con,
        [
            (K23, 4, fy, "100.0000", "Seg=A"),
            (K23, 4, fy, "200.0000", "Seg=B"),
            (Q3_23, 3, nm, "60.0000", "Seg=A"),
            (Q3_23, 3, nm, "150.0000", "Seg=B"),
        ],
    )
    out = fetch(derive_fiscal(con, name, ["segments"]))
    derived = [r for r in out if r["derived_from"] is not None]
    assert {r["segments"]: r["value"] for r in derived} == {
        "Seg=A": Decimal("40.0000"),
        "Seg=B": Decimal("50.0000"),
    }
    assert len(out) == 6


def test_duckdb_extra_key_cols_derived_from_points_into_the_same_series(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Each derived Q4's parents are the FY and 9M rows of ITS series, not of the other one:
    resolved through fact_id, the parents carry the same segments value as the derived row.
    """
    fy, nm = date(2023, 9, 30), date(2023, 7, 1)
    name = _seg_table(
        con,
        [
            (K23, 4, fy, "100.0000", "Seg=A"),
            (K23, 4, fy, "200.0000", "Seg=B"),
            (Q3_23, 3, nm, "60.0000", "Seg=A"),
            (Q3_23, 3, nm, "150.0000", "Seg=B"),
        ],
    )
    out = fetch(derive_fiscal(con, name, ["segments"]))
    by_id = {r["fact_id"]: r for r in out}
    assert len(by_id) == len(out), "fact_id must be unique when segments differ"
    for r in out:
        if r["derived_from"] is None:
            continue
        parents = [by_id[i] for i in r["derived_from"]]
        assert [p["qtrs"] for p in parents] == [4, 3]
        assert {p["segments"] for p in parents} == {r["segments"]}


def test_duckdb_extra_key_cols_block_pairing_across_different_segments(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """With extra_key_cols=['segments'], an FY of Seg=A and a 9M of Seg=B are different series:
    nothing is derived.
    """
    name = _seg_table(
        con,
        [
            (K23, 4, date(2023, 9, 30), "100.0000", "Seg=A"),
            (Q3_23, 3, date(2023, 7, 1), "60.0000", "Seg=B"),
        ],
    )
    out = fetch(derive_fiscal(con, name, ["segments"]))
    assert len(out) == 2
    assert all(r["derived_from"] is None for r in out)


def test_duckdb_without_extra_key_cols_segments_do_not_enter_the_key(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Default (consolidated) call: a `segments` pass-through column is just a column. Rows that
    differ only in it share a fact_key (the caller asked for no extra key columns).
    """
    pe = date(2023, 9, 30)
    name = _seg_table(
        con,
        [
            (K23, 4, pe, "1.0000", "Seg=A"),
            (Q3_23, 4, pe, "3.0000", "Seg=B"),
        ],
    )
    keys = {r["segments"]: r["fact_key"] for r in fetch(derive_fiscal(con, name))}
    assert keys["Seg=A"] == keys["Seg=B"]


def test_duckdb_extra_key_cols_enter_fact_key(con: duckdb.DuckDBPyConnection) -> None:
    """With extra_key_cols=['segments'], two rows identical except for `segments` get different
    fact_keys (they are different series), while equal segments give equal fact_keys.
    """
    pe = date(2023, 9, 30)
    name = _seg_table(
        con,
        [
            (K23, 4, pe, "1.0000", "Seg=A"),
            (K23, 4, pe, "2.0000", "Seg=B"),
            (Q3_23, 4, pe, "3.0000", "Seg=A"),  # same series as row 0, other accession
        ],
    )
    out = fetch(derive_fiscal(con, name, ["segments"]))
    keys = {(r["adsh"], r["segments"]): r["fact_key"] for r in out}
    assert keys[(K23.adsh, "Seg=A")] != keys[(K23.adsh, "Seg=B")]
    assert keys[(K23.adsh, "Seg=A")] == keys[(Q3_23.adsh, "Seg=A")]


def test_duckdb_extra_key_cols_null_segments_value_is_a_series_of_its_own(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """A NULL value in an extra key column must not break hashing or pairing: two rows with a
    NULL `segments` are the same series (same fact_key), and differ from a non-NULL one. (A NULL
    that propagated through the hash would make fact_key NULL and silently merge everything.)
    """
    pe = date(2023, 9, 30)
    facts = [
        K23.fact("Revenues", pe, 4, "1.0000"),
        Q3_23.fact("Revenues", pe, 4, "2.0000"),
        K23.fact("Other", pe, 4, "3.0000"),
    ]
    name = to_table(
        con, facts, [(None,), (None,), ("Seg=A",)], [("segments", "VARCHAR")], name="seg_null"
    )
    out = fetch(derive_fiscal(con, name, ["segments"]))
    assert all(r["fact_key"] is not None and r["fact_id"] is not None for r in out)
    by_adsh_tag = {(r["adsh"], r["tag"]): r["fact_key"] for r in out}
    assert by_adsh_tag[(K23.adsh, "Revenues")] == by_adsh_tag[(Q3_23.adsh, "Revenues")]
    assert by_adsh_tag[(K23.adsh, "Revenues")] != by_adsh_tag[(K23.adsh, "Other")]
