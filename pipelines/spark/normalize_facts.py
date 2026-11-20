"""Normalize raw FSDS numbers into the curated fact tables (M01 section 3.5).

    uv run python -m pipelines.spark.normalize_facts --env local

On AWS Glue the same `run()` is started by `pipelines/glue/normalize_facts.py` (`--env glue`).

This module is the ORCHESTRATION only: read, join, clean, split, call the two fiscal functions,
write. The interview-critical logic lives elsewhere and is not implemented here:

* `pipelines.spark.fiscal.derive_fiscal_columns`  (fiscal year/period, label offset, Q4)
* `pipelines.spark.versions.select_versions`      (latest and original versions)

Always a full rebuild from all of the input it is given (all of `raw.fsds_*`, or the subset named
by `--ciks`, `--limit-filings` and `--fiscal-years`; a subset belongs in its own namespaces, never
in `lake.curated`): a fact's fiscal year can differ from the quarter it was filed in, and
`overwritePartitions()` replaces whole `fiscal_year` partitions, so a partial input would silently
drop facts from partitions it touches. Incremental updates are M10 (MERGE), not this job.

What it does to the rows before the fiscal step:

* joins `num` to `sub` on `adsh` (fye, form, period, fy and filed come from the SAME submission);
* drops rows with a NULL `value` or an unparseable `ddate` (kept in `raw.*`);
* drops rows whose submission has no `fye` (S-1, F-1, 8-K and the like; kept in `raw.*`);
* `taxonomy` = year-stripped family of `version` (`us-gaap/2023` -> `us-gaap`), or `custom` when
  `version == adsh`;
* splits: consolidated (`coreg` and `segments` empty) -> `facts`; `segments` set and `coreg`
  empty -> `facts_dimensional`; co-registrant rows (`coreg` set) are not curated;
* collapses rows that would share one `(fact_key, accession)` (a tag reported under two taxonomy
  years inside one filing; the same filing present in two source quarters), keeping the latest
  taxonomy year and then the latest source quarter.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from pyspark.sql import Column, DataFrame, SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql.types import StructType

from pipelines.common.spark_session import Env, spark_session
from pipelines.spark.curated_schemas import FACTS, FACTS_DIMENSIONAL, FACTS_HISTORY
from pipelines.spark.fiscal import derive_fiscal_columns
from pipelines.spark.iceberg_util import ensure_table
from pipelines.spark.versions import select_versions

log = logging.getLogger(__name__)

RAW_NAMESPACE = "lake.raw"
CURATED_NAMESPACE = "lake.curated"

# Columns handed to derive_fiscal_columns (see its docstring); `segments` is added for the
# dimensional branch.
FISCAL_INPUT_COLUMNS = (
    "cik", "adsh", "tag", "taxonomy", "is_custom", "uom", "qtrs", "period_end", "value",
    "form", "filed", "fye_mmdd", "sub_fy", "sub_period",
)  # fmt: skip


def blank(name: str) -> Column:
    """True when a text column is NULL or empty (raw files load empty fields as NULL)."""
    return F.col(name).isNull() | (F.col(name) == "")


def yyyymmdd(name: str) -> Column:
    """An int like 20230930 as a date; NULL if it does not parse."""
    return F.to_date(F.col(name).cast("string"), "yyyyMMdd")


@dataclass(frozen=True)
class Prepared:
    consolidated: DataFrame
    dimensional: DataFrame


def prepare_facts(num: DataFrame, sub: DataFrame) -> Prepared:
    """Join, clean and split the raw rows. Pure DataFrame logic, no Shekar-owned code."""
    latest_sub = Window.partitionBy("adsh").orderBy(
        F.col("src_quarter").desc(), F.col("ingested_at").desc()
    )
    subs = (
        sub.withColumn("_rn", F.row_number().over(latest_sub))
        .where(F.col("_rn") == 1)
        .select(
            "adsh",
            "cik",
            F.col("fye").alias("fye_mmdd"),
            "form",
            yyyymmdd("filed").alias("filed"),
            F.col("fy").alias("sub_fy"),
            yyyymmdd("period").alias("sub_period"),
        )
        # Rule 1 needs the submission's fye. Registration statements and 8-Ks can have none; their
        # facts stay in raw.* rather than reaching the fiscal step, which cannot place them.
        .where(~blank("fye_mmdd"))
    )

    rows = (
        num.where(F.col("value").isNotNull())
        .withColumn("period_end", yyyymmdd("ddate"))
        .where(F.col("period_end").isNotNull())
        .withColumn("is_custom", F.col("version") == F.col("adsh"))
        .withColumn(
            "taxonomy",
            F.when(F.col("is_custom"), F.lit("custom")).otherwise(
                F.regexp_extract(F.col("version"), r"^([^/]+)", 1)
            ),
        )
        .withColumn("segments", F.when(blank("segments"), F.lit(None)).otherwise(F.col("segments")))
        .withColumn("coreg", F.when(blank("coreg"), F.lit(None)).otherwise(F.col("coreg")))
    )

    # One row per natural key after year-stripping: latest taxonomy year, then latest quarter.
    key = ["adsh", "tag", "taxonomy", "ddate", "qtrs", "uom", "segments", "coreg"]
    pick = Window.partitionBy(*key).orderBy(F.col("version").desc(), F.col("src_quarter").desc())
    deduped = rows.withColumn("_rn", F.row_number().over(pick)).where(F.col("_rn") == 1).drop("_rn")

    joined = deduped.join(subs, "adsh", "inner")
    consolidated = joined.where(F.col("coreg").isNull() & F.col("segments").isNull())
    dimensional = joined.where(F.col("coreg").isNull() & F.col("segments").isNotNull())
    return Prepared(
        consolidated=consolidated.select(*FISCAL_INPUT_COLUMNS),
        dimensional=dimensional.select(*FISCAL_INPUT_COLUMNS, "segments"),
    )


def with_history_columns(derived: DataFrame) -> DataFrame:
    """Add the columns `facts_history` has that the fiscal step does not produce."""
    return derived.withColumns(
        {
            "accession": F.col("adsh"),
            "is_amendment": F.col("form").endswith("/A"),
            "source": F.lit("fsds"),
            # select_versions needs the filing's own reporting period (sub.period).
            "filing_period": F.col("sub_period"),
        }
    )


def conform(df: DataFrame, schema: StructType) -> DataFrame:
    """Select and cast to exactly the contract's columns. A missing column fails loudly."""
    return df.select(*[F.col(f.name).cast(f.dataType).alias(f.name) for f in schema.fields])


