"""Differential tests: the DuckDB functions must agree with the pure-Python reference.

`pipelines.duckdb.normalize_facts_duckdb.derive_fiscal` vs `fiscal_ref.derive_fiscal`, and
`select_versions` vs `select_versions_ref`, on the SAME seeded synthetic generators as
`tests/integration/test_differential.py` (about 10,000 fact rows sampled by whole company, and
about 4,500 versions over 2,500 fact keys). No JVM: DuckDB in memory only.

Rows are matched on the natural key, never on the hash columns (DuckDB's `hash` differs from
Spark's `xxhash64`); `derived_from` ids are resolved back to (period_end, qtrs, accession)
through the output's own `fact_id`. The generators are tested in test_differential.py; here a red
run means the implementations disagree.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

import duckdb
import pytest

from pipelines.common.fiscal_ref import (
    FactInput,
    FactVersion,
    select_versions_ref,
)
from pipelines.common.fiscal_ref import (
    derive_fiscal as derive_fiscal_ref,
)
from pipelines.duckdb.normalize_facts_duckdb import derive_fiscal, select_versions
from tests.integration.test_differential import generate, generate_versions

FISCAL_COLUMNS = [
    ("cik", "INTEGER"),
    ("adsh", "VARCHAR"),
    ("tag", "VARCHAR"),
    ("taxonomy", "VARCHAR"),
    ("is_custom", "BOOLEAN"),
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
SEP = "\x1f"  # joins tag and series for the reference, which has no notion of extra key columns


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def create(
    con: duckdb.DuckDBPyConnection,
    name: str,
    columns: Sequence[tuple[str, str]],
    rows: Sequence[tuple[Any, ...]],
) -> str:
    con.execute(f"CREATE TABLE {name} ({', '.join(f'{c} {t}' for c, t in columns)})")
    con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in columns)})", list(rows))
    return name


def fact_rows(facts: Sequence[FactInput]) -> list[tuple[Any, ...]]:
    return [
        (f.cik, f.adsh, f.tag, f.taxonomy, False, f.uom, f.qtrs, f.period_end, f.value, f.form,
         f.filed, f.fye_mmdd, f.sub_fy, f.sub_period)
        for f in facts
    ]  # fmt: skip


def fetch(rel: duckdb.DuckDBPyRelation) -> list[dict[str, Any]]:
    names = list(rel.columns)
    return [dict(zip(names, r, strict=True)) for r in rel.fetchall()]


def mismatches_fiscal(
    out: list[dict[str, Any]],
    expected: Sequence[Any],
    key_of_out: Any,
    key_of_ref: Any,
    compare_value: bool = True,
) -> list[str]:
    """Row-set equality on the natural key, then column equality; returns the mismatches."""
    actual = {key_of_out(r): r for r in out}
    assert len(actual) == len(out), "duplicate natural keys in the DuckDB output"
    ref = {key_of_ref(x): x for x in expected}
    assert len(ref) == len(expected), "duplicate natural keys in the reference output"
    assert set(actual) == set(ref), (
        f"row sets differ: {len(set(actual) - set(ref))} only in DuckDB, "
        f"{len(set(ref) - set(actual))} only in the reference"
    )
    by_id = {r["fact_id"]: key_of_out(r) for r in out}
    assert len(by_id) == len(out), "fact_id must be unique across the output"
    problems: list[str] = []
    for key, x in ref.items():
        r = actual[key]
        assert r["dq_flags"] is not None, f"{key}: dq_flags is NULL"
        got_parents = (
            tuple((by_id[i][-1], by_id[i][-2], by_id[i][1]) for i in r["derived_from"])
            if r["derived_from"] is not None
            else None
        )
        checks = {
            "period_start": (r["period_start"], x.period_start),
            "fiscal_year": (r["fiscal_year"], x.fiscal_year),
            "fiscal_period": (r["fiscal_period"], x.fiscal_period),
            "calendar_frame": (r["calendar_frame"], x.calendar_frame),
            "dq_flags": (tuple(sorted(r["dq_flags"])), tuple(sorted(x.dq_flags))),
            "derived_from": (got_parents, x.derived_from),
        }
        if compare_value:
            checks["value"] = (r["value"], x.fact.value)
        for name, (got, want) in checks.items():
            if got != want:
                problems.append(f"{key} {name}: duckdb={got!r} reference={want!r}")
    return problems


def natural_key_out(r: dict[str, Any]) -> tuple[Any, ...]:
    # parents are resolved from this key: adsh is [1], qtrs [-2], period_end [-1]
    return (r["cik"], r["adsh"], r["tag"], r["taxonomy"], r["uom"], r["qtrs"], r["period_end"])


def natural_key_ref(x: Any) -> tuple[Any, ...]:
    f = x.fact
    return (f.cik, f.adsh, f.tag, f.taxonomy, f.uom, f.qtrs, f.period_end)


def test_derive_fiscal_agrees_with_the_python_reference(con: duckdb.DuckDBPyConnection) -> None:
    """About 10,000 generated facts (52/53-week jitter, late-January offset -1 companies, direct
    Q4s, per-share facts, instants, missing FY/9M partners): the DuckDB output has exactly the
    reference's row set (inputs plus derived Q4s) and, row by row, the same period_start,
    fiscal_year, fiscal_period, calendar_frame, dq_flags, value and derived_from parents.
    """
    gen = generate()
    expected = derive_fiscal_ref(gen.facts)
    table = create(con, "src", FISCAL_COLUMNS, fact_rows(gen.facts))
    out = fetch(derive_fiscal(con, table))

    problems = mismatches_fiscal(out, expected, natural_key_out, natural_key_ref)
    assert not problems, f"{len(problems)} mismatches, first 10:\n" + "\n".join(problems[:10])
    assert sum(1 for r in out if r["derived_from"] is not None) > 100  # real derivation happened


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_derive_fiscal_agreement_holds_for_other_seeds(
    con: duckdb.DuckDBPyConnection, seed: int
) -> None:
    """Different companies, fiscal year ends and gaps: not one lucky data set."""
    gen = generate(seed=seed, target_rows=2_000)
    expected = derive_fiscal_ref(gen.facts)
    table = create(con, "src", FISCAL_COLUMNS, fact_rows(gen.facts))
    out = fetch(derive_fiscal(con, table))
    problems = mismatches_fiscal(out, expected, natural_key_out, natural_key_ref)
    assert not problems, f"{len(problems)} mismatches, first 10:\n" + "\n".join(problems[:10])


def test_derive_fiscal_input_row_order_does_not_matter(con: duckdb.DuckDBPyConnection) -> None:
    """The reference documents input order as irrelevant; the DuckDB twin must give the same
    answer for a shuffled table (a window frame or `first()` that depended on scan order would
    differ).
    """
    gen = generate(seed=11, target_rows=2_000)
    rows = fact_rows(gen.facts)
    random.Random(11).shuffle(rows)
    table = create(con, "src", FISCAL_COLUMNS, rows)
    out = fetch(derive_fiscal(con, table))
    problems = mismatches_fiscal(
        out, derive_fiscal_ref(gen.facts), natural_key_out, natural_key_ref
    )
    assert not problems, f"{len(problems)} mismatches, first 10:\n" + "\n".join(problems[:10])


def test_derive_fiscal_with_extra_key_cols_agrees_with_the_reference_per_series(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """With extra_key_cols=['segments'] every (company, tag, segments) is its own series. The
    reference has no such notion, so each series is given its own tag there ('tag' + SEP +
    'segments'); the two must then agree on every derived column and every derived Q4. Two
    segment series per generated fact, with different values, so pairing across series would
    show up as a wrong value or an extra row.
    """
    gen = generate(seed=5, target_rows=2_000)
    series = ["Seg=A", "Seg=B"]
    seg_rows: list[tuple[Any, ...]] = []
    ref_facts: list[FactInput] = []
    for f in gen.facts:
        for i, seg in enumerate(series):
            value = None if f.value is None else f.value + Decimal(i) * 7
            seg_rows.append(
                (f.cik, f.adsh, f.tag, f.taxonomy, False, f.uom, f.qtrs, f.period_end, value,
                 f.form, f.filed, f.fye_mmdd, f.sub_fy, f.sub_period, seg)
            )  # fmt: skip
            ref_facts.append(replace(f, tag=f"{f.tag}{SEP}{seg}", value=value))
    table = create(con, "seg_src", [*FISCAL_COLUMNS, ("segments", "VARCHAR")], seg_rows)
    out = fetch(derive_fiscal(con, table, ["segments"]))
    expected = derive_fiscal_ref(ref_facts)

    def key_out(r: dict[str, Any]) -> tuple[Any, ...]:
        return (
            r["cik"], r["adsh"], f"{r['tag']}{SEP}{r['segments']}", r["taxonomy"], r["uom"],
            r["qtrs"], r["period_end"],
        )  # fmt: skip

    problems = mismatches_fiscal(out, expected, key_out, natural_key_ref)
    assert not problems, f"{len(problems)} mismatches, first 10:\n" + "\n".join(problems[:10])
    derived = [r for r in out if r["derived_from"] is not None]
    assert derived, "no derived Q4 in the segment data; the test would pass vacuously"
    assert {r["segments"] for r in derived} == set(series)


# --------------------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------------------


def version_rows(versions: Sequence[FactVersion]) -> list[tuple[Any, ...]]:
    return [
        (v.fact_key, v.fact_id, v.period_end, v.value, v.accession, v.form, v.filed,
         v.is_amendment, v.filing_period)
        for v in versions
    ]  # fmt: skip


def assert_versions_agree(con: duckdb.DuckDBPyConnection, versions: list[FactVersion]) -> None:
    expected = select_versions_ref(versions)
    table = create(con, "versions", VERSION_COLUMNS, version_rows(versions))
    out = fetch(select_versions(con, table))

    got = Counter(
        (r["fact_key"], r["accession"], r["is_latest"], r["is_original"], r["n_versions"],
         r["is_restated"])
        for r in out
    )  # fmt: skip
    want = Counter(
        (s.version.fact_key, s.version.accession, s.is_latest, s.is_original, s.n_versions,
         s.is_restated)
        for s in expected
    )  # fmt: skip
    assert got == want, (
        f"{sum((got - want).values())} rows only in DuckDB, "
        f"{sum((want - got).values())} only in the reference"
    )
    # The selected rows are input rows carried through whole, not rebuilt.
    by_id = {v.fact_id: v for v in versions}
    for r in out:
        v = by_id[r["fact_id"]]
        assert (r["value"], r["filed"], r["form"], r["filing_period"]) == (
            v.value,
            v.filed,
            v.form,
            v.filing_period,
        )


def test_select_versions_agrees_with_the_python_reference(con: duckdb.DuckDBPyConnection) -> None:
    """2,500 fact keys with 1-4 versions each, ties on `filed` (the accession tie-break),
    several covering versions per key, and keys with no covering version: the DuckDB rows,
    flags, n_versions and is_restated equal the reference's, as a multiset.
    """
    assert_versions_agree(con, generate_versions())


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_select_versions_agreement_holds_for_other_seeds(
    con: duckdb.DuckDBPyConnection, seed: int
) -> None:
    """Different seeds change which versions tie and which cover the period."""
    assert_versions_agree(con, generate_versions(seed=seed, n_keys=1_000))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_select_versions_agrees_with_the_reference_with_nulls(
    con: duckdb.DuckDBPyConnection, seed: int
) -> None:
    """NULL values (about 15% of versions) and NULL filing_periods (about 15%) injected into the
    generated data: NULL counts as a distinct value, NULL == NULL collapses latest/original, and a
    NULL filing_period is never native, exactly as in the reference (Python `==`).
    """
    rng = random.Random(seed)
    versions = [
        replace(
            v,
            value=None if rng.random() < 0.15 else v.value,
            filing_period=None if rng.random() < 0.15 else v.filing_period,  # type: ignore[arg-type]
        )
        for v in generate_versions(seed=seed, n_keys=1_500)
    ]
    assert any(v.value is None for v in versions)
    assert any(v.filing_period is None for v in versions)
    assert_versions_agree(con, versions)


def test_select_versions_agrees_with_the_reference_on_shuffled_input(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Row order of the input table must not matter."""
    versions = generate_versions(seed=9, n_keys=1_000)
    random.Random(9).shuffle(versions)
    assert_versions_agree(con, versions)
