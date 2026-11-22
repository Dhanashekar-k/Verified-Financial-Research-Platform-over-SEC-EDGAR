"""Export a versioned serving snapshot of the lake (M03 section 3.2, contract section 2.3).

Reads `curated.facts`, `chunks`, `chunk_embeddings` and `companies` at their LATEST Iceberg
snapshots (the snapshot ids go into the manifest, so any eval run can be reproduced by time travel),
filters them to the serving scope and writes zstd Parquet plus `_MANIFEST.json` under
`<export_root>/corpus_version=N/`. Nothing is written unless every check passes: an in-scope chunk
without an embedding (or an embedding that is not 384-d) raises `ExportError` first.

`N` comes from `curated.corpus_versions`: `max + 1`, or the current max again to re-export it
(the table directory is overwritten, never appended to, so a rerun is idempotent).
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pipelines.common.embedding_artifact import (
    ARTIFACT_HF_REPO,
    ARTIFACT_HF_REVISION,
    ARTIFACT_ONNX_FILE,
    ARTIFACT_ONNX_SHA256,
    DIM,
    MODEL_NAME,
)
from pipelines.common.manifest import (
    MANIFEST_NAME,
    ChunkExclusions,
    EmbeddingArtifact,
    EmbeddingScope,
    Manifest,
    ManifestFile,
    NotEmbedded,
)
from pipelines.common.object_store import S3Store
from pipelines.common.spark_session import Env, spark_session


def _ensure_distutils() -> None:
    """PySpark 3.5's pandas support imports `distutils` (removed in Python 3.12); see fiscal.py."""
    try:
        importlib.import_module("distutils")
    except ModuleNotFoundError:
        importlib.import_module("setuptools")


_ensure_distutils()

from pyspark.sql import DataFrame, SparkSession  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402

from pipelines.spark.curated_schemas import CORPUS_VERSIONS  # noqa: E402
from pipelines.spark.iceberg_util import latest_snapshot_id  # noqa: E402
from pipelines.spark.serving_scope import (  # noqa: E402
    NOT_EMBEDDED_RULE,
    SERVED_RULE,
    not_embedded_predicate,
    served_predicate,
)

log = logging.getLogger(__name__)

NAMESPACE = "lake.curated"
MIN_TOKENS_EXCLUSIVE = (
    5  # chunks with n_tokens <= 5 are embedded in the lake but not exported (ADR-035)
)
TAXONOMIES = ("us-gaap", "dei", "srt")
UOMS = ("USD", "shares", "USD/shares", "pure")
ROWS_PER_FILE = {"facts": 2_000_000, "chunks": 100_000, "companies": 1_000_000}

FACT_COLUMNS = [
    "fact_id", "fact_key", "cik", "tag", "taxonomy", "uom", "qtrs", "period_start", "period_end",
    "fiscal_year", "fiscal_period", "calendar_frame", "value", "accession", "form", "filed",
    "is_latest", "is_original", "is_restated", "anomaly_score", "derived_from",
]  # fmt: skip
CHUNK_COLUMNS = [
    "chunk_id", "cik", "accession", "form", "fiscal_year", "fiscal_period", "section",
    "section_title", "chunk_index", "text", "n_tokens", "is_table", "source_url", "anchor", "page",
    "doc_source", "tenant_id", "embedding",
]  # fmt: skip
COMPANY_COLUMNS = ["cik", "name", "ticker", "sic", "fye_mmdd", "aliases"]


class ExportError(Exception):
    """The lake does not satisfy the export contract; nothing was written."""


def _read(spark: SparkSession, table: str, snapshot_id: int) -> DataFrame:
    return spark.read.option("snapshot-id", snapshot_id).format("iceberg").load(table)


def _snapshot(spark: SparkSession, table: str) -> int:
    sid = latest_snapshot_id(spark, table)
    if sid is None:
        raise ExportError(f"{table} has no Iceberg snapshot (missing or empty table)")
    return sid


def _next_version(spark: SparkSession, versions_table: str, requested: int | None) -> int:
    if not spark.catalog.tableExists(versions_table):
        current = 0
    else:
        row = spark.table(versions_table).agg(F.max("corpus_version").alias("m")).first()
        current = int(row.m) if row is not None and row.m is not None else 0
    if requested is None:
        return current + 1
    if requested not in {current, current + 1} or requested < 1:
        raise ValueError(
            f"version must be {current + 1} (new) or {current} (re-export of the latest), "
            f"got {requested}"
        )
    return requested


