"""Embed every chunk of `curated.chunks` into `curated.chunk_embeddings` (M03 §3.1, ADR-032).

Incremental by design: the chunks that still lack an embedding are found with a LEFT ANTI JOIN on
`chunk_id` (never `NOT IN`), after the form/cik filters and BEFORE any limit. A run can be bounded
to a wave of `max_chunks` chunks, taken in `xxhash64(chunk_id)` order, so a smoke run is a
pseudo-random sample, a failed wave loses only itself and a rerun resumes where the table left off.

Each Python worker fetches the staged model once (`pipelines.common.model_store`), loads it once
(`pipelines.common.embedding.get_embedder`, ONNX threads = 1) and embeds the rows of its partitions
with `passage_embed`: no query instruction, vectors L2-normalized. Both the driver and the workers
check the ONNX file against the pinned sha256, so a wrong model never writes a vector.

One run at a time per output table (the Glue job has `max_concurrent_runs = 1`): two overlapping
runs would both see the same chunks as missing and both insert them, so the table could end up
with duplicate `chunk_id` rows. Embeddings are matched by `chunk_id` only: if a re-parse changes a
chunk's text but keeps its id, delete that filing's embeddings first (ADR-032), or the old vector
stays; the export inner-joins to `chunks`, so vectors of chunks that no longer exist are dropped.

Every chunk is embedded, including degenerate ones (ADR-035 drops them at export only), and a NULL
text is embedded as the empty string, so "missing = 0" stays checkable.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import platform
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from importlib.metadata import version as package_version
from pathlib import Path

import numpy as np
import pandas as pd

from pipelines.common.embedding import (
    ARTIFACT_ONNX_SHA256,
    DIM,
    MODEL_NAME,
    get_embedder,
    sha256_file,
)
from pipelines.common.model_store import fetch_model_dir
from pipelines.common.object_store import S3Store
from pipelines.common.spark_session import Env, spark_session


def _ensure_distutils() -> None:
    """PySpark 3.5's `pandas_udf` imports `distutils`, which Python 3.12 removed; `setuptools`
    supplies a shim (see `pipelines/spark/fiscal.py`, which has the same guard). Runs on the
    workers too, because they import this module to unpickle `_embed_batches`."""
    try:
        importlib.import_module("distutils")
    except ModuleNotFoundError:
        importlib.import_module("setuptools")


_ensure_distutils()

from pyspark import StorageLevel  # noqa: E402 - after the distutils guard
from pyspark.sql import DataFrame, SparkSession  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402

from pipelines.spark.curated_schemas import CHUNK_EMBEDDINGS  # noqa: E402
from pipelines.spark.iceberg_util import ensure_table  # noqa: E402

log = logging.getLogger(__name__)

CHUNKS_TABLE = "lake.curated.chunks"
OUTPUT_TABLE = "lake.curated.chunk_embeddings"
PARTITIONING = "bucket(16, chunk_id)"
DEFAULT_ROWS_PER_PARTITION = 10_000
DEFAULT_BATCH_SIZE = 256


@dataclass(frozen=True)
class EmbedStats:
    chunks_selected: int  # still lacking an embedding after the filters, before this run's limit
    chunks_embedded: int  # rows written this run
    chunks_remaining: int  # same filters, still lacking one after this run
    # Wall time of the whole embedding stage: text join, repartition, each worker's model fetch and
    # load, and the ONNX inference. A small run therefore reports a rate far below steady state.
    seconds_embedding: float
    chunks_per_minute: float
    onnx_sha256: str  # the verified ONNX file the vectors came from
    dim: int


_MODEL_DIRS: dict[str, Path] = {}


def _local_model_dir(model_location: str) -> Path:
    """The verified local copy of the staged model (fetched once per process).

    `expected_onnx_sha256` checks the staged manifest against the pin, so a model and a manifest
    that were replaced together are still refused. The fetch is atomic and idempotent, so several
    Python workers on one node can call it at once.
    """
    cached = _MODEL_DIRS.get(model_location)
    if cached is None:
        dest = Path(tempfile.gettempdir()) / f"edgarqa-bge-{ARTIFACT_ONNX_SHA256[:12]}"
        cached = fetch_model_dir(model_location, dest, expected_onnx_sha256=ARTIFACT_ONNX_SHA256)
        _MODEL_DIRS[model_location] = cached
    return cached


def _embed_batches(
    model_location: str, batch_size: int
) -> Callable[[Iterator[pd.DataFrame]], Iterator[pd.DataFrame]]:
    """The `mapInPandas` function: embeds the `text` of each pandas batch with the cached model."""

    def run(batches: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
        embedder = get_embedder(
            _local_model_dir(model_location), threads=1, expected_onnx_sha256=ARTIFACT_ONNX_SHA256
        )
        for batch in batches:
            vectors = embedder.embed_passages(batch["text"].fillna("").tolist(), batch_size)
            yield pd.DataFrame(
                {
                    "chunk_id": batch["chunk_id"].to_numpy(),
                    "model_name": MODEL_NAME,
                    "dim": np.full(len(batch), DIM, dtype=np.int32),
                    "embedding": list(vectors),
                    "embedded_at": pd.Timestamp.now("UTC").tz_localize(None),
                }
            )

    return run


def _select_missing_ids(
    spark: SparkSession,
    chunks_table: str,
    output_table: str,
    forms: Sequence[str] | None,
    ciks: Sequence[int] | None,
) -> DataFrame:
    chunks = spark.table(chunks_table)
    if forms:
        chunks = chunks.where(F.col("form").isin(list(forms)))
    if ciks:
        chunks = chunks.where(F.col("cik").isin([int(c) for c in ciks]))
    done = spark.table(output_table).select("chunk_id")
    # Ids only: the text is joined back for the wave, so the anti-join shuffles little.
    # distinct: a duplicated chunk_id in the chunks table must give one vector, not several.
    return chunks.select("chunk_id").distinct().join(done, "chunk_id", "left_anti")


def embed_chunks(
    spark: SparkSession,
    *,
    chunks_table: str,
    output_table: str,
    model_location: str,
    forms: Sequence[str] | None = None,
    ciks: Sequence[int] | None = None,
    max_chunks: int | None = None,
    rows_per_partition: int = DEFAULT_ROWS_PER_PARTITION,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> EmbedStats:
    """Embed the chunks that lack a vector, at most `max_chunks` of them (see the module docstring).

    `max_chunks=None` means no limit and `max_chunks=0` embeds nothing (a dry count of the backlog).
    """
    if rows_per_partition < 1:
        raise ValueError(f"rows_per_partition must be >= 1, got {rows_per_partition}")
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    if max_chunks is not None and max_chunks < 0:
        raise ValueError(f"max_chunks must be >= 0, got {max_chunks}")

    # Verify the model on the driver first: a bad or missing one fails before any table is touched.
    onnx_sha256 = sha256_file(_local_model_dir(model_location) / "model_optimized.onnx")
    # The cross-architecture check (ADR-036) reads this line from the Glue driver log.
    log.info(
        "driver platform %s, onnxruntime %s, fastembed %s",
        platform.machine(),
        package_version("onnxruntime"),
        package_version("fastembed"),
    )

    ensure_table(spark, output_table, spark.createDataFrame([], CHUNK_EMBEDDINGS), PARTITIONING)

    missing = _select_missing_ids(spark, chunks_table, output_table, forms, ciks)
    wave = missing
    if max_chunks is not None:
        # The order only decides WHICH chunks a limited wave takes; an unlimited run needs no sort.
        wave = wave.orderBy(F.xxhash64("chunk_id"), "chunk_id").limit(max_chunks)
    wave = wave.persist(StorageLevel.MEMORY_AND_DISK)
    tag = uuid.uuid4().hex
    view = f"_embedded_{tag}"
    embedded_df: DataFrame | None = None
    try:
        n_wave = wave.count()
        n_selected = n_wave if max_chunks is None else _selected_count(missing, n_wave, max_chunks)
        if n_wave == 0:
            return EmbedStats(n_selected, 0, n_selected, 0.0, 0.0, onnx_sha256, DIM)

        texts = (
            spark.table(chunks_table)
            .select("chunk_id", F.coalesce("text", F.lit("")).alias("text"))
            .dropDuplicates(["chunk_id"])
            .join(wave, "chunk_id", "inner")
        )
        n_parts = max(1, math.ceil(n_wave / rows_per_partition))
        embedded_df = (
            texts.repartition(n_parts, "chunk_id")
            .mapInPandas(_embed_batches(model_location, batch_size), schema=CHUNK_EMBEDDINGS)
            # Persisted so the MERGE below reads the vectors instead of embedding them again.
            .persist(StorageLevel.DISK_ONLY)
        )
        started = time.monotonic()
        n_embedded = embedded_df.count()
        seconds = time.monotonic() - started

        embedded_df.createOrReplaceTempView(view)
        spark.sql(
            f"MERGE INTO {output_table} t USING {view} s ON t.chunk_id = s.chunk_id "
            "WHEN NOT MATCHED THEN INSERT *"
        )
    finally:
        spark.catalog.dropTempView(view)
        if embedded_df is not None:
            embedded_df.unpersist()
        wave.unpersist()

    per_minute = n_embedded / seconds * 60 if seconds > 0 else 0.0
    stats = EmbedStats(
        chunks_selected=n_selected,
        chunks_embedded=n_embedded,
        chunks_remaining=n_selected - n_embedded,
        seconds_embedding=seconds,
        chunks_per_minute=per_minute,
        onnx_sha256=onnx_sha256,
        dim=DIM,
    )
    log.info(
        "embedded %d of %d missing chunks in %.1fs (%.0f/min)",
        n_embedded,
        n_selected,
        seconds,
        per_minute,
    )
    return stats


def _selected_count(missing: DataFrame, n_wave: int, max_chunks: int) -> int:
    """Size of the backlog when a limit applied: the wave is the backlog if it did not fill up."""
    return n_wave if n_wave < max_chunks else missing.count()


def _csv(cast: type) -> Callable[[str], list]:
    def parse(value: str) -> list:
        try:
            return [cast(v) for v in value.split(",") if v.strip()]
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"cannot parse {value!r}: {exc}") from exc

    return parse


def _write_json(location: str, payload: dict[str, object]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if location.startswith("s3://"):
        bucket, _, key = location[len("s3://") :].partition("/")
        S3Store(bucket).put(key, text.encode("utf-8"))
        return
    path = Path(location)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Embed the chunks that lack a vector.")
    parser.add_argument("--env", choices=["local", "emr", "glue"], default="local")
    parser.add_argument("--chunks-table", default=CHUNKS_TABLE)
    parser.add_argument("--output-table", default=OUTPUT_TABLE)
    parser.add_argument("--model-location", required=True, help="staged model dir or s3:// prefix")
    parser.add_argument("--forms", type=_csv(str), help="comma-separated forms, e.g. 10-K,10-K/A")
    parser.add_argument("--ciks", type=_csv(int), help="comma-separated CIKs")
    parser.add_argument("--max-chunks", type=int, help="wave size; omit for no limit")
    parser.add_argument("--rows-per-partition", type=int, default=DEFAULT_ROWS_PER_PARTITION)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--metrics-out", help="write the EmbedStats JSON here (path or s3:// URI)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    env: Env = args.env
    spark = spark_session(env, app_name="embed_chunks")
    try:
        namespace = args.output_table.rsplit(".", 1)[0]
        spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
        stats = embed_chunks(
            spark,
            chunks_table=args.chunks_table,
            output_table=args.output_table,
            model_location=args.model_location,
            forms=args.forms,
            ciks=args.ciks,
            max_chunks=args.max_chunks,
            rows_per_partition=args.rows_per_partition,
            batch_size=args.batch_size,
        )
    finally:
        spark.stop()
    if args.metrics_out:
        _write_json(args.metrics_out, asdict(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
