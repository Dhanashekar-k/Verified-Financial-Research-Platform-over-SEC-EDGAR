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
* **Local or S3 sources.** `--src` is a directory or an `s3://bucket/prefix` with the same
  `{quarter}/{name}.txt.gz` layout. The header and line-count checks stream the object through
  `gzip` either way, so the validation is identical in both places.
* **Parallel quarters, spread writes (cloud).** A `.gz` file is not splittable, so Spark parses it
  in ONE task and nothing can speed that task up. Two things keep a cluster busy instead:
  `--parallel N` loads N quarters at once (each quarter's checks and writes are independent
  partition overwrites; Iceberg's commit retries, raised in `ensure_table`, absorb collisions), and
  `--repartition N` shuffles the two big tables (`num`, `pre`) right after the read so the write
  is not one task. Defaults (1 and off) keep laptop runs exactly as they were.
* **Subset (smoke runs).** `--ciks 320193` still validates the WHOLE files, then writes only those
  companies: their `sub` rows, and the `num`/`pre` rows of their submissions (`tag` has no company
  and is kept whole). Use it with a separate `--namespace`: a subset in `lake.raw` would look like
  a full load.
"""

from __future__ import annotations

import argparse
import gzip
import logging
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import IO, Any

import boto3
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


Location = Path | str  # a local path, or an `s3://bucket/key` URI


def is_s3(location: Location) -> bool:
    return str(location).startswith("s3://")


def join_location(base: Location, *parts: str) -> str:
    """`base/part/...` as a string (a `Path` would collapse the `//` of an `s3://` URI)."""
    return "/".join([str(base).rstrip("/"), *parts])


def _s3_object(uri: str) -> tuple[Any, str, str]:
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return boto3.client("s3"), bucket, key


def source_exists(location: Location) -> bool:
    if not is_s3(location):
        return Path(location).is_file()
    client, bucket, key = _s3_object(str(location))
    try:
        client.head_object(Bucket=bucket, Key=key)
    except client.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return True


def open_gzip(location: Location) -> IO[bytes]:
    """The decompressed bytes of a `.gz` file, streamed from disk or S3."""
    if not is_s3(location):
        return gzip.open(location, "rb")
    client, bucket, key = _s3_object(str(location))
    return gzip.open(client.get_object(Bucket=bucket, Key=key)["Body"], "rb")


def count_source_rows(path: Location) -> int:
    """Data rows in a gzipped TSV: lines minus the header (a last line needs no final newline)."""
    lines = 0
    last = b"\n"
    with open_gzip(path) as fh:
        while chunk := fh.read(1024 * 1024):
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if last != b"\n":
        lines += 1  # final line without a terminating newline
    return max(lines - 1, 0)


def check_header(path: Location, name: str) -> None:
    """The whole header line must equal the schema's column names. Spark only checks the
    columns a query selects, so this guards the ones `raw.fsds_sub` drops as well."""
    with open_gzip(path) as fh:
        header = fh.readline().decode("utf-8").rstrip("\r\n").split("\t")
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


def read_fsds_file(spark: SparkSession, path: Location, name: str) -> DataFrame:
    """Read one source file with its explicit schema; fail on any malformed row or header."""
    if not source_exists(path):
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


def source_file(src: Location, quarter: str, name: str) -> str:
    return join_location(src, quarter, f"{name}.txt.gz")


BIG_TABLES = ("num", "pre")  # millions of rows per quarter; sub and tag are small


def raw_frame(
    spark: SparkSession,
    src: Location,
    quarter: str,
    name: str,
    repartition: int | None = None,
) -> DataFrame:
    """The raw table's rows for one quarter: source columns plus `src_quarter`, `ingested_at`.

    `repartition` (big tables only) spreads the rows over that many tasks right after the
    single-task gzip read, so the write and anything after it use the whole cluster.
    """
    df = read_fsds_file(spark, source_file(src, quarter, name), name)
    if repartition and name in BIG_TABLES:
        df = df.repartition(repartition)
    return df.select(*RAW_COLUMNS[name]).withColumns(
        {"src_quarter": F.lit(quarter), "ingested_at": F.current_timestamp()}
    )


def validated_row_count(spark: SparkSession, src: Location, quarter: str, name: str) -> int:
    """Rows in a source file, after converting EVERY column of EVERY row.

    Spark converts only the columns a query uses, so a bad value in a column that
    `raw.fsds_sub` drops (e.g. `nciks="oops"`) would pass a plain count. Hashing all the columns
    forces every conversion, and FAILFAST turns a bad one into an error.
    """
    full = read_fsds_file(spark, source_file(src, quarter, name), name)
    row = full.agg(F.count(F.lit(1)).alias("n"), F.max(F.hash(*full.columns)).alias("h")).first()
    assert row is not None
    return int(row["n"])


def write_partition(spark: SparkSession, df: DataFrame, table: str, quarter: str) -> None:
    """Create the table if needed (partitioned by src_quarter), then atomically replace this
    quarter's rows. Unlike `overwritePartitions()`, an empty `df` still clears the quarter."""
    ensure_table(spark, table, df, "src_quarter")
    df.writeTo(table).overwrite(F.col("src_quarter") == F.lit(quarter))


def subset_to_ciks(frames: dict[str, DataFrame], ciks: Sequence[int]) -> dict[str, DataFrame]:
    """Keep only these companies: their `sub` rows and the `num`/`pre` rows of their submissions.

    `tag` is the taxonomy's tag dictionary (no company), so it stays whole.
    """
    sub = frames["sub"].where(F.col("cik").isin([int(c) for c in ciks]))
    adsh = sub.select("adsh").distinct()
    return {
        "sub": sub,
        "num": frames["num"].join(adsh, "adsh", "left_semi"),
        "pre": frames["pre"].join(adsh, "adsh", "left_semi"),
        "tag": frames["tag"],
    }


def load_quarter(
    spark: SparkSession,
    src: Location,
    quarter: str,
    namespace: str = DEFAULT_NAMESPACE,
    ciks: Sequence[int] | None = None,
    repartition: int | None = None,
) -> dict[str, int]:
    """Validate all four files of a quarter, then load them; return rows written per table.

    With `ciks`, the files are still validated whole, but only those companies are written.
    """
    # Phase 1: validate everything before touching the lake.
    frames: dict[str, DataFrame] = {}
    counts: dict[str, int] = {}
    for name in TABLES:
        frame = raw_frame(spark, src, quarter, name, repartition)
        loaded = validated_row_count(spark, src, quarter, name)
        expected = count_source_rows(source_file(src, quarter, name))
        if loaded != expected:
            raise RowCountMismatch(
                f"{name} {quarter}: parsed {loaded} rows but the file has {expected} "
                "(lines minus header); blank lines inside the file are skipped by Spark"
            )
        frames[name], counts[name] = frame, loaded

    if ciks:
        frames = subset_to_ciks(frames, ciks)

    # Phase 2: write.
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
    for name in TABLES:
        table = f"{namespace}.fsds_{name}"
        write_partition(spark, frames[name], table, quarter)
        if ciks:
            counts[name] = spark.table(table).where(F.col("src_quarter") == quarter).count()
        log.info("%s %s: %d rows", table, quarter, counts[name])
    return counts


def load_quarters(
    spark: SparkSession,
    src: Location,
    quarters: Sequence[str],
    namespace: str = DEFAULT_NAMESPACE,
    ciks: Sequence[int] | None = None,
    repartition: int | None = None,
    parallel: int = 1,
) -> dict[str, dict[str, int]]:
    """Load several quarters, `parallel` at a time; return rows written per table per quarter.

    Quarters are independent (each replaces only its own `src_quarter` partition), so any order
    gives the same tables. The first failure is raised after the running quarters finish; quarters
    that already committed stay (a rerun replaces them), which is the idempotence the job promises.
    """

    def one(quarter: str) -> dict[str, int]:
        return load_quarter(spark, src, quarter, namespace, ciks, repartition)

    if parallel <= 1 or len(quarters) <= 1:
        return {q: one(q) for q in quarters}
    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="fsds") as pool:
        futures = {q: pool.submit(one, q) for q in quarters}
    return {q: f.result() for q, f in futures.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load FSDS quarters into raw Iceberg tables.")
    parser.add_argument("--env", choices=["local", "emr", "glue"], default="local")
    parser.add_argument("--quarters", required=True, help="comma-separated, e.g. 2023q3,2023q4")
    parser.add_argument("--src", default="data/raw/fsds", help="a directory or s3://bucket/prefix")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument(
        "--ciks",
        default="",
        help="comma-separated CIKs to load (smoke runs; use another --namespace)",
    )
    parser.add_argument("--parallel", type=int, default=1, help="quarters loaded at once")
    parser.add_argument(
        "--repartition", type=int, default=0, help="spread num/pre over N tasks after the read"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    env: Env = args.env
    ciks = [int(c) for c in args.ciks.split(",") if c.strip()]
    quarters = [q.strip().lower() for q in args.quarters.split(",") if q.strip()]
    spark = spark_session(env, app_name="fsds_to_iceberg")
    try:
        totals = dict.fromkeys(TABLES, 0)
        loaded = load_quarters(
            spark,
            args.src,
            quarters,
            args.namespace,
            ciks,
            repartition=args.repartition or None,
            parallel=args.parallel,
        )
        for quarter, counts in loaded.items():
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
