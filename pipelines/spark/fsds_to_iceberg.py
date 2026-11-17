"""Load FSDS quarters into the raw Iceberg tables (M01 section 3.4).

    uv run python -m pipelines.spark.fsds_to_iceberg --env local --quarters 2023q3,2023q4

Reads `{src}/{quarter}/{sub,num,tag,pre}.txt.gz` (written by `pipelines.fetch.download_fsds`)
with explicit schemas and writes `lake.raw.fsds_{sub,num,tag,pre}` partitioned by `src_quarter`.

* **Validate everything, then write.** For a quarter, all four files are checked first (present,
  the full header line equals the schema's column names in order, every row parses, the row
  count equals the file's line count minus the header). Only if all four pass is anything
  written, so a bad file never leaves a quarter half-replaced.
* **Idempotent.** A quarter's rows are replaced atomically with `overwrite(src_quarter = q)`, so
  a rerun replaces exactly that quarter, other quarters are untouched, and a file with no data
  rows really empties the quarter.
* **Strict reads.** `FAILFAST` with CSV column pruning off (otherwise Spark parses only the
  columns a query uses and silently accepts a malformed `sub` row), no trimming. The files are
  RFC-4180-style: a field containing a quote or a tab is wrapped in `"` with inner quotes doubled
  (`"Vacation ownership interest (""VOI"") inventory"`; measured on 2023q3/q4: hundreds per
  file, and 12 `num` rows whose quoted `segments` holds a tab). So quoting is ON with
  `escape='"'`; turning it off, as the runbook first said, keeps the quotes in the text and
  splits the tab rows.
* **Absent values.** An empty field loads as NULL, including `segments` and `coreg` (the
  consolidated entity). Downstream code treats NULL and '' alike anyway.
* **Local sources only.** `s3://` sources are rejected until the downloader supports S3.
"""

from __future__ import annotations

import argparse
import gzip
import logging
import sys
from pathlib import Path

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from pipelines.common.spark_session import Env, spark_session
from pipelines.spark.iceberg_util import ensure_table
from pipelines.spark.raw_schemas import RAW_COLUMNS, SOURCE_SCHEMAS

log = logging.getLogger(__name__)

TABLES = tuple(SOURCE_SCHEMAS)  # sub, num, tag, pre
DEFAULT_NAMESPACE = "lake.raw"


class RowCountMismatch(RuntimeError):
    """The loaded row count differs from the source file's line count minus the header."""


class HeaderMismatch(RuntimeError):
    """A file's header line is not exactly the expected column names, in order."""


def count_source_rows(path: Path) -> int:
    """Data rows in a gzipped TSV: lines minus the header (a last line needs no final newline)."""
    lines = 0
    last = b"\n"
    with gzip.open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if last != b"\n":
        lines += 1  # final line without a terminating newline
    return max(lines - 1, 0)


def check_header(path: Path, name: str) -> None:
    """The whole header line must equal the schema's column names. Spark only checks the
    columns a query selects, so this guards the ones `raw.fsds_sub` drops as well."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        header = fh.readline().rstrip("\r\n").split("\t")
    expected = SOURCE_SCHEMAS[name].fieldNames()
    if header != expected:
        raise HeaderMismatch(
            f"{path}: header differs from the expected columns "
            f"(got {len(header)} columns, expected {len(expected)}); "
            f"first difference: {_first_difference(header, expected)}"
        )


def _first_difference(got: list[str], want: list[str]) -> str:
    for i, (g, w) in enumerate(zip(got, want, strict=False)):
        if g != w:
            return f"position {i}: got {g!r}, expected {w!r}"
    extra_got, extra_want = got[len(want) :], want[len(got) :]
    return f"position {min(len(got), len(want))}: extra {extra_got!r}, missing {extra_want!r}"


def read_fsds_file(spark: SparkSession, path: Path, name: str) -> DataFrame:
    """Read one source file with its explicit schema; fail on any malformed row or header."""
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found; run `make fsds` for this quarter first")
    check_header(path, name)
    return spark.read.csv(
        str(path),
        schema=SOURCE_SCHEMAS[name],
        sep="\t",
        header=True,
        enforceSchema=False,  # also validate the header row, by position, inside Spark
        mode="FAILFAST",
        quote='"',
        escape='"',  # a doubled quote inside a quoted field is a literal quote
        ignoreLeadingWhiteSpace=False,
        ignoreTrailingWhiteSpace=False,
        timestampFormat="yyyy-MM-dd HH:mm:ss.S",
    )


def raw_frame(spark: SparkSession, src: Path, quarter: str, name: str) -> DataFrame:
    """The raw table's rows for one quarter: source columns plus `src_quarter`, `ingested_at`."""
    df = read_fsds_file(spark, src / quarter / f"{name}.txt.gz", name)
    return df.select(*RAW_COLUMNS[name]).withColumns(
        {"src_quarter": F.lit(quarter), "ingested_at": F.current_timestamp()}
    )


