"""Spark twin of `tests/unit/test_versions.py`: `select_versions` over DataFrames.

The same hand-derived scenarios (`tests/fiscal_cases.py::VERSION_SCENARIOS`) are run through
Spark and compared with the same expected rows as the pure-Python reference.

Needs a JVM (tests/integration/conftest.py): the `spark` fixture fails without one, never skips.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DateType,
    DecimalType,
    IntegerType,
    LongType,
    StringType,
    StructField,
    StructType,
)

from pipelines.common.fiscal_ref import FactVersion
from pipelines.spark.versions import select_versions
from tests.fiscal_cases import VERSION_SCENARIOS, VersionScenario, sorted_version_tuples

VERSION_SCHEMA = StructType(
    [
        StructField("fact_key", LongType()),
        StructField("fact_id", LongType()),
        StructField("period_end", DateType()),
        StructField("value", DecimalType(28, 4)),
        StructField("accession", StringType()),
        StructField("form", StringType()),
        StructField("filed", DateType()),
        StructField("is_amendment", BooleanType()),
        StructField("filing_period", DateType()),
    ]
)
INPUT_COLUMNS = [f.name for f in VERSION_SCHEMA.fields]


def to_df(
    spark: SparkSession, versions: list[FactVersion], extras: list[tuple[Any, ...]] | None = None
) -> DataFrame:
    fields = list(VERSION_SCHEMA.fields)
    if extras is not None:
        fields.append(StructField("note", StringType()))
    rows = []
    for i, v in enumerate(versions):
        base = tuple(getattr(v, c) for c in INPUT_COLUMNS)
        rows.append(base + (extras[i] if extras is not None else ()))
    return spark.createDataFrame(rows, StructType(fields))


def as_tuples(out: DataFrame) -> list[tuple[int, str, Decimal | None, bool, bool, int, bool]]:
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
            for r in out.collect()
        ]
    )


@pytest.mark.parametrize("scenario", VERSION_SCENARIOS, ids=repr)
def test_spark_version_selection_matches_hand_derived_rows(
    spark: SparkSession, scenario: VersionScenario
) -> None:
    """Latest/original selection in Spark: single version, comparative with the same value, a
    restatement, 10-K then 10-K/A, no filing_period match, a non-match filed earlier, the
    accession tie-breaks on both sides, and 100 -> 90 -> 100. Each yields exactly the expected
    rows with the expected flags, n_versions and is_restated.
    """
    out = select_versions(to_df(spark, list(scenario.versions)))
    assert as_tuples(out) == sorted_version_tuples([e.as_tuple() for e in scenario.expect])


NULL_SCENARIOS = [
    s for s in VERSION_SCENARIOS if s.name.startswith(("null_", "all_filing_periods_null"))
]


def test_null_scenarios_are_present() -> None:
    """Guard: the NULL-value / NULL-filing_period scenarios exist, so the Spark test below can
    never pass vacuously over an empty parameter list.
    """
    assert len(NULL_SCENARIOS) == 5


@pytest.mark.parametrize("scenario", NULL_SCENARIOS, ids=repr)
def test_spark_null_value_and_null_filing_period_follow_python_semantics(
    spark: SparkSession, scenario: VersionScenario
) -> None:
    """NULL handling matches the reference: a NULL value is a distinct value for is_restated
    (Spark's count(distinct) ignores NULLs) and NULL == NULL collapses latest/original (SQL `=`
    yields NULL, so a null-safe comparison is needed); a NULL filing_period is never native and
    does not poison the original selection. Rows keep their NULL value through the selection.
    """
    out = select_versions(to_df(spark, list(scenario.versions)))
    assert as_tuples(out) == sorted_version_tuples([e.as_tuple() for e in scenario.expect])


@pytest.mark.parametrize("seed", [None, 1, 2, 3])
def test_spark_independent_fact_keys_and_input_order(spark: SparkSession, seed: int | None) -> None:
    """All scenarios in one DataFrame (each with its own fact_key): the output is the union of
    the per-scenario outputs whatever the row order (as given, reversed, three shuffles).
    """
    versions = [v for s in VERSION_SCENARIOS for v in s.versions]
    expected = sorted_version_tuples([e.as_tuple() for s in VERSION_SCENARIOS for e in s.expect])
    if seed is None:
        versions = versions[::-1]
    else:
        random.Random(seed).shuffle(versions)
    assert as_tuples(select_versions(to_df(spark, versions))) == expected


def test_spark_output_schema_and_pass_through_columns(spark: SparkSession) -> None:
    """The output has every input column (pass-through `note` included, with its value intact on
    the selected rows) plus is_latest/is_original (boolean), n_versions (int), is_restated
    (boolean).
    """
    sc = next(s for s in VERSION_SCENARIOS if s.name == "restated_100_to_90")
    notes = [(f"note-{v.accession}",) for v in sc.versions]
    out = select_versions(to_df(spark, list(sc.versions), notes))
    dtypes = dict(out.dtypes)
    for f in VERSION_SCHEMA.fields:
        assert dtypes[f.name] == f.dataType.simpleString(), f.name
    assert dtypes["note"] == "string"
    assert dtypes["is_latest"] == "boolean"
    assert dtypes["is_original"] == "boolean"
    assert dtypes["n_versions"] == IntegerType().simpleString()
    assert dtypes["is_restated"] == "boolean"
    assert {r["accession"]: r["note"] for r in out.collect()} == {
        v.accession: f"note-{v.accession}" for v in sc.versions
    }


def test_spark_output_rows_are_selected_input_rows(spark: SparkSession) -> None:
    """Each output row is an input row carried through whole (same fact_id, value, filed, form):
    selecting a version never synthesises or mixes columns from different versions.
    """
    sc = next(s for s in VERSION_SCENARIOS if s.name == "ten_k_then_ten_k_a_with_different_value")
    out = select_versions(to_df(spark, list(sc.versions)))
    by_id = {v.fact_id: v for v in sc.versions}
    for r in out.collect():
        v = by_id[r["fact_id"]]
        assert (r["accession"], r["value"], r["filed"], r["form"], r["is_amendment"]) == (
            v.accession,
            v.value,
            v.filed,
            v.form,
            v.is_amendment,
        )
        assert (r["period_end"], r["filing_period"]) == (v.period_end, v.filing_period)
    assert date(2023, 4, 20) in {r["filed"] for r in out.collect()}


def test_a_derived_row_sharing_the_identity_of_a_real_row_never_displaces_it(
    spark: SparkSession,
):
    """A derived Q4 takes its FY parent's accession, so it can share (fact_key, accession,
    fact_id) with a real quarter row. Whatever the row order or partitioning, the real row wins.
    """
    schema = StructType(
        [*VERSION_SCHEMA.fields, StructField("derived_from", ArrayType(LongType()))]
    )
    real = (1, 77, date(2023, 9, 30), Decimal("12131.0000"), "0001-23-000018", "10-Q",
            date(2023, 10, 27), False, date(2023, 9, 30), None)  # fmt: skip
    seen = set()
    for derived_value in ("100.0000", "12685.0000"):  # lower and higher than the real row
        derived = (1, 77, date(2023, 9, 30), Decimal(derived_value), "0001-23-000018", "10-Q",
                   date(2023, 10, 27), False, date(2023, 9, 30), [8, 9])  # fmt: skip
        _check_derived_never_displaces_real(spark, schema, real, derived, seen)

    assert len(seen) == 1


def _check_derived_never_displaces_real(spark, schema, real, derived, seen):  # type: ignore[no-untyped-def]
    for rows, partitions in (([real, derived], 1), ([derived, real], 1), ([derived, real], 2),
                             ([real, derived], 2)):  # fmt: skip
        df = spark.createDataFrame(rows, schema).repartition(partitions)
        out = select_versions(df).collect()
        assert len(out) == 1
        assert out[0]["derived_from"] is None and out[0]["value"] == Decimal("12131.0000")
        assert (out[0]["is_latest"], out[0]["is_original"], out[0]["n_versions"]) == (True, True, 2)
        seen.add((out[0]["value"], out[0]["n_versions"]))


def test_rows_tied_on_every_key_resolve_to_the_non_null_value_first(spark: SparkSession):
    """NULLS LAST in the tie-break, the same as the DuckDB twin: a NULL value never wins a tie."""
    schema = StructType(
        [*VERSION_SCHEMA.fields, StructField("derived_from", ArrayType(LongType()))]
    )
    real = (1, 77, date(2023, 9, 30), Decimal("12131.0000"), "0001-23-000018", "10-Q",
            date(2023, 10, 27), False, date(2023, 9, 30), None)  # fmt: skip
    with_null = (*real[:3], None, *real[4:])
    for rows in ([real, with_null], [with_null, real]):
        out = select_versions(spark.createDataFrame(rows, schema)).collect()

        assert any(r["is_latest"] and r["value"] == Decimal("12131.0000") for r in out)
