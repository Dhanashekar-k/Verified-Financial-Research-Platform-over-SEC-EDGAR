"""Spark implementation of fact version selection (M01 section 3.5, "Dedup and restatements").

Implemented by Claude at Shekar's explicit request (M1 session, 2026-11-17); no longer
hook-protected.

The pure-Python twin is `select_versions_ref` in `pipelines/common/fiscal_ref.py`; the
differential test asserts the two agree.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F


def select_versions(df: DataFrame) -> DataFrame:
    """Turn `facts_history` rows into `curated.facts` rows (latest + original per fact_key).

    Input: one row per (fact_key, accession), with at least these columns, plus any
    pass-through columns (which must be preserved):
        fact_key bigint, fact_id bigint, period_end date, value decimal(28,4),
        accession string, form string, filed date, is_amendment boolean,
        filing_period date   -- sub.period of the accession: the filing's own reporting period

    Output: the input columns plus
        is_latest boolean, is_original boolean, n_versions int, is_restated boolean

    Latest = max(filed), ties by max(accession).
    Original = among rows whose filing_period == period_end, the earliest filed (ties: smallest
    accession); if none match, the earliest filed overall (ties: smallest accession).
    One row per fact_key, with is_latest and is_original both true, when the original and the
    latest have the same value. Otherwise two rows: the latest (is_latest only) and the
    original (is_original only). n_versions counts the rows of the group in the input;
    is_restated is true when the group has more than one distinct value.
    """
    by_key = Window.partitionBy("fact_key")

    # A version is "native" when the filing's own reporting period is the fact's period.
    # eqNullSafe + coalesce mirror Python's `==` (a null filing_period is simply not native).
    native = F.coalesce(F.col("filing_period").eqNullSafe(F.col("period_end")), F.lit(False))
    staged = (
        df.withColumn("_native", native)
        .withColumn("_has_native", F.max(F.col("_native").cast("int")).over(by_key) == 1)
        .withColumn("n_versions", F.count(F.lit(1)).over(by_key).cast("int"))
        # distinct values INCLUDING null (Python: len({v.value ...}) > 1); count_distinct skips null
        .withColumn(
            "is_restated",
            (
                F.size(F.collect_set("value").over(by_key))
                + F.max(F.col("value").isNull().cast("int")).over(by_key)
            )
            > 1,
        )
    )

    # Latest: newest filed, ties -> largest accession. fact_id only makes the order total.
    # A derived Q4 takes its FY parent's accession, so it can share (fact_key, accession, fact_id)
    # with a real quarter row. The order must still be total, or reruns differ: the real row (no
    # `derived_from`) comes first, then the value. This makes the choice repeatable; it does not
    # fix the collision itself.
    tie = [F.col("value").asc_nulls_last()]
    if "derived_from" in df.columns:
        tie = [F.col("derived_from").isNotNull().asc(), *tie]
    latest_order = by_key.orderBy(
        F.col("filed").desc(), F.col("accession").desc(), F.col("fact_id").desc(), *tie
    )
    # Original: native rows first (only if the group has any), then oldest filed, then smallest
    # accession.
    original_order = by_key.orderBy(
        F.when(F.col("_has_native") & ~F.col("_native"), 1).otherwise(0).asc(),
        F.col("filed").asc(),
        F.col("accession").asc(),
        F.col("fact_id").asc(),
        *tie,
    )

    ranked = (
        staged.withColumn("_is_latest", F.row_number().over(latest_order) == 1)
        .withColumn("_is_original", F.row_number().over(original_order) == 1)
        # `first` over an ordered window sees its first row in every frame, so each row learns the
        # latest/original value null-safely (max() would skip a null value).
        .withColumn("_latest_value", F.first("value").over(latest_order))
        .withColumn("_original_value", F.first("value").over(original_order))
    )

    collapsed = F.col("_latest_value").eqNullSafe(F.col("_original_value"))
    selected = ranked.where(
        (F.col("_is_original"))  # the original always survives
        | (F.col("_is_latest") & ~collapsed)  # the latest only when it differs from the original
    )
    return selected.select(
        *df.columns,
        (F.col("_is_latest") | collapsed & F.col("_is_original")).alias("is_latest"),
        F.col("_is_original").alias("is_original"),
        "n_versions",
        "is_restated",
    )