def normalize_export_root(export_root: str) -> str:
    """The export prefix, validated: `s3://<bucket>/<key>` or an absolute local path.

    Anything else (a bare `s3://`, `s3a://`, `file:` URIs, a relative path, an empty string) is
    refused BEFORE any write: Spark's overwrite mode deletes the target directory, so a prefix
    that resolves to the wrong place must never reach it.
    """
    root = export_root.strip()
    if root.startswith("s3://"):
        bucket, _, key = root[len("s3://") :].partition("/")
        key = key.strip("/")
        if not bucket or not key:
            raise ValueError(
                f"export root {export_root!r} needs a bucket and a non-empty key prefix"
            )
        return f"s3://{bucket}/{key}"
    if "://" in root or root.startswith("file:"):
        raise ValueError(
            f"export root {export_root!r}: only s3:// or an absolute local path is allowed"
        )
    path = Path(root)
    if not root or not path.is_absolute() or len(path.parts) < 2:
        raise ValueError(f"export root {export_root!r} must be an absolute local path below /")
    return str(path)


def _delete_text(location: str) -> None:
    """Remove a small file written by `_put_text` (no error if it is absent)."""
    if location.startswith("s3://"):
        import boto3

        bucket, _, key = location[len("s3://") :].partition("/")
        boto3.client("s3").delete_object(Bucket=bucket, Key=key)
        return
    Path(location).unlink(missing_ok=True)


def _put_text(location: str, text: str) -> None:
    if location.startswith("s3://"):
        bucket, _, key = location[len("s3://") :].partition("/")
        S3Store(bucket).put(key, text.encode("utf-8"))
        return
    path = Path(location)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_table(spark: SparkSession, df: DataFrame, name: str, out: str) -> list[ManifestFile]:
    """Write `df` as zstd Parquet under `<out>/<name>/` (overwriting it); list the files written."""
    path = f"{out}/{name}"
    df = df.persist()
    try:
        n_rows = df.count()
        parts = max(1, math.ceil(n_rows / ROWS_PER_FILE[name]))
        (
            df.repartition(parts)
            .write.mode("overwrite")
            .option("compression", "zstd")
            .option("maxRecordsPerFile", ROWS_PER_FILE[name])
            .parquet(path)
        )
    finally:
        df.unpersist()
    files = _list_parquet(spark, out, name)
    written = sum(f.rows for f in files)
    if written != n_rows:
        raise ExportError(f"{name}: wrote {written} rows in {len(files)} files, expected {n_rows}")
    return files


def _list_parquet(spark: SparkSession, out: str, name: str) -> list[ManifestFile]:
    """Every Parquet file under `<out>/<name>/` with its size and the row count of its footer."""
    jvm = spark._jvm  # noqa: SLF001 - Hadoop FileSystem API: works for file:// and s3://
    hadoop_path = jvm.org.apache.hadoop.fs.Path(f"{out}/{name}")
    fs = hadoop_path.getFileSystem(spark._jsc.hadoopConfiguration())  # noqa: SLF001
    sizes: dict[str, int] = {}
    for status in fs.listStatus(hadoop_path):
        file_name = status.getPath().getName()
        if file_name.endswith(".parquet"):
            sizes[file_name] = int(status.getLen())
    counts = {
        Path(r["f"]).name: int(r["n"])
        for r in spark.read.parquet(f"{out}/{name}")
        .select(F.input_file_name().alias("f"))
        .groupBy("f")
        .agg(F.count(F.lit(1)).alias("n"))
        .collect()
    }
    return [
        ManifestFile(path=f"{name}/{file_name}", rows=counts.get(file_name, 0), bytes=size)
        for file_name, size in sorted(sizes.items())
    ]


