"""Integration tests for the orchestration in `pipelines/spark/normalize_facts.py`.

`prepare_facts`, `conform` and `write_curated` are tested directly: they do not depend on the
Shekar-owned fiscal/version functions. The last test runs the whole job and stays RED until
`derive_fiscal_columns` and `select_versions` are implemented.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import StringType, StructField, StructType
from pyspark.sql.utils import AnalysisException

from pipelines.spark.curated_schemas import FACTS, FACTS_DIMENSIONAL, FACTS_HISTORY
from pipelines.spark.normalize_facts import (
    FISCAL_INPUT_COLUMNS,
    conform,
    prepare_facts,
    run,
    write_curated,
)
from tests.raw_builders import A_K, A_Q, num_df, sub_df


def collect(df: DataFrame) -> list[dict[str, Any]]:
    return [r.asDict() for r in df.collect()]


def test_consolidated_dimensional_and_coreg_rows_are_split(spark: SparkSession):
    num = num_df(
        spark,
        [
            {"tag": "Consolidated"},
            {"tag": "Segmented", "segments": "ProductOrServiceAxis=IPhoneMember"},
            {"tag": "CoRegistrant", "coreg": "0000000001"},
            {"tag": "Both", "segments": "A=B", "coreg": "0000000001"},
        ],
    )

    prepared = prepare_facts(num, sub_df(spark))

    assert [r["tag"] for r in collect(prepared.consolidated)] == ["Consolidated"]
    assert [r["tag"] for r in collect(prepared.dimensional)] == ["Segmented"]
    # `CoRegistrant` and `Both` are in neither table: co-registrant rows are not curated.


def test_empty_string_and_null_segments_and_coreg_are_both_consolidated(spark: SparkSession):
    """Raw loads empty fields as NULL, but '' must mean the same thing if it ever shows up."""
    num = num_df(
        spark,
        [
            {"tag": "NullBoth"},
            {"tag": "EmptyBoth", "segments": "", "coreg": ""},
            {"tag": "EmptySegOnly", "segments": ""},
        ],
    )

    prepared = prepare_facts(num, sub_df(spark))

    assert sorted(r["tag"] for r in collect(prepared.consolidated)) == [
        "EmptyBoth",
        "EmptySegOnly",
        "NullBoth",
    ]
    assert prepared.dimensional.count() == 0


@pytest.mark.parametrize(
    ("version", "taxonomy", "is_custom"),
    [
        ("us-gaap/2023", "us-gaap", False),
        ("us-gaap/2009", "us-gaap", False),
        ("dei/2022", "dei", False),
        ("srt/2023", "srt", False),
        ("ifrs-full/2023", "ifrs-full", False),
        (A_K, "custom", True),
    ],
)
def test_taxonomy_is_the_year_stripped_family_or_custom(
    spark: SparkSession, version: str, taxonomy: str, is_custom: bool
):
    prepared = prepare_facts(num_df(spark, [{"version": version}]), sub_df(spark))

    (row,) = collect(prepared.consolidated)
    assert row["taxonomy"] == taxonomy
    assert row["is_custom"] is is_custom


def test_dates_and_submission_fields_come_from_the_same_submission(spark: SparkSession):
    """fye, form, filed, fy and period are the 10-K's, even when a 10-Q row is also present."""
    num = num_df(
        spark,
        [
            {"adsh": A_K, "ddate": 20230930},
            {"adsh": A_Q, "ddate": 20230630, "qtrs": 3},
        ],
    )
    subs = sub_df(
        spark,
        [
            {},
            {
                "adsh": A_Q,
                "form": "10-Q",
                "fp": "Q3",
                "period": 20230630,
                "filed": 20230804,
                "src_quarter": "2023q3",
            },
        ],
    )

    prepared = prepare_facts(num, subs)

    by_adsh = {r["adsh"]: r for r in collect(prepared.consolidated)}
    assert by_adsh[A_K]["period_end"] == date(2023, 9, 30)
    assert by_adsh[A_K]["form"] == "10-K"
    assert by_adsh[A_K]["filed"] == date(2023, 11, 3)
    assert by_adsh[A_K]["fye_mmdd"] == "0930"
    assert by_adsh[A_K]["sub_fy"] == 2023
    assert by_adsh[A_K]["sub_period"] == date(2023, 9, 30)
    assert by_adsh[A_Q]["period_end"] == date(2023, 6, 30)
    assert by_adsh[A_Q]["form"] == "10-Q"
    assert by_adsh[A_Q]["filed"] == date(2023, 8, 4)
    assert by_adsh[A_Q]["sub_period"] == date(2023, 6, 30)
    assert by_adsh[A_Q]["cik"] == 320193


