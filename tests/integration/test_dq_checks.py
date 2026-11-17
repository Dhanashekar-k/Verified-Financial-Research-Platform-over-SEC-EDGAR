"""Integration tests for the DQ metrics (`pipelines/spark/dq_checks.py`) on tiny raw tables."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pyspark.sql import SparkSession
from pyspark.sql.types import StringType, StructField, StructType, TimestampType

from pipelines.spark.curated_schemas import FACTS
from pipelines.spark.dq_checks import (
    NotMeasured,
    collect_metrics,
    curated_summary,
    fye_changes,
    raw_shares,
    render_report,
    rows_per_quarter,
)
from pipelines.spark.raw_schemas import PRE_SOURCE, TAG_SOURCE
from tests.raw_builders import A_K, A_Q, INGESTED, num_df, sub_df

NO_FYE = "0000000000-23-000009"  # a submission whose sub.fye is NULL
EMPTY_FYE = "0000000000-23-000008"  # a submission whose sub.fye is ''


def make_raw(spark: SparkSession, ns: str) -> str:
    """Raw tables: num/sub in quarters 2023q3 and 2024q1 (a gap at 2023q4); tag/pre empty."""
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {ns}")
    num = num_df(
        spark,
        [
            {"tag": "Revenues", "src_quarter": "2023q3"},
            {"tag": "Custom", "version": A_K, "src_quarter": "2023q3"},
            {"tag": "Seg", "segments": "A=B", "src_quarter": "2024q1"},
            {"tag": "Co", "coreg": "0000000001", "src_quarter": "2024q1"},
            {"tag": "NoValue", "value": None, "src_quarter": "2024q1", "uom": "pure"},
        ],
    )
    sub = sub_df(
        spark,
        [
            {"src_quarter": "2023q3"},
            {"adsh": A_Q, "fye": "1231", "form": "10-Q", "src_quarter": "2024q1"},  # FYE changed
        ],
    )
    num.writeTo(f"{ns}.fsds_num").using("iceberg").create()
    sub.writeTo(f"{ns}.fsds_sub").using("iceberg").create()
    for name, schema in (("tag", TAG_SOURCE), ("pre", PRE_SOURCE)):
        full = StructType(
            [
                *schema.fields,
                StructField("src_quarter", StringType()),
                StructField("ingested_at", TimestampType()),
            ]
        )
        spark.createDataFrame([], full).writeTo(f"{ns}.fsds_{name}").using("iceberg").create()
    return ns


def drop_all(spark: SparkSession, ns: str) -> None:
    for r in spark.sql(f"SHOW TABLES IN {ns}").collect():
        spark.sql(f"DROP TABLE IF EXISTS {ns}.{r.tableName} PURGE")
    spark.sql(f"DROP NAMESPACE IF EXISTS {ns}")


def test_raw_sections_are_measured_and_curated_sections_say_not_measured(
    spark: SparkSession, lake_namespace: str, tmp_path: Path
):
    raw_ns = make_raw(spark, f"{lake_namespace}_raw")
    try:
        m = collect_metrics(
            spark, raw_ns, f"{lake_namespace}_cur", spotcheck_path=tmp_path / "missing.json"
        )

        rq = m["rows_per_quarter"]
        assert rq["tables"]["num"] == {"2023q3": 2, "2024q1": 3}
        assert rq["missing_quarters"] == ["2023q4"]
        assert rq["starts_at_2009q2"] is False
        shares = m["raw_shares"]
        assert shares["rows"] == 5
        assert shares["custom"]["rows"] == 1
        assert shares["segmented"]["rows"] == 1
        assert shares["coreg"]["rows"] == 1
        assert shares["null_value"]["rows"] == 1
        assert m["uom"]["distinct_uom"] == 2
        assert m["fye_changes"]["companies_with_changed_fye"] == 1
        assert m["fye_changes"]["examples"][0]["fyes"] == ["0930", "1231"]
        assert m["apple_revenue_raw"]["rows"][0]["value"] == "100.0000"
        # Nothing is estimated for what does not exist yet.
        for key in ("curated_facts", "spotcheck", "apple_revenue_curated"):
            assert isinstance(m[key], NotMeasured), key
        report = render_report(m)
        assert "**NOT MEASURED**" in report
        assert "| 2023q3 | 1 | 2 | 0 | 0 |" in report
    finally:
        drop_all(spark, raw_ns)


def test_label_offsets_is_not_measured_until_the_reference_exists_then_a_distribution(
    spark: SparkSession, lake_namespace: str, tmp_path: Path
):
    raw_ns = make_raw(spark, f"{lake_namespace}_raw")
    try:
        section = collect_metrics(spark, raw_ns, "lake.none", tmp_path / "x.json")["label_offsets"]

        if isinstance(section, NotMeasured):
            assert "not implemented" in section.reason
        else:  # once Shekar implements label_offsets the section becomes real numbers
            assert section["companies"] >= 1
    finally:
        drop_all(spark, raw_ns)


def test_curated_summary_counts_flags_versions_and_derived_rows(
    spark: SparkSession, lake_namespace: str
):
    cols = FACTS.fieldNames()

    def fact(i: int, **kw: object) -> tuple[object, ...]:
        base: dict[str, object] = {c: None for c in cols} | {
            "fact_id": i,
            "fact_key": i,
            "fiscal_year": 2023,
            "is_custom": False,
            "is_restated": False,
            "n_versions": 1,
            "dq_flags": [],
            "value": Decimal("1.0000"),
        }
        return tuple((base | kw)[c] for c in cols)

    rows = [
        fact(1),
        fact(2, is_custom=True),
        fact(3, dq_flags=["q4_derived"], derived_from=[1, 2]),
        # one restated fact (fact_key 4) is TWO rows: latest and original
        fact(4, fact_key=4, is_restated=True, n_versions=2, fiscal_year=2022),
        fact(5, fact_key=4, is_restated=True, n_versions=2, fiscal_year=2022),
    ]
    table = f"{lake_namespace}.facts"
    spark.createDataFrame(rows, FACTS).writeTo(table).using("iceberg").create()

    out = curated_summary(spark, lake_namespace)

    assert out["rows"] == 5
    assert out["facts"] == 4  # the restated fact's two rows count once
    assert out["rows_by_fiscal_year"] == {2023: 3, 2022: 2}
    assert out["custom"]["rows"] == 1
    assert out["restated_facts"] == 1  # not 2
    assert out["derived_q4"]["rows"] == 1
    assert out["with_dq_flags"] == {"rows": 1, "share": 0.2}
    assert out["flag_counts"] == {"q4_derived": 1}
    assert out["n_versions_per_fact"] == {1: 3, 2: 1}  # one fact with 2 versions, not two
    assert out["null_rate"]["calendar_frame"] == 1.0
    assert out["null_rate"]["fact_id"] == 0.0


def test_individual_metrics_work_on_their_own(spark: SparkSession, lake_namespace: str):
    raw_ns = make_raw(spark, f"{lake_namespace}_raw")
    try:
        assert rows_per_quarter(spark, raw_ns)["last_quarter"] == "2024q1"
        assert fye_changes(spark, raw_ns)["companies"] == 1
        assert raw_shares(spark.table(f"{raw_ns}.fsds_num"))["null_value"]["share"] == 0.2
    finally:
        drop_all(spark, raw_ns)


_ = INGESTED  # re-exported by the builders; kept so the import documents the fixed ingest time


def test_drop_counts_and_blank_equivalence_on_raw(spark: SparkSession, lake_namespace: str):
    from pipelines.spark.dq_checks import drop_counts

    ns = f"{lake_namespace}_raw"
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {ns}")
    try:
        num = num_df(
            spark,
            [
                {"tag": "Ok"},
                {"tag": "NoValue", "value": None},
                {"tag": "BadDate", "ddate": 20231399},
                {"tag": "Orphan", "adsh": "0000000000-23-000001"},
                {"tag": "EmptySeg", "segments": ""},  # '' counts as absent, like NULL
                {"tag": "NoFye", "adsh": NO_FYE},  # S-1: its sub row has no fye
                {"tag": "EmptyFye", "adsh": EMPTY_FYE},  # '' counts as absent, like NULL
                {"tag": "Both", "adsh": NO_FYE, "value": None},  # two causes: counted in each
            ],
        )
        num.writeTo(f"{ns}.fsds_num").using("iceberg").create()
        subs = sub_df(
            spark,
            [
                {},
                {"adsh": NO_FYE, "form": "S-1", "fye": None},
                {"adsh": EMPTY_FYE, "form": "8-K", "fye": ""},
            ],
        )
        subs.writeTo(f"{ns}.fsds_sub").using("iceberg").create()

        d = drop_counts(spark, ns)
        shares = raw_shares(spark.table(f"{ns}.fsds_num"))

        assert d == {
            "rows": 8,
            "null_value": 2,
            "unparseable_ddate": 1,
            "no_matching_sub": 1,
            "no_fye": 3,
            # distinct rows: NoValue, BadDate, Orphan, NoFye, EmptyFye, Both (not 2+1+1+3 = 7)
            "dropped_any": 6,
        }
        assert shares["segmented"]["rows"] == 0  # '' is not 'segmented'
    finally:
        drop_all(spark, ns)


def test_drop_counts_account_for_every_raw_row_the_job_sees(
    spark: SparkSession, lake_namespace: str
):
    """The DQ query and `prepare_facts` are two implementations of one filter. Every raw row is
    either dropped (dropped_any), a co-registrant row (not curated), or reaches the fiscal step.
    """
    from pipelines.spark.dq_checks import drop_counts
    from pipelines.spark.normalize_facts import prepare_facts

    ns = f"{lake_namespace}_raw"
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {ns}")
    try:
        num = num_df(
            spark,
            [
                {"tag": "Ok"},
                {"tag": "Seg", "segments": "A=B"},
                {"tag": "Coreg", "coreg": "Sub Inc"},
                {"tag": "NoValue", "value": None},
                {"tag": "BadDate", "ddate": 20231399},
                {"tag": "Orphan", "adsh": "0000000000-23-000001"},
                {"tag": "NoFye", "adsh": NO_FYE},
            ],
        )
        subs = sub_df(spark, [{}, {"adsh": NO_FYE, "form": "S-1", "fye": None}])
        num.writeTo(f"{ns}.fsds_num").using("iceberg").create()
        subs.writeTo(f"{ns}.fsds_sub").using("iceberg").create()

        d = drop_counts(spark, ns)
        prepared = prepare_facts(spark.table(f"{ns}.fsds_num"), spark.table(f"{ns}.fsds_sub"))
        reached = prepared.consolidated.count() + prepared.dimensional.count()
        coreg_rows = spark.table(f"{ns}.fsds_num").where("tag = 'Coreg'").count()

        assert (d["rows"], d["dropped_any"], reached, coreg_rows) == (7, 4, 2, 1)
        assert d["rows"] == d["dropped_any"] + reached + coreg_rows
    finally:
        drop_all(spark, ns)
