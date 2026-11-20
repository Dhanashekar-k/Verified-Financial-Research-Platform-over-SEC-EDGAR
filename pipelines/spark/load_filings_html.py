"""Load the fetcher's manifest into `raw.filings_html` (M02 section 3.2; contract 2.1).

    uv run python -m pipelines.spark.load_filings_html --env local
        [--manifest data/raw/filings/_manifest.jsonl] [--namespace lake.raw]

The JSONL is parsed with the explicit `FILINGS_HTML` schema in FAILFAST mode, so a malformed row
fails the load instead of loading NULLs. Rows are merged on `accession`: a rerun changes nothing,
and a retried fetch replaces its earlier (failed) row.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from pipelines.common.object_store import open_store
from pipelines.common.spark_session import Env, spark_session
from pipelines.spark.iceberg_util import ensure_table
from pipelines.spark.raw_schemas import FILINGS_HTML

log = logging.getLogger(__name__)

RAW_NAMESPACE = "lake.raw"
DEFAULT_MANIFEST = Path("data/raw/filings/_manifest.jsonl")


def read_manifest_lines(manifest: Path | str) -> list[str]:
    """The manifest's JSON lines, from a local path or an s3:// URI.

    Read on the driver, not with `spark.read.json`: Spark treats a file whose name starts with `_`
    as hidden and reads nothing from it, and the fetcher's manifest is `_manifest.jsonl` (so the
    parse job's `*/*.htm.gz` glob never matches it).
    """
    location = str(manifest)
    if location.startswith("s3://"):
        parent, _, name = location.rpartition("/")
        text = open_store(parent).get(name).decode("utf-8")
    else:
        text = Path(location).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def load_filings_html(spark: SparkSession, manifest: Path | str, table: str) -> int:
    """Merge the manifest into `table` on accession; return the manifest's row count."""
    lines = read_manifest_lines(manifest)
    raw = spark.createDataFrame([(line,) for line in lines], "value string")
    # FAILFAST: a malformed line raises instead of becoming a row of NULLs.
    df = raw.select(F.from_json("value", FILINGS_HTML, {"mode": "FAILFAST"}).alias("r")).select(
        "r.*"
    )
    n = df.count()  # forces the parse before anything is written
    if n != len(lines):
        raise ValueError(f"{manifest}: {len(lines)} lines but {n} rows parsed")
    ensure_table(spark, table, df, "form")
    view = f"_manifest_{uuid.uuid4().hex}"
    df.createOrReplaceTempView(view)
    try:
        spark.sql(
            f"MERGE INTO {table} t USING {view} s ON t.accession = s.accession "
            "WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *"
        )
    finally:
        spark.catalog.dropTempView(view)
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the filings manifest into raw.filings_html.")
    parser.add_argument("--env", choices=["local", "emr", "glue"], default="local")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--namespace", default=RAW_NAMESPACE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    env: Env = args.env
    spark = spark_session(env, app_name="load_filings_html")
    try:
        spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {args.namespace}")
        n = load_filings_html(spark, args.manifest, f"{args.namespace}.filings_html")
    finally:
        spark.stop()
    log.info("merged %d manifest rows into %s.filings_html", n, args.namespace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