def export_serving(
    spark: SparkSession,
    *,
    export_root: str,
    version: int | None = None,
    namespace: str = NAMESPACE,
    git_sha: str = "unknown",
    extra_ciks: Sequence[int] = (),
    now: datetime | None = None,
) -> Manifest:
    """Export one serving snapshot; returns the manifest that was written."""
    export_root = normalize_export_root(export_root)
    created = now or datetime.now(UTC)
    versions_table = f"{namespace}.corpus_versions"
    n = _next_version(spark, versions_table, version)

    tables = {
        name: f"{namespace}.{name}" for name in ("facts", "chunks", "chunk_embeddings", "companies")
    }
    snaps = {name: _snapshot(spark, table) for name, table in tables.items()}
    facts_in, chunks_in = (
        _read(spark, tables["facts"], snaps["facts"]),
        _read(spark, tables["chunks"], snaps["chunks"]),
    )
    emb_in = _read(spark, tables["chunk_embeddings"], snaps["chunk_embeddings"])
    companies_in = _read(spark, tables["companies"], snaps["companies"])

    # ---- companies and the cik scope of the facts
    in_scope_ciks = companies_in.where(
        F.coalesce(F.col("in_sp500"), F.lit(False))
        | F.coalesce(F.col("in_financebench"), F.lit(False))
    ).select("cik")
    if extra_ciks:
        extra = spark.createDataFrame([(int(c),) for c in extra_ciks], "cik int")
        in_scope_ciks = in_scope_ciks.unionByName(extra)
    in_scope_ciks = in_scope_ciks.distinct()
    log.info(
        "facts scope: %d companies (%d from --extra-ciks)", in_scope_ciks.count(), len(extra_ciks)
    )
    companies = companies_in.select(
        "cik",
        "name",
        "ticker",
        "sic",
        "fye_mmdd",
        F.coalesce("aliases", F.array().cast("array<string>")).alias("aliases"),
    ).select(*COMPANY_COLUMNS)

    # ---- facts
    facts = (
        facts_in.where(F.col("taxonomy").isin(list(TAXONOMIES)) & F.col("uom").isin(list(UOMS)))
        .where(
            F.coalesce(F.col("is_latest"), F.lit(False))
            | F.coalesce(F.col("is_original"), F.lit(False))
        )
        .join(in_scope_ciks, "cik", "left_semi")
        .withColumn("is_restated", F.coalesce(F.col("is_restated"), F.lit(False)))
        .select(*FACT_COLUMNS)
    )

    # ---- chunks: in scope, not degenerate, with an embedding of the right size
    # One row per chunk_id on both sides, so the exclusion count and chunks_rows share a base.
    in_scope = chunks_in.where(served_predicate()).dropDuplicates(["chunk_id"])
    tokens = F.coalesce(F.col("n_tokens"), F.lit(0))
    excluded = in_scope.where(tokens <= MIN_TOKENS_EXCLUSIVE).count()
    candidates = in_scope.where(tokens > MIN_TOKENS_EXCLUSIVE)
    # Only vectors of the pinned model and dimension; a chunk with two of them is ambiguous.
    emb = emb_in.where((F.col("model_name") == MODEL_NAME) & (F.col("dim") == DIM)).select(
        "chunk_id", "embedding"
    )
    ambiguous = emb.groupBy("chunk_id").count().where(F.col("count") > 1).count()
    if ambiguous:
        raise ExportError(f"{ambiguous} chunk_ids have more than one {MODEL_NAME} embedding")
    missing = (
        candidates.select("chunk_id").join(emb.select("chunk_id"), "chunk_id", "left_anti").count()
    )
    if missing:
        raise ExportError(f"{missing} in-scope chunks have no embedding; run embed_chunks first")
    wrong_dim = emb.where(F.size("embedding") != DIM).count()
    if wrong_dim:
        raise ExportError(f"{wrong_dim} embeddings are not {DIM}-dimensional")
    chunks = (
        candidates.dropDuplicates(["chunk_id"])
        .join(emb, "chunk_id", "inner")
        .withColumn("tenant_id", F.lit(None).cast("int"))
        .select(*CHUNK_COLUMNS)
    )
    not_embedded = chunks_in.where(not_embedded_predicate()).count()

    # ---- write (every check above has passed)
    out = f"{export_root}/corpus_version={n}"
    # A re-export overwrites the table directories one after another. Removing the old manifest
    # first means a run that dies half-way leaves a version with NO manifest (visibly incomplete)
    # instead of an old manifest that describes files that are gone.
    _delete_text(f"{out}/{MANIFEST_NAME}")
    files = {
        "companies": _write_table(spark, companies, "companies", out),
        "facts": _write_table(spark, facts, "facts", out),
        "chunks": _write_table(spark, chunks, "chunks", out),
    }
    manifest = Manifest(
        corpus_version=n,
        created_at=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        git_sha=git_sha,
        facts_rows=sum(f.rows for f in files["facts"]),
        chunks_rows=sum(f.rows for f in files["chunks"]),
        companies_rows=sum(f.rows for f in files["companies"]),
        embedding_model=MODEL_NAME,
        embedding_artifact=EmbeddingArtifact(
            hf_repo=ARTIFACT_HF_REPO,
            hf_revision=ARTIFACT_HF_REVISION,
            onnx_file=ARTIFACT_ONNX_FILE,
            onnx_sha256=ARTIFACT_ONNX_SHA256,
        ),
        dim=DIM,
        chunk_exclusions=ChunkExclusions(
            rule=f"n_tokens <= {MIN_TOKENS_EXCLUSIVE}", count=excluded
        ),
        embedding_scope=EmbeddingScope(
            rule=SERVED_RULE,
            not_embedded=NotEmbedded(
                rule=NOT_EMBEDDED_RULE,
                count=not_embedded,
                reason="serving is 10-K-only; quarterly numbers come from curated.facts (ADR-037)",
            ),
        ),
        iceberg_snapshots={f"curated.{name}": sid for name, sid in snaps.items()},
        files=files,
        filters={
            "facts": "cik of companies with in_sp500 OR in_financebench (plus --extra-ciks); "
            f"taxonomy in {list(TAXONOMIES)}; uom in {list(UOMS)}; is_latest OR is_original",
            "chunks": f"{SERVED_RULE}; n_tokens > {MIN_TOKENS_EXCLUSIVE}; "
            "inner join chunk_embeddings",
        },
    )
    _put_text(f"{out}/{MANIFEST_NAME}", manifest.model_dump_json(indent=2) + "\n")
    _record_version(spark, versions_table, manifest, out, created)
    log.info(
        "exported corpus_version=%d: %d facts, %d chunks (%d excluded, %d not embedded), "
        "%d companies",
        n,
        manifest.facts_rows,
        manifest.chunks_rows,
        excluded,
        not_embedded,
        manifest.companies_rows,
    )
    return manifest