def test_output_columns_are_exactly_the_fiscal_input_contract(spark: SparkSession):
    prepared = prepare_facts(num_df(spark, [{}, {"tag": "Seg", "segments": "A=B"}]), sub_df(spark))

    assert tuple(prepared.consolidated.columns) == FISCAL_INPUT_COLUMNS
    assert tuple(prepared.dimensional.columns) == (*FISCAL_INPUT_COLUMNS, "segments")
    dtypes = dict(prepared.consolidated.dtypes)
    assert dtypes["value"] == "decimal(28,4)"
    assert dtypes["period_end"] == "date" and dtypes["filed"] == "date"
    assert dtypes["sub_period"] == "date" and dtypes["fye_mmdd"] == "string"


def test_null_values_and_unparseable_dates_are_dropped(spark: SparkSession):
    num = num_df(
        spark,
        [
            {"tag": "Good"},
            {"tag": "NoValue", "value": None},
            {"tag": "BadDate", "ddate": 20231399},
        ],
    )

    prepared = prepare_facts(num, sub_df(spark))

    assert [r["tag"] for r in collect(prepared.consolidated)] == ["Good"]


def test_rows_without_a_submission_are_dropped(spark: SparkSession):
    num = num_df(spark, [{"tag": "Orphan", "adsh": "0000000000-23-000001"}, {"tag": "Kept"}])

    prepared = prepare_facts(num, sub_df(spark))

    assert [r["tag"] for r in collect(prepared.consolidated)] == ["Kept"]


def test_a_tag_under_two_taxonomy_years_in_one_filing_collapses_to_the_latest_year(
    spark: SparkSession,
):
    """Year-stripping makes these two rows share one (fact_key, accession); keep one."""
    num = num_df(
        spark,
        [
            {"version": "us-gaap/2022", "value": Decimal("1.0000")},
            {"version": "us-gaap/2023", "value": Decimal("2.0000")},
        ],
    )

    prepared = prepare_facts(num, sub_df(spark))

    (row,) = collect(prepared.consolidated)
    assert row["value"] == Decimal("2.0000")


def test_the_same_filing_loaded_from_two_quarters_collapses_to_the_latest_quarter(
    spark: SparkSession,
):
    num = num_df(
        spark,
        [
            {"src_quarter": "2023q4", "value": Decimal("1.0000")},
            {"src_quarter": "2024q1", "value": Decimal("2.0000")},
        ],
    )
    subs = sub_df(
        spark,
        [{"src_quarter": "2023q4", "fy": 2023}, {"src_quarter": "2024q1", "fy": 2024}],
    )

    prepared = prepare_facts(num, subs)

    (row,) = collect(prepared.consolidated)  # one row, not 2 x 2 from the join
    assert row["value"] == Decimal("2.0000")
    assert row["sub_fy"] == 2024  # the latest quarter's sub row, not the older one


def test_dedup_does_not_merge_different_periods_units_or_segments(spark: SparkSession):
    num = num_df(
        spark,
        [
            {"tag": "T"},
            {"tag": "T", "ddate": 20220930},
            {"tag": "T", "qtrs": 3},
            {"tag": "T", "uom": "EUR"},
            {"tag": "T", "segments": "A=B"},
            {"tag": "T", "segments": "A=C"},
        ],
    )

    prepared = prepare_facts(num, sub_df(spark))

    assert prepared.consolidated.count() == 4
    assert prepared.dimensional.count() == 2


# --- conform / write_curated ------------------------------------------------------------


def test_conform_selects_and_casts_to_the_contract_columns(spark: SparkSession):
    df = spark.createDataFrame(
        [(1, "x")],
        StructType([StructField("fact_id", StringType()), StructField("zzz", StringType())]),
    ).withColumn("fact_id", F.col("fact_id"))
    schema = StructType([f for f in FACTS_HISTORY.fields if f.name == "fact_id"])

    out = conform(df, schema)

    assert out.columns == ["fact_id"]
    assert dict(out.dtypes)["fact_id"] == "bigint"


def test_conform_fails_loudly_when_a_contract_column_is_missing(spark: SparkSession):
    """If a Shekar-owned function stops producing a column, the job must not write NULLs."""
    df = spark.createDataFrame([(1,)], ["fact_id"])

    with pytest.raises(AnalysisException):
        conform(df, FACTS_HISTORY)