def write_curated(spark: SparkSession, df: DataFrame, table: str, schema: StructType) -> int:
    """Create the table if needed (partitioned by fiscal_year) and REPLACE ITS WHOLE CONTENT.

    This job is a full rebuild. `overwritePartitions()` would leave behind any `fiscal_year`
    partition the new output no longer contains (for example after a fiscal-rule fix moves
    facts from 2022 to 2021), and `facts` would then hold the same fact twice.
    """
    out = conform(df, schema)
    ensure_table(spark, table, out, "fiscal_year")
    out.writeTo(table).overwrite(F.lit(True))
    return spark.table(table).count()


def subset_input(
    num: DataFrame,
    sub: DataFrame,
    ciks: Sequence[int] = (),
    limit_filings: int | None = None,
) -> tuple[DataFrame, DataFrame]:
    """Restrict the raw input to some companies and/or the first N submissions (by accession).

    Cutting at the INPUT keeps every fiscal rule intact for what remains: a company's facts are
    all-or-nothing, so label offsets and Q4 derivation see the same rows they would in a full run.
    """
    if ciks:
        sub = sub.where(F.col("cik").isin([int(c) for c in ciks]))
    if limit_filings is not None:
        first = sub.select("adsh").distinct().orderBy("adsh").limit(limit_filings)
        sub = sub.join(first, "adsh", "left_semi")
    return num.join(sub.select("adsh").distinct(), "adsh", "left_semi"), sub


def only_fiscal_years(df: DataFrame, fiscal_years: Sequence[int]) -> DataFrame:
    """Keep these fiscal years (after derivation, so comparatives are assigned first)."""
    if not fiscal_years:
        return df
    return df.where(F.col("fiscal_year").isin([int(y) for y in fiscal_years]))


def run(
    spark: SparkSession,
    raw_ns: str = RAW_NAMESPACE,
    curated_ns: str = CURATED_NAMESPACE,
    ciks: Sequence[int] = (),
    fiscal_years: Sequence[int] = (),
    limit_filings: int | None = None,
) -> dict[str, int]:
    """Rebuild facts_history, facts and facts_dimensional from raw.*; return row counts.

    `ciks`, `fiscal_years` and `limit_filings` make a smoke subset; leave them empty for the real
    full rebuild.
    """
    num, sub = subset_input(
        spark.table(f"{raw_ns}.fsds_num"),
        spark.table(f"{raw_ns}.fsds_sub"),
        ciks,
        limit_filings,
    )
    prepared = prepare_facts(num, sub)
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {curated_ns}")

    # Consolidated branch: fiscal columns -> history -> latest/original selection -> facts.
    history = only_fiscal_years(
        with_history_columns(derive_fiscal_columns(prepared.consolidated)), fiscal_years
    )
    facts = select_versions(history).withColumn("anomaly_score", F.lit(None).cast("float"))
    counts = {
        "facts_history": write_curated(
            spark, history, f"{curated_ns}.facts_history", FACTS_HISTORY
        ),
        "facts": write_curated(spark, facts, f"{curated_ns}.facts", FACTS),
    }

    # Dimensional branch: same steps, series identified by `segments` as well.
    dim_history = only_fiscal_years(
        with_history_columns(
            derive_fiscal_columns(prepared.dimensional, extra_key_cols=["segments"])
        ),
        fiscal_years,
    )
    counts["facts_dimensional"] = write_curated(
        spark,
        select_versions(dim_history),
        f"{curated_ns}.facts_dimensional",
        FACTS_DIMENSIONAL,
    )
    for name, n in counts.items():
        log.info("%s.%s: %d rows", curated_ns, name, n)
    return counts


def _ints(csv: str) -> list[int]:
    return [int(x) for x in csv.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild curated facts from raw FSDS tables.")
    parser.add_argument("--env", choices=["local", "emr", "glue"], default="local")
    parser.add_argument("--raw-namespace", default=RAW_NAMESPACE)
    parser.add_argument("--curated-namespace", default=CURATED_NAMESPACE)
    parser.add_argument("--ciks", default="", help="comma-separated CIKs (smoke subset)")
    parser.add_argument("--fiscal-years", default="", help="comma-separated years (smoke subset)")
    parser.add_argument("--limit-filings", type=int, default=None, help="first N submissions")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    env: Env = args.env
    spark = spark_session(env, app_name="normalize_facts")
    try:
        counts = run(
            spark,
            args.raw_namespace,
            args.curated_namespace,
            ciks=_ints(args.ciks),
            fiscal_years=_ints(args.fiscal_years),
            limit_filings=args.limit_filings,
        )
    finally:
        spark.stop()
    for name, n in counts.items():
        print(f"{name}: {n:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