def _record_version(
    spark: SparkSession, table: str, manifest: Manifest, export_uri: str, created: datetime
) -> None:
    if not spark.catalog.tableExists(table):
        spark.createDataFrame([], CORPUS_VERSIONS).writeTo(table).using("iceberg").create()
    snaps = manifest.iceberg_snapshots
    row = (
        manifest.corpus_version,
        created.astimezone(UTC).replace(tzinfo=None),
        manifest.git_sha,
        manifest.facts_rows,
        manifest.chunks_rows,
        manifest.chunk_exclusions.count,
        manifest.embedding_model,
        manifest.embedding_artifact.onnx_sha256,
        manifest.dim,
        snaps["curated.facts"],
        snaps["curated.chunks"],
        snaps["curated.chunk_embeddings"],
        export_uri,
    )
    view = f"_corpus_version_{uuid.uuid4().hex}"
    spark.createDataFrame([row], CORPUS_VERSIONS).createOrReplaceTempView(view)
    try:
        spark.sql(
            f"MERGE INTO {table} t USING {view} s ON t.corpus_version = s.corpus_version "
            "WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *"
        )
    finally:
        spark.catalog.dropTempView(view)


def _ints(value: str) -> list[int]:
    try:
        return [int(v) for v in value.split(",") if v.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"cannot parse {value!r}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a versioned serving snapshot.")
    parser.add_argument("--env", choices=["local", "emr", "glue"], default="local")
    parser.add_argument("--export-root", required=True, help="e.g. s3://<lake>/exports/serving")
    parser.add_argument("--version", type=int, help="omit for max+1; the current max re-exports it")
    parser.add_argument("--namespace", default=NAMESPACE)
    parser.add_argument("--git-sha", default="unknown")
    parser.add_argument("--extra-ciks", type=_ints, default=[], help="comma-separated CIKs to add")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    env: Env = args.env
    spark = spark_session(env, app_name="export_serving")
    try:
        manifest = export_serving(
            spark,
            export_root=args.export_root,
            version=args.version,
            namespace=args.namespace,
            git_sha=args.git_sha,
            extra_ciks=args.extra_ciks,
        )
    finally:
        spark.stop()
    summary = {
        "corpus_version": manifest.corpus_version,
        "facts_rows": manifest.facts_rows,
        "chunks_rows": manifest.chunks_rows,
        "companies_rows": manifest.companies_rows,
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