def test_write_curated_creates_a_fiscal_year_partitioned_table_and_replaces_all_of_it(
    spark: SparkSession, lake_namespace: str
):
    cols = {f.name: f for f in FACTS_DIMENSIONAL.fields}

    def frame(year: int, n: int) -> DataFrame:
        rows = [
            {c: None for c in cols} | {"fact_id": i, "fiscal_year": year, "value": Decimal(i)}
            for i in range(n)
        ]
        data = [tuple(r[c] for c in cols) for r in rows]
        return spark.createDataFrame(data, FACTS_DIMENSIONAL)

    table = f"{lake_namespace}.dim"
    assert write_curated(spark, frame(2022, 2).union(frame(2023, 3)), table, FACTS_DIMENSIONAL) == 5
    # A full rebuild whose output has no 2022 rows must not leave the old 2022 partition behind.
    assert write_curated(spark, frame(2023, 1), table, FACTS_DIMENSIONAL) == 1

    years = {
        r.fiscal_year: r["count"]
        for r in spark.table(table).groupBy("fiscal_year").count().collect()
    }
    assert years == {2023: 1}
    assert write_curated(spark, frame(2023, 1), table, FACTS_DIMENSIONAL) == 1  # idempotent
    parts = spark.sql(f"SELECT partition.fiscal_year AS y FROM {table}.partitions")
    assert [r.y for r in parts.collect() if r.y is not None] == [2023]


# --- the whole job (RED until Shekar implements the two functions) -----------------------


def test_run_builds_the_three_curated_tables_from_raw(spark: SparkSession, lake_namespace: str):
    """End to end on Apple-like data. Fails with NotImplementedError until fiscal.py and
    versions.py exist; then it checks the contract of the written tables."""
    raw_ns, cur_ns = f"{lake_namespace}_raw", f"{lake_namespace}_cur"
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {raw_ns}")
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {cur_ns}")
    try:
        num = num_df(
            spark,
            [
                # FY2023 10-K: revenue FY, EPS FY, an instant, and one segmented row.
                {"tag": "Revenues", "value": Decimal("383285000000.0000")},
                {"tag": "EarningsPerShareDiluted", "uom": "USD/shares", "value": Decimal("6.1300")},
                {"tag": "Assets", "qtrs": 0, "value": Decimal("352583000000.0000")},
                {"tag": "Revenues", "segments": "ProductOrServiceAxis=IPhoneMember"},
                # Q3 10-Q: 9M revenue, so Q4 is derivable for revenue only.
                {"adsh": A_Q, "tag": "Revenues", "qtrs": 3, "ddate": 20230630,
                 "value": Decimal("293787000000.0000"), "src_quarter": "2023q3"},
                {"adsh": A_Q, "tag": "EarningsPerShareDiluted", "uom": "USD/shares", "qtrs": 3,
                 "ddate": 20230630, "value": Decimal("4.6800"), "src_quarter": "2023q3"},
            ],
        )  # fmt: skip
        subs = sub_df(
            spark,
            [
                {},
                {"adsh": A_Q, "form": "10-Q", "fp": "Q3", "period": 20230630,
                 "filed": 20230804, "src_quarter": "2023q3"},
            ],
        )  # fmt: skip
        num.writeTo(f"{raw_ns}.fsds_num").using("iceberg").create()
        subs.writeTo(f"{raw_ns}.fsds_sub").using("iceberg").create()

        counts = run(spark, raw_ns, cur_ns)

        assert spark.table(f"{cur_ns}.facts").columns == FACTS.fieldNames()
        assert spark.table(f"{cur_ns}.facts_history").columns == FACTS_HISTORY.fieldNames()
        assert spark.table(f"{cur_ns}.facts_dimensional").columns == FACTS_DIMENSIONAL.fieldNames()
        facts = spark.table(f"{cur_ns}.facts")
        assert facts.where(F.col("tag") == "Revenues").count() >= 3  # FY, 9M, derived Q4
        assert (
            facts.where(F.array_contains("dq_flags", "q4_derived"))
            .where(F.col("tag") == "Revenues")
            .count()
            == 1
        )
        assert (
            facts.where(F.col("tag") == "EarningsPerShareDiluted")
            .where(F.array_contains("dq_flags", "q4_derived"))
            .count()
            == 0
        )
        assert counts["facts_dimensional"] == 1
    finally:
        for ns in (raw_ns, cur_ns):
            for r in spark.sql(f"SHOW TABLES IN {ns}").collect():
                spark.sql(f"DROP TABLE IF EXISTS {ns}.{r.tableName} PURGE")
            spark.sql(f"DROP NAMESPACE IF EXISTS {ns}")