def validated_row_count(spark: SparkSession, src: Path, quarter: str, name: str) -> int:
    """Rows in a source file, after converting EVERY column of EVERY row.

    Spark converts only the columns a query uses, so a bad value in a column that
    `raw.fsds_sub` drops (e.g. `nciks="oops"`) would pass a plain count. Hashing all the columns
    forces every conversion, and FAILFAST turns a bad one into an error.
    """
    full = read_fsds_file(spark, src / quarter / f"{name}.txt.gz", name)
    row = full.agg(F.count(F.lit(1)).alias("n"), F.max(F.hash(*full.columns)).alias("h")).first()
    assert row is not None
    return int(row["n"])


def write_partition(spark: SparkSession, df: DataFrame, table: str, quarter: str) -> None:
    """Create the table if needed (partitioned by src_quarter), then atomically replace this
    quarter's rows. Unlike `overwritePartitions()`, an empty `df` still clears the quarter."""
    ensure_table(spark, table, df, "src_quarter")
    df.writeTo(table).overwrite(F.col("src_quarter") == F.lit(quarter))


def load_quarter(
    spark: SparkSession,
    src: Path,
    quarter: str,
    namespace: str = DEFAULT_NAMESPACE,
) -> dict[str, int]:
    """Validate all four files of a quarter, then load them; return rows written per table."""
    # Phase 1: validate everything before touching the lake.
    frames: dict[str, DataFrame] = {}
    counts: dict[str, int] = {}
    for name in TABLES:
        frame = raw_frame(spark, src, quarter, name)
        loaded = validated_row_count(spark, src, quarter, name)
        expected = count_source_rows(src / quarter / f"{name}.txt.gz")
        if loaded != expected:
            raise RowCountMismatch(
                f"{name} {quarter}: parsed {loaded} rows but the file has {expected} "
                "(lines minus header); blank lines inside the file are skipped by Spark"
            )
        frames[name], counts[name] = frame, loaded

    # Phase 2: write.
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
    for name in TABLES:
        table = f"{namespace}.fsds_{name}"
        write_partition(spark, frames[name], table, quarter)
        log.info("%s %s: %d rows", table, quarter, counts[name])
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load FSDS quarters into raw Iceberg tables.")
    parser.add_argument("--env", choices=["local", "emr"], default="local")
    parser.add_argument("--quarters", required=True, help="comma-separated, e.g. 2023q3,2023q4")
    parser.add_argument("--src", default="data/raw/fsds")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if "://" in args.src:
        print(
            f"error: --src {args.src!r}: only local directories are supported yet "
            "(the downloader has no S3 destination, so there is nothing to read there).",
            file=sys.stderr,
        )
        return 2

    env: Env = args.env
    quarters = [q.strip().lower() for q in args.quarters.split(",") if q.strip()]
    spark = spark_session(env, app_name="fsds_to_iceberg")
    try:
        totals = dict.fromkeys(TABLES, 0)
        for quarter in quarters:
            counts = load_quarter(spark, Path(args.src), quarter, args.namespace)
            print(f"{quarter}: " + ", ".join(f"{n}={counts[n]:,}" for n in TABLES), flush=True)
            for n in TABLES:
                totals[n] += counts[n]
        print("total: " + ", ".join(f"{n}={totals[n]:,}" for n in TABLES))
    except (RowCountMismatch, HeaderMismatch, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        spark.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
