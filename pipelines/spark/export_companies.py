"""Export the CIKs the filing fetcher should list (M02 section 3.1).

    uv run python -m pipelines.spark.export_companies --env local
        [--dest data/targets/tracked_ciks.csv]

Writes `cik,ticker,name` for every `curated.companies` row with `in_sp500 = true`, sorted by cik,
atomically, so `pipelines.fetch.list_filings` (which must not import Spark) reads a plain CSV.
FinanceBench-only companies are not listed: their documents are PDFs, not EDGAR HTML.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import tempfile
from pathlib import Path

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from pipelines.common.spark_session import Env, spark_session

log = logging.getLogger(__name__)

CURATED_NAMESPACE = "lake.curated"
DEFAULT_DEST = Path("data/targets/tracked_ciks.csv")


def export_tracked_ciks(spark: SparkSession, curated_ns: str, dest: Path) -> int:
    """Write the tracked-CIK CSV and return the number of rows."""
    rows = (
        spark.table(f"{curated_ns}.companies")
        .where(F.col("in_sp500"))
        .select("cik", "ticker", "name")
        .dropDuplicates(["cik"])  # contract: one row per cik; defensive for a hand-edited table
        .orderBy("cik")
        .collect()
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(["cik", "ticker", "name"])
            for r in rows:
                writer.writerow([r.cik, r.ticker or "", r.name or ""])
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export tracked CIKs from curated.companies.")
    parser.add_argument("--env", choices=["local", "emr"], default="local")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--curated-namespace", default=CURATED_NAMESPACE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    env: Env = args.env
    spark = spark_session(env, app_name="export_companies")
    try:
        n = export_tracked_ciks(spark, args.curated_namespace, args.dest)
    finally:
        spark.stop()
    log.info("wrote %d CIKs to %s", n, args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
