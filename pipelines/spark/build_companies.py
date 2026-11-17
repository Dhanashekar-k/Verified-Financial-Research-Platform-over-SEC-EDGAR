"""Build `curated.companies` (contract section 2.2) from `raw.fsds_sub` and the frozen files.

    uv run python -m pipelines.spark.build_companies --env local [--tickers F] [--sp500 F]

One row per cik, for every cik that either files in `raw.fsds_sub` or appears in the SEC ticker
map (a listed company with no filing in the loaded quarters is kept, with sic and fye NULL).

* `ticker` / `name`: the ticker-map row with the lowest rank (the SEC file's own order) and its
  title; a cik without a ticker takes its latest non-blank `sub.name`. The other tickers of the
  cik (share classes) go to `aliases`; `aliases` is an empty array, never NULL.
* `sic` / `fye_mmdd`: the value on the latest submission (max `filed`, then max `adsh`) where
  that column is non-blank, independently per column: an 8-K with no fye filed after a 10-K must
  not erase the 10-K's fye.
* `in_sp500`: true when any ticker of the cik is in the frozen S&P 500 CSV (tickers compared
  with `.` as `-`, so `BRK.B` matches `BRK-B`), else false. S&P rows that match no ticker are
  returned by `unmatched_sp500` and logged, not dropped silently. Membership is one dated snapshot.
* `in_financebench`: NULL until the FinanceBench documents are loaded (M2/M4).

The whole table is replaced on every run (`createOrReplace`), so reruns are idempotent.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from pathlib import Path

from pyspark.sql import Column, DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import IntegerType, StringType, StructField, StructType

from pipelines.common.spark_session import Env, spark_session
from pipelines.fetch.reference_data import parse_company_tickers
from pipelines.spark.curated_schemas import COMPANIES

log = logging.getLogger(__name__)

_SNAPSHOT_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

RAW_NAMESPACE = "lake.raw"
CURATED_NAMESPACE = "lake.curated"

TICKERS_SCHEMA = StructType(
    [
        StructField("cik", IntegerType()),
        StructField("ticker", StringType()),
        StructField("name", StringType()),
        StructField("rank", IntegerType()),
    ]
)
SP500_SCHEMA = StructType([StructField("ticker", StringType()), StructField("name", StringType())])


def normalize_ticker(ticker: str) -> str:
    """`brk.b` -> `BRK-B`: the SEC ticker map spells share classes with a hyphen."""
    return ticker.strip().upper().replace(".", "-")


def _norm(col: str) -> Column:
    return F.regexp_replace(F.upper(F.trim(F.col(col))), r"\.", "-")


def _blank(col: str) -> Column:
    return F.col(col).isNull() | (F.col(col).cast("string") == "")


def _latest_nonblank(col: str) -> Column:
    """The value of `col` on the latest submission (filed, adsh) where it is not blank."""
    pick = F.struct(F.col("filed"), F.col("adsh"), F.col(col).alias("v"))
    return F.max(F.when(~_blank(col), pick)).getField("v")


def build_companies(sub: DataFrame, tickers: DataFrame, sp500: DataFrame) -> DataFrame:
    """One row per cik with exactly the `COMPANIES` schema (see the module docstring)."""
    from_sub = sub.groupBy("cik").agg(
        _latest_nonblank("name").alias("sub_name"),
        _latest_nonblank("sic").alias("sic"),
        _latest_nonblank("fye").alias("fye_mmdd"),
    )

    in_index = sp500.select(_norm("ticker").alias("_n")).distinct().withColumn("_in", F.lit(True))
    from_tickers = (
        tickers.withColumn("_n", _norm("ticker"))
        .join(in_index, "_n", "left")
        .groupBy("cik")
        .agg(
            F.sort_array(F.collect_list(F.struct("rank", "ticker", "name"))).alias("_t"),
            F.coalesce(F.max("_in"), F.lit(False)).alias("in_sp500"),
        )
        .select(
            "cik",
            F.col("_t")[0]["ticker"].alias("ticker"),
            F.col("_t")[0]["name"].alias("ticker_name"),
            # the other tickers, in rank order, without repeats and never the primary one
            F.expr(
                "array_distinct(transform("
                "filter(_t, x -> x.ticker != _t[0].ticker), x -> x.ticker))"
            ).alias("aliases"),
            "in_sp500",
        )
    )

    universe = sub.select("cik").union(tickers.select("cik")).where(F.col("cik").isNotNull())
    joined = universe.distinct().join(from_sub, "cik", "left").join(from_tickers, "cik", "left")
    out = joined.select(
        "cik",
        F.coalesce("ticker_name", "sub_name").alias("name"),
        "ticker",
        "sic",
        "fye_mmdd",
        F.coalesce("in_sp500", F.lit(False)).alias("in_sp500"),
        F.lit(None).cast("boolean").alias("in_financebench"),
        F.coalesce("aliases", F.array().cast("array<string>")).alias("aliases"),
    )
    return out.select(*[F.col(f.name).cast(f.dataType).alias(f.name) for f in COMPANIES.fields])


def unmatched_sp500(tickers: DataFrame, sp500: DataFrame) -> DataFrame:
    """S&P 500 rows whose ticker is not in the SEC ticker map (they cannot be given a cik)."""
    known = tickers.select(_norm("ticker").alias("_n")).distinct()
    return (
        sp500.withColumn("_n", _norm("ticker"))
        .join(known, "_n", "left_anti")
        .select("ticker", "name")
    )


def run(
    spark: SparkSession,
    tickers_path: Path,
    sp500_path: Path,
    raw_ns: str = RAW_NAMESPACE,
    curated_ns: str = CURATED_NAMESPACE,
) -> dict[str, int]:
    """Rebuild `{curated_ns}.companies` from `raw.fsds_sub` and the two frozen files."""
    ticker_rows = parse_company_tickers(json.loads(tickers_path.read_text()))
    tickers = spark.createDataFrame(
        [(r.cik, r.ticker, r.name, r.rank) for r in ticker_rows], TICKERS_SCHEMA
    )
    with sp500_path.open(newline="") as f:
        sp500 = spark.createDataFrame(
            [(r["ticker"], r["name"]) for r in csv.DictReader(f)], SP500_SCHEMA
        )

    companies = build_companies(spark.table(f"{raw_ns}.fsds_sub"), tickers, sp500)
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {curated_ns}")
    companies.writeTo(f"{curated_ns}.companies").using("iceberg").createOrReplace()

    table = spark.table(f"{curated_ns}.companies")
    unmatched = unmatched_sp500(tickers, sp500).collect()
    for r in unmatched:
        log.warning("S&P 500 ticker %s (%s) is not in the SEC ticker map", r["ticker"], r["name"])
    return {
        "companies": table.count(),
        "in_sp500": table.where("in_sp500").count(),
        "sp500_rows": sp500.count(),
        "sp500_unmatched": len(unmatched),
    }


def warn_if_snapshot_dates_differ(tickers_path: Path, sp500_path: Path) -> None:
    """Warn when the two frozen files carry different dates: membership and tickers then come
    from different days (for example after a `--refresh` where one fetch failed)."""
    a, b = _SNAPSHOT_DATE.search(tickers_path.name), _SNAPSHOT_DATE.search(sp500_path.name)
    if a and b and a.group() != b.group():
        log.warning(
            "the ticker map is from %s but the S&P 500 CSV is from %s", a.group(), b.group()
        )


def _newest(directory: Path, pattern: str) -> Path:
    """The newest frozen snapshot (names embed an ISO date, so the sorted last is newest)."""
    found = sorted(directory.glob(pattern))
    if not found:
        raise FileNotFoundError(f"no {pattern} in {directory}; run pipelines.fetch.reference_data")
    return found[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild curated.companies.")
    parser.add_argument("--env", choices=["local", "emr"], default="local")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--tickers", type=Path, default=None)
    parser.add_argument("--sp500", type=Path, default=None)
    parser.add_argument("--raw-namespace", default=RAW_NAMESPACE)
    parser.add_argument("--curated-namespace", default=CURATED_NAMESPACE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    tickers_path = args.tickers or _newest(args.data_dir, "company_tickers_*.json")
    sp500_path = args.sp500 or _newest(args.data_dir, "sp500_*.csv")
    log.info("tickers: %s; sp500: %s", tickers_path, sp500_path)
    warn_if_snapshot_dates_differ(tickers_path, sp500_path)

    env: Env = args.env
    spark = spark_session(env, app_name="build_companies")
    try:
        counts = run(spark, tickers_path, sp500_path, args.raw_namespace, args.curated_namespace)
    finally:
        spark.stop()
    for name, n in counts.items():
        log.info("%s: %d", name, n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
