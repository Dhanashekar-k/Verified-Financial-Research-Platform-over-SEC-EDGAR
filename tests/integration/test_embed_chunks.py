"""Integration tests for ``pipelines/spark/embed_chunks.py`` (test-first; module not written yet).

The job reads ``curated.chunks`` and writes ``curated.chunk_embeddings`` (contract 2.2, ADR-032):
every chunk gets one L2-normalized 384-dim vector from the quantized bge-small model, found
incrementally through a LEFT ANTI JOIN on ``chunk_id`` (never a blind append), in waves bounded by
``max_chunks`` and ordered by ``xxhash64(chunk_id)``.

The embedder is NEVER faked (CLAUDE.md hard rule 4): the real ONNX model is read from
``EDGARQA_EMBED_MODEL_DIR`` (default ``data/models/bge-small-en-v1.5``), and a missing model
FAILS the run (it never skips), like the JVM does in ``conftest.resolve_java``.

Design decisions the implementation must satisfy (the tests pin them):

* ``chunks_selected`` is counted AFTER the form/cik filters and the anti-join but BEFORE the
  ``max_chunks`` limit, so ``selected == embedded + remaining`` on a run that completes.
* Wave order is ``ORDER BY xxhash64(chunk_id), chunk_id``, applied AFTER filters and the anti-join
  (a limit before the anti-join would starve later waves).
* ``main`` is exercised through a subprocess with its own warehouse: it must build a session
  through ``spark_session(env)`` and may call ``spark.stop()``, which would kill the
  session-scoped ``spark`` fixture if it ran in-process.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from pyspark.sql import SparkSession

from pipelines.common.embedding import (
    ARTIFACT_ONNX_FILE,
    ARTIFACT_ONNX_SHA256,
    DIM,
    MODEL_NAME,
    Embedder,
)
from pipelines.common.model_store import write_manifest
from pipelines.spark import curated_schemas
from pipelines.spark.curated_schemas import CHUNKS
from pipelines.spark.embed_chunks import EmbedStats, embed_chunks

REPO_ROOT = Path(__file__).resolve().parents[2]
CIKS = [320193, 789019, 1018724]

# The contract columns of curated.chunk_embeddings (docs/plan/05_data_contracts.md section 2.2).
CONTRACT_COLUMNS = [
    ("chunk_id", "string"),
    ("model_name", "string"),
    ("dim", "int"),
    ("embedding", "array<float>"),
    ("embedded_at", "timestamp"),
]

SENTENCES = [
    "Revenue increased 12% year over year driven by higher subscription volume.",
    "We are exposed to foreign currency exchange rate fluctuations.",
    "The Company's principal executive offices are located in Cupertino, California.",
    "Net cash provided by operating activities was $4.2 billion.",
    "We may be unable to protect our intellectual property rights.",
    "Our credit facility contains covenants that restrict our operations.",
    "Inventory write-downs reduced gross margin by 40 basis points.",
    "The Board authorized a share repurchase program of up to $10 billion.",
    "Cybersecurity incidents could disrupt our operations and damage our reputation.",
    "Goodwill is tested for impairment annually in the fourth quarter.",
    "We depend on a limited number of contract manufacturers and suppliers.",
    "The company declared a quarterly dividend of $0.25 per share payable in March.",
    "Operating expenses grew 8% as we continued to invest in research and development.",
    "Changes in tax legislation could adversely affect our effective tax rate.",
    "Long-term debt, net of current portion, was $9.1 billion at fiscal year end.",
    "We lease office space under non-cancelable operating leases expiring through 2031.",
    "Competition in our markets is intense and may result in pricing pressure.",
    "Stock-based compensation expense was $2.8 billion for the year.",
    "Our results may fluctuate from quarter to quarter due to seasonality.",
    "Segment operating income decreased primarily due to higher freight costs.",
]


# --------------------------------------------------------------------------------------------
# fixtures and helpers
# --------------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def model_dir() -> Path:
    """The local ONNX model directory; FAIL (never skip) when it has not been fetched."""
    path = Path(os.environ.get("EDGARQA_EMBED_MODEL_DIR", "data/models/bge-small-en-v1.5"))
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_dir() or not (path / ARTIFACT_ONNX_FILE).is_file():
        pytest.fail(f"embedding model not found at {path}: run `make fetch-model`", pytrace=False)
    return path


@pytest.fixture(scope="session")
def reference_embedder(model_dir: Path) -> Embedder:
    """The library embedder the stored vectors are compared against (real model, 1 thread)."""
    return Embedder(model_dir, threads=1)


def long_text(min_words: int = 300) -> str:
    """Several hundred words of filing-like prose: about 400 bge tokens."""
    words: list[str] = []
    i = 0
    while len(words) < min_words:
        words.extend(SENTENCES[i % len(SENTENCES)].split())
        i += 1
    return " ".join(words)


def chunk_row(
    i: int,
    cik: int,
    form: str,
    text: str | None,
    n_tokens: int | None = None,
    chunk_id: str | None = None,
) -> dict[str, object]:
    """One `curated.chunks` row with every contract column set."""
    return {
        "chunk_id": chunk_id or f"{cik}-{form.replace('/', '')}-{i:04d}",
        "cik": cik,
        "accession": f"{cik:010d}-23-{i:06d}",
        "form": form,
        "fiscal_year": 2023,
        "fiscal_period": "FY" if form == "10-K" else "Q2",
        "section": "item_7" if i % 2 else "item_1a",
        "section_title": "MD&A" if i % 2 else "Risk Factors",
        "chunk_index": i,
        "text": text,
        "n_tokens": n_tokens if n_tokens is not None else len((text or "").split()),
        "is_table": False,
        "source_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/x.htm",
        "anchor": f"a{i}",
        "page": None,
        "doc_source": "edgar_html",
        "corpus_version": 1,
    }


def base_rows() -> list[dict[str, object]]:
    """40 chunks over forms 10-K (24) and 10-Q (16) and three ciks, with degenerate members."""
    rows: list[dict[str, object]] = []
    for i in range(40):
        cik = CIKS[i % 3]
        form = "10-K" if i % 5 < 3 else "10-Q"
        text = f"{SENTENCES[i % len(SENTENCES)]} (fiscal {2019 + i % 5}, note {i})"
        rows.append(chunk_row(i, cik, form, text))
    rows[0] = chunk_row(0, CIKS[0], "10-K", "", n_tokens=0)  # empty text
    rows[1] = chunk_row(1, CIKS[1], "10-K", "Revenue", n_tokens=1)  # one word
    rows[2] = chunk_row(2, CIKS[2], "10-K", long_text(), n_tokens=400)  # ~400 tokens
    return rows


@dataclass(frozen=True)
class Seed:
    chunks: str
    output: str
    rows: list[dict[str, object]]

    @property
    def ids(self) -> set[str]:
        return {str(r["chunk_id"]) for r in self.rows}


def write_chunks(spark: SparkSession, table: str, rows: list[dict[str, object]]) -> None:
    df = spark.createDataFrame([tuple(r[f.name] for f in CHUNKS.fields) for r in rows], CHUNKS)
    df.writeTo(table).using("iceberg").create()


@pytest.fixture
def seed(spark: SparkSession, lake_namespace: str) -> Seed:
    """`{ns}.chunks` with the 40 base rows in the real contract schema; output name only."""
    rows = base_rows()
    write_chunks(spark, f"{lake_namespace}.chunks", rows)
    return Seed(f"{lake_namespace}.chunks", f"{lake_namespace}.chunk_embeddings", rows)


def run(
    spark: SparkSession,
    seed: Seed,
    model_dir: Path,
    *,
    output: str | None = None,
    rows_per_partition: int = 20,
    **kw: object,
) -> EmbedStats:
    """Call the job with test defaults: 20 rows per partition keeps the model loads to two."""
    return embed_chunks(
        spark,
        chunks_table=seed.chunks,
        output_table=output or seed.output,
        model_location=str(model_dir),
        rows_per_partition=rows_per_partition,
        **kw,  # type: ignore[arg-type]
    )


def ids_in(spark: SparkSession, table: str) -> list[str]:
    return [r.chunk_id for r in spark.table(table).select("chunk_id").collect()]


def vectors(spark: SparkSession, table: str) -> dict[str, np.ndarray]:
    return {
        r.chunk_id: np.asarray(r.embedding, dtype=np.float32)
        for r in spark.table(table).select("chunk_id", "embedding").collect()
    }


def embedded_at(spark: SparkSession, table: str) -> dict[str, object]:
    return {r.chunk_id: r.embedded_at for r in spark.table(table).collect()}


def hash_order(spark: SparkSession, chunks_table: str) -> list[str]:
    """All chunk ids in the wave order the job promises: xxhash64(chunk_id), then chunk_id."""
    rows = spark.sql(
        f"SELECT chunk_id FROM {chunks_table} ORDER BY xxhash64(chunk_id), chunk_id"
    ).collect()
    return [r.chunk_id for r in rows]


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    a64, b64 = a.astype(np.float64), b.astype(np.float64)
    return float(a64 @ b64 / (np.linalg.norm(a64) * np.linalg.norm(b64)))


def ids_where(
    seed: Seed, *, forms: list[str] | None = None, ciks: list[int] | None = None
) -> set[str]:
    return {
        str(r["chunk_id"])
        for r in seed.rows
        if (forms is None or r["form"] in forms) and (ciks is None or r["cik"] in ciks)
    }


# --------------------------------------------------------------------------------------------
# 1. output table: contract schema, CHUNK_EMBEDDINGS, bucket partitioning
# --------------------------------------------------------------------------------------------


def test_output_table_has_the_contract_columns_and_the_curated_schema_matches(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """The table has exactly the contract columns/types, and `CHUNK_EMBEDDINGS` says the same.

    A new StructType `CHUNK_EMBEDDINGS` in `curated_schemas` is the code-side contract; the
    table written by the job must not drift from it or from the contract document.
    """
    run(spark, seed, model_dir)

    table = spark.table(seed.output)
    actual = [(f.name, f.dataType.simpleString()) for f in table.schema.fields]
    assert actual == CONTRACT_COLUMNS
    assert hasattr(curated_schemas, "CHUNK_EMBEDDINGS"), "add CHUNK_EMBEDDINGS to curated_schemas"
    declared = [
        (f.name, f.dataType.simpleString()) for f in curated_schemas.CHUNK_EMBEDDINGS.fields
    ]
    assert declared == CONTRACT_COLUMNS


def test_output_table_is_partitioned_by_bucket_16_of_chunk_id(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """Iceberg partition spec is `bucket(16, chunk_id)` (contract 2.2), and data files use it.

    The serving export reads the table by bucket; a plain unpartitioned table would still pass
    every row-level test, so the spec itself is asserted from Iceberg's metadata.
    """
    run(spark, seed, model_dir)

    described = {
        (r[0], r[1]) for r in spark.sql(f"DESCRIBE TABLE EXTENDED {seed.output}").collect()
    }
    assert ("Part 0", "bucket(16, chunk_id)") in described
    parts = spark.sql(f"SELECT partition.chunk_id_bucket AS b FROM {seed.output}.partitions")
    buckets = [r.b for r in parts.collect()]
    assert buckets, "no data was written into any bucket partition"
    assert all(0 <= b < 16 for b in buckets)
    assert len(buckets) > 1, "40 hashed ids must land in more than one of the 16 buckets"


def test_an_existing_output_table_is_reused_not_recreated(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """A pre-created contract table (DDL from the contract) is appended to, and kept as is.

    Loaders may create the table first from the contract DDL; the job must neither fail on
    "already exists" nor replace it (that would drop earlier embeddings).
    """
    spark.sql(
        f"CREATE TABLE {seed.output} (chunk_id string, model_name string, dim int, "
        "embedding array<float>, embedded_at timestamp) USING iceberg "
        "PARTITIONED BY (bucket(16, chunk_id))"
    )

    stats = run(spark, seed, model_dir)

    assert stats.chunks_embedded == len(seed.rows)
    assert spark.table(seed.output).count() == len(seed.rows)


# --------------------------------------------------------------------------------------------
# 2 + 3. every chunk embedded, vector quality, equality with the library embedder
# --------------------------------------------------------------------------------------------


def test_every_chunk_is_embedded_including_empty_and_tiny_ones(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """Missing = 0 by a left-anti join, with the degenerate chunks (ADR-035 drops them at export).

    The empty-text, one-word and ~400-token chunks must all have a row: skipping them would make
    "every chunk has a vector" uncheckable.
    """
    run(spark, seed, model_dir)

    missing = spark.table(seed.chunks).join(spark.table(seed.output), "chunk_id", "left_anti")
    assert missing.count() == 0
    stored = ids_in(spark, seed.output)
    assert sorted(stored) == sorted(seed.ids)
    assert {str(r["chunk_id"]) for r in seed.rows[:3]} <= set(stored)


def test_rows_carry_model_name_dim_unit_norm_no_nan_and_a_timestamp(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """Per-row invariants: model name, dim 384 == size(embedding), finite, unit norm, timestamp."""
    run(spark, seed, model_dir)

    out = spark.table(seed.output)
    assert out.where(f"model_name != '{MODEL_NAME}' OR model_name IS NULL").count() == 0
    assert MODEL_NAME == "BAAI/bge-small-en-v1.5"
    assert out.where(f"dim != {DIM} OR dim IS NULL").count() == 0
    assert out.where(f"size(embedding) != {DIM} OR embedding IS NULL").count() == 0
    assert out.where("embedded_at IS NULL").count() == 0
    assert out.where("chunk_id IS NULL").count() == 0
    for chunk_id, vec in vectors(spark, seed.output).items():
        assert vec.dtype == np.float32
        assert not np.isnan(vec).any(), chunk_id
        assert abs(float(np.linalg.norm(vec.astype(np.float64))) - 1.0) < 1e-3, chunk_id


def test_stored_vectors_equal_embed_passages_and_carry_no_query_instruction(
    spark: SparkSession, seed: Seed, model_dir: Path, reference_embedder: Embedder
):
    """Stored vector == `Embedder.embed_passages(text)` (cosine >= 0.99999), never the query form.

    The job must use the shared wrapper path (passages, no BGE instruction, ADR-033). Comparing
    against `embed_queries` shows the check discriminates: the prefixed vector is measurably
    different for real sentences, so a job that prefixed passages would fail the second bound.
    """
    run(spark, seed, model_dir)

    stored = vectors(spark, seed.output)
    texts = {str(r["chunk_id"]): str(r["text"]) for r in seed.rows}
    ids = sorted(texts)
    expected = reference_embedder.embed_passages([texts[i] for i in ids])
    for chunk_id, ref in zip(ids, expected, strict=True):
        assert cosine(stored[chunk_id], ref) >= 0.99999, chunk_id

    real = [i for i in ids if len(texts[i].split()) > 5][:5]
    prefixed = reference_embedder.embed_queries([texts[i] for i in real], instruction=True)
    for chunk_id, with_instruction in zip(real, prefixed, strict=True):
        assert cosine(stored[chunk_id], with_instruction) < 0.99999, chunk_id


def test_a_null_text_chunk_is_embedded_as_the_empty_string(
    spark: SparkSession, lake_namespace: str, model_dir: Path, reference_embedder: Embedder
):
    """A NULL `text` must not crash pandas or be skipped: it gets the empty-string vector.

    "Every chunk is embedded" (ADR-032) has no carve-out for a NULL; a skipped row would stay
    in the anti-join result forever and make every later run report it as still lacking.
    """
    rows = [chunk_row(0, CIKS[0], "10-K", None), chunk_row(1, CIKS[0], "10-K", SENTENCES[0])]
    write_chunks(spark, f"{lake_namespace}.chunks", rows)
    s = Seed(f"{lake_namespace}.chunks", f"{lake_namespace}.chunk_embeddings", rows)

    stats = run(spark, s, model_dir)

    assert stats.chunks_embedded == 2
    stored = vectors(spark, s.output)
    empty = reference_embedder.embed_passages([""])[0]
    assert cosine(stored[str(rows[0]["chunk_id"])], empty) >= 0.99999


# --------------------------------------------------------------------------------------------
# 4. idempotent and incremental
# --------------------------------------------------------------------------------------------


def test_second_run_selects_and_embeds_nothing_and_leaves_rows_untouched(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """Rerun = no-op: stats say 0/0/0, same ids, no duplicates, and `embedded_at` is unchanged.

    Unchanged timestamps prove existing rows were not rewritten (a delete-and-reinsert job would
    pass the count checks but change them). The chunks table is read-only for the job.
    """
    first = run(spark, seed, model_dir)
    before = embedded_at(spark, seed.output)
    chunk_count = spark.table(seed.chunks).count()

    second = run(spark, seed, model_dir)

    assert first.chunks_embedded == len(seed.rows)
    assert (second.chunks_selected, second.chunks_embedded, second.chunks_remaining) == (0, 0, 0)
    assert math.isfinite(second.chunks_per_minute) and second.chunks_per_minute >= 0
    assert embedded_at(spark, seed.output) == before
    out = spark.table(seed.output)
    assert out.count() == len(seed.rows)
    assert out.select("chunk_id").distinct().count() == out.count()
    assert spark.table(seed.chunks).count() == chunk_count


def test_running_twice_in_a_row_never_duplicates_a_chunk_id(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """No blind append: `count(distinct chunk_id) == count(*)` after repeated runs."""
    for _ in range(3):
        run(spark, seed, model_dir)

    total, distinct = spark.sql(
        f"SELECT count(*), count(DISTINCT chunk_id) FROM {seed.output}"
    ).first()  # type: ignore[misc]
    assert total == distinct == len(seed.rows)


def test_only_new_chunks_are_embedded_after_the_chunks_table_grows(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """Five chunks appended to `chunks` -> selected 5, embedded 5, old rows not touched."""
    run(spark, seed, model_dir)
    before = embedded_at(spark, seed.output)
    new_rows = [
        chunk_row(100 + j, CIKS[j % 3], "10-K", f"{SENTENCES[j]} (new {j})") for j in range(5)
    ]
    df = spark.createDataFrame([tuple(r[f.name] for f in CHUNKS.fields) for r in new_rows], CHUNKS)
    df.writeTo(seed.chunks).append()

    stats = run(spark, seed, model_dir)

    assert (stats.chunks_selected, stats.chunks_embedded, stats.chunks_remaining) == (5, 5, 0)
    after = embedded_at(spark, seed.output)
    assert len(after) == len(seed.rows) + 5
    assert {k: v for k, v in after.items() if k in before} == before
    assert set(after) - set(before) == {str(r["chunk_id"]) for r in new_rows}


def test_only_deleted_embeddings_are_recomputed(spark: SparkSession, seed: Seed, model_dir: Path):
    """Deleting four embeddings from the output re-embeds exactly those four (same vectors).

    The anti-join is against the output table as it is NOW, not against a remembered watermark.
    """
    run(spark, seed, model_dir)
    before_at = embedded_at(spark, seed.output)
    before_vec = vectors(spark, seed.output)
    gone = sorted(seed.ids)[:4]
    quoted = ", ".join(f"'{g}'" for g in gone)
    spark.sql(f"DELETE FROM {seed.output} WHERE chunk_id IN ({quoted})")
    assert spark.table(seed.output).count() == len(seed.rows) - 4

    stats = run(spark, seed, model_dir)

    assert (stats.chunks_selected, stats.chunks_embedded, stats.chunks_remaining) == (4, 4, 0)
    after_at = embedded_at(spark, seed.output)
    after_vec = vectors(spark, seed.output)
    assert set(after_at) == seed.ids
    assert all(after_at[k] == before_at[k] for k in seed.ids - set(gone))
    assert all(cosine(after_vec[k], before_vec[k]) >= 0.99999 for k in gone)


def test_an_empty_chunks_table_creates_the_output_table_and_embeds_nothing(
    spark: SparkSession, lake_namespace: str, model_dir: Path
):
    """No chunks: a valid empty output table with the contract schema, zero stats, no error."""
    write_chunks(spark, f"{lake_namespace}.chunks", [])
    s = Seed(f"{lake_namespace}.chunks", f"{lake_namespace}.chunk_embeddings", [])

    stats = run(spark, s, model_dir)

    assert (stats.chunks_selected, stats.chunks_embedded, stats.chunks_remaining) == (0, 0, 0)
    table = spark.table(s.output)
    assert [(f.name, f.dataType.simpleString()) for f in table.schema.fields] == CONTRACT_COLUMNS
    assert table.count() == 0


# --------------------------------------------------------------------------------------------
# 5. max_chunks waves
# --------------------------------------------------------------------------------------------


def test_max_chunks_embeds_exactly_that_many_in_xxhash_order_and_is_deterministic(
    spark: SparkSession, seed: Seed, model_dir: Path, lake_namespace: str
):
    """`max_chunks=10` writes the first 10 ids by `xxhash64(chunk_id)` (then chunk_id).

    Same state -> same 10: a second, fresh output table gets the identical set. Stats: 40
    selected (before the limit), 10 embedded, 30 remaining.
    """
    expected = hash_order(spark, seed.chunks)[:10]

    stats = run(spark, seed, model_dir, max_chunks=10)
    again = run(spark, seed, model_dir, output=f"{lake_namespace}.repeat", max_chunks=10)

    assert set(ids_in(spark, seed.output)) == set(expected)
    assert set(ids_in(spark, f"{lake_namespace}.repeat")) == set(expected)
    assert (stats.chunks_selected, stats.chunks_embedded, stats.chunks_remaining) == (40, 10, 30)
    assert again.chunks_embedded == 10


def test_waves_cover_everything_exactly_once_and_remaining_counts_down(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """Repeated `max_chunks=10` runs take consecutive slices of the hash order until done.

    Each wave embeds 10, `chunks_remaining` falls 30, 20, 10, 0; no chunk is embedded twice
    (count == distinct == 40); a wave after completion embeds 0.
    """
    order = hash_order(spark, seed.chunks)
    seen: set[str] = set()
    remaining_after: list[int] = []

    for wave in range(4):
        stats = run(spark, seed, model_dir, max_chunks=10)
        now = set(ids_in(spark, seed.output))
        assert now - seen == set(order[wave * 10 : wave * 10 + 10]), f"wave {wave}"
        assert stats.chunks_embedded == 10
        seen = now
        remaining_after.append(stats.chunks_remaining)

    assert remaining_after == [30, 20, 10, 0]
    out = spark.table(seed.output)
    assert out.count() == out.select("chunk_id").distinct().count() == len(seed.rows)
    assert run(spark, seed, model_dir, max_chunks=10).chunks_embedded == 0


def test_max_chunks_larger_than_the_backlog_embeds_the_whole_backlog(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """A limit above the number of lacking chunks is not an error and does not pad or repeat."""
    stats = run(spark, seed, model_dir, max_chunks=1000)

    assert (stats.chunks_selected, stats.chunks_embedded, stats.chunks_remaining) == (40, 40, 0)
    assert spark.table(seed.output).count() == 40


def test_max_chunks_zero_is_a_dry_count_that_writes_nothing(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """`max_chunks=0` embeds nothing and reports the backlog (documented in `embed_chunks`)."""
    stats = run(spark, seed, model_dir, max_chunks=0)

    assert (stats.chunks_selected, stats.chunks_embedded, stats.chunks_remaining) == (40, 0, 40)
    assert spark.table(seed.output).count() == 0


def test_max_chunks_equal_to_the_backlog_embeds_it_all_and_leaves_none(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """A limit that exactly fills the wave must still report the true backlog (40), not 0."""
    stats = run(spark, seed, model_dir, max_chunks=40)

    assert (stats.chunks_selected, stats.chunks_embedded, stats.chunks_remaining) == (40, 40, 0)
    assert spark.table(seed.output).count() == 40


def test_a_duplicated_chunk_id_in_the_chunks_table_gets_one_vector(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """Two source rows with one id must not become four (or two) output rows."""
    dup = f"{seed.chunks}_dup"
    rows = [*seed.rows, dict(seed.rows[5]), dict(seed.rows[5])]  # id of row 5 appears three times
    write_chunks(spark, dup, rows)

    stats = embed_chunks(
        spark,
        chunks_table=dup,
        output_table=seed.output,
        model_location=str(model_dir),
        rows_per_partition=20,
    )

    out = spark.table(seed.output)
    assert stats.chunks_selected == stats.chunks_embedded == 40
    assert out.count() == out.select("chunk_id").distinct().count() == 40


# --------------------------------------------------------------------------------------------
# 6. filters
# --------------------------------------------------------------------------------------------


def test_forms_filter_embeds_only_those_forms(spark: SparkSession, seed: Seed, model_dir: Path):
    """`forms=["10-K"]` embeds the 10-K chunks only; remaining is counted under the same filter."""
    wanted = ids_where(seed, forms=["10-K"])

    stats = run(spark, seed, model_dir, forms=["10-K"])

    assert set(ids_in(spark, seed.output)) == wanted
    assert (stats.chunks_selected, stats.chunks_embedded) == (len(wanted), len(wanted))
    assert stats.chunks_remaining == 0  # 10-Q chunks lack vectors but are outside the filter


def test_ciks_filter_embeds_only_those_ciks(spark: SparkSession, seed: Seed, model_dir: Path):
    """`ciks=[a, b]` embeds the chunks of those companies only."""
    wanted = ids_where(seed, ciks=[CIKS[0], CIKS[2]])

    stats = run(spark, seed, model_dir, ciks=[CIKS[0], CIKS[2]])

    assert set(ids_in(spark, seed.output)) == wanted
    assert stats.chunks_embedded == len(wanted)
    assert stats.chunks_remaining == 0


def test_forms_and_ciks_filters_are_anded_and_a_later_run_picks_up_the_rest(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """forms AND ciks narrow together; an unfiltered run then embeds exactly the others."""
    both = ids_where(seed, forms=["10-Q"], ciks=[CIKS[1]])
    assert 0 < len(both) < len(seed.rows)

    first = run(spark, seed, model_dir, forms=["10-Q"], ciks=[CIKS[1]])
    second = run(spark, seed, model_dir)

    assert first.chunks_embedded == len(both)
    assert second.chunks_selected == second.chunks_embedded == len(seed.rows) - len(both)
    assert spark.table(seed.output).count() == len(seed.rows)


def test_a_filter_matching_nothing_embeds_nothing_and_is_not_an_error(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """An unknown form or cik selects 0 chunks and writes 0 rows; the table still exists."""
    a = run(spark, seed, model_dir, forms=["8-K"])
    b = run(spark, seed, model_dir, ciks=[999999999])

    assert (a.chunks_selected, a.chunks_embedded, a.chunks_remaining) == (0, 0, 0)
    assert (b.chunks_selected, b.chunks_embedded, b.chunks_remaining) == (0, 0, 0)
    assert spark.table(seed.output).count() == 0


def test_filters_with_max_chunks_take_the_first_hashes_of_the_filtered_set(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """forms + ciks + max_chunks=3: first 3 hashes of the filtered chunks; the rest remain."""
    filtered = ids_where(seed, forms=["10-K"], ciks=[CIKS[0]])
    expected = [c for c in hash_order(spark, seed.chunks) if c in filtered][:3]
    assert len(filtered) > 3

    stats = run(spark, seed, model_dir, forms=["10-K"], ciks=[CIKS[0]], max_chunks=3)

    assert set(ids_in(spark, seed.output)) == set(expected)
    assert (stats.chunks_selected, stats.chunks_embedded) == (len(filtered), 3)
    assert stats.chunks_remaining == len(filtered) - 3


def test_the_anti_join_runs_before_the_limit(spark: SparkSession, seed: Seed, model_dir: Path):
    """Already-embedded chunks must not consume the `max_chunks` budget of a later filtered run.

    Wave 1 (unfiltered, 10) takes the first 10 hashes. A second run for 10-K only, max 10, must
    then embed 10 NEW 10-K chunks: the first 10 by hash among 10-K chunks still lacking a vector.
    A job that applies LIMIT before the anti-join would embed fewer (or none) here.
    """
    order = hash_order(spark, seed.chunks)
    run(spark, seed, model_dir, max_chunks=10)
    done = set(ids_in(spark, seed.output))
    tenk = ids_where(seed, forms=["10-K"])
    lacking = [c for c in order if c in tenk and c not in done]
    assert len(lacking) >= 10

    stats = run(spark, seed, model_dir, forms=["10-K"], max_chunks=10)

    assert set(ids_in(spark, seed.output)) - done == set(lacking[:10])
    assert stats.chunks_embedded == 10
    assert stats.chunks_selected == len(lacking)
    assert stats.chunks_remaining == len(lacking) - 10


# --------------------------------------------------------------------------------------------
# 7. partitioning and batch size do not change the result
# --------------------------------------------------------------------------------------------


def test_small_partitions_and_batches_give_the_same_vectors_as_one_partition(
    spark: SparkSession, seed: Seed, model_dir: Path, lake_namespace: str
):
    """`rows_per_partition=10, batch_size=3` (4+ tasks, 14 batches) equals the single-task run.

    Compared by cosine, not exact equality: batch padding may change float rounding slightly.
    The point is that no row is lost, duplicated or shifted to another chunk_id by the
    partition/batch plumbing.
    """
    run(spark, seed, model_dir, rows_per_partition=10_000)
    small = f"{lake_namespace}.small"

    stats = run(spark, seed, model_dir, output=small, rows_per_partition=10, batch_size=3)

    assert stats.chunks_embedded == len(seed.rows)
    one, many = vectors(spark, seed.output), vectors(spark, small)
    assert set(one) == set(many) == seed.ids
    assert spark.table(small).count() == len(seed.rows)
    assert all(cosine(one[k], many[k]) >= 0.99999 for k in one)


# --------------------------------------------------------------------------------------------
# 9. stats
# --------------------------------------------------------------------------------------------


def test_embedstats_is_a_frozen_dataclass_with_the_documented_fields():
    """The metrics JSON, the Glue log line and the cost report all read exactly these fields."""
    assert dataclasses.is_dataclass(EmbedStats)
    assert [f.name for f in dataclasses.fields(EmbedStats)] == [
        "chunks_selected",
        "chunks_embedded",
        "chunks_remaining",
        "seconds_embedding",
        "chunks_per_minute",
        "onnx_sha256",
        "dim",
    ]
    stats = EmbedStats(1, 1, 0, 1.0, 60.0, ARTIFACT_ONNX_SHA256, DIM)
    with pytest.raises(dataclasses.FrozenInstanceError):
        stats.dim = 5  # type: ignore[misc]


def test_stats_are_consistent_with_what_was_written(
    spark: SparkSession, seed: Seed, model_dir: Path
):
    """embedded == rows written; selected == embedded + remaining; rate and sha are real.

    `onnx_sha256` equals the pinned constant: the job verified the staged model rather than
    copying a constant into the stats.
    """
    stats = run(spark, seed, model_dir)

    assert isinstance(stats, EmbedStats)
    assert stats.chunks_embedded == spark.table(seed.output).count() == len(seed.rows)
    assert stats.chunks_selected == stats.chunks_embedded + stats.chunks_remaining
    assert stats.chunks_remaining == 0
    assert stats.seconds_embedding > 0
    assert stats.chunks_per_minute > 0
    assert stats.chunks_per_minute == pytest.approx(
        stats.chunks_embedded / stats.seconds_embedding * 60, rel=0.05
    )
    assert stats.onnx_sha256 == ARTIFACT_ONNX_SHA256
    assert stats.dim == DIM == 384


# --------------------------------------------------------------------------------------------
# adversarial: model integrity and argument validation
# --------------------------------------------------------------------------------------------


def test_a_model_dir_whose_onnx_does_not_match_the_pinned_sha_aborts_the_job(
    spark: SparkSession, seed: Seed, model_dir: Path, tmp_path: Path
):
    """A tampered ONNX is refused even when its own MODEL_FILES.json agrees with it.

    The staged manifest only proves the copy is faithful to what was uploaded; the job must also
    check the pinned `ARTIFACT_ONNX_SHA256`, or any wrong model would silently produce vectors
    that the manifest later claims came from the pinned artifact. Nothing is written.
    """
    bad = tmp_path / "bad-model"
    shutil.copytree(model_dir, bad)
    onnx = bad / ARTIFACT_ONNX_FILE
    data = bytearray(onnx.read_bytes())
    data[len(data) // 2] ^= 0xFF
    onnx.write_bytes(bytes(data))
    write_manifest(bad)  # the manifest now agrees with the tampered file

    with pytest.raises(Exception) as err:  # noqa: PT011 - Py4J may wrap the error type
        run(spark, seed, bad)

    assert "sha256" in str(err.value) or "ModelIntegrityError" in str(err.value)
    if spark.catalog.tableExists(seed.output):
        assert spark.table(seed.output).count() == 0


def test_a_missing_model_location_fails_and_writes_nothing(
    spark: SparkSession, seed: Seed, tmp_path: Path
):
    """A model location without files is an error, never a fallback to a download."""
    with pytest.raises(Exception):  # noqa: B017, PT011 - the exact type is the implementer's call
        run(spark, seed, tmp_path / "no-such-model")

    if spark.catalog.tableExists(seed.output):
        assert spark.table(seed.output).count() == 0


@pytest.mark.parametrize(
    "bad",
    [{"rows_per_partition": 0}, {"batch_size": 0}, {"max_chunks": -1}],
    ids=["rows_per_partition=0", "batch_size=0", "max_chunks=-1"],
)
def test_nonsense_numeric_arguments_raise_value_error_before_writing(
    spark: SparkSession, seed: Seed, model_dir: Path, bad: dict[str, int]
):
    """Zero or negative sizes are rejected up front (`ValueError`), not turned into an empty run.

    `max_chunks=0` is deliberately not tested: whether it means "nothing" or "no limit" is the
    implementer's decision to document.
    """
    kwargs: dict[str, object] = {"rows_per_partition": 20} | bad

    with pytest.raises(ValueError):
        embed_chunks(
            spark,
            chunks_table=seed.chunks,
            output_table=seed.output,
            model_location=str(model_dir),
            **kwargs,  # type: ignore[arg-type]
        )

    if spark.catalog.tableExists(seed.output):
        assert spark.table(seed.output).count() == 0


# --------------------------------------------------------------------------------------------
# 10. main(): run in a subprocess with its own warehouse (see module docstring)
# --------------------------------------------------------------------------------------------

_SEED_SCRIPT = """
import json, sys
from pipelines.common.spark_session import spark_session
from pipelines.spark.curated_schemas import CHUNKS
rows = json.load(open(sys.argv[1]))
spark = spark_session("local", app_name="seed")
spark.sql("CREATE NAMESPACE IF NOT EXISTS lake.cli")
df = spark.createDataFrame([tuple(r[f.name] for f in CHUNKS.fields) for r in rows], CHUNKS)
df.writeTo("lake.cli.chunks").using("iceberg").create()
spark.stop()
"""

_MAIN_SCRIPT = (
    "import sys; from pipelines.spark.embed_chunks import main; sys.exit(main(sys.argv[1:]))"
)


def _python(code: str, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "PYTHONPATH": os.pathsep.join(
            filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH", "")])
        ),
        "EDGARQA_SPARK_DRIVER_MEMORY": "2g",
        "EDGARQA_LLM_MODE": "stub",
    }
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )


def test_main_writes_the_metrics_json_and_returns_zero(model_dir: Path, tmp_path: Path):
    """`main` parses the documented flags, embeds, writes `--metrics-out` JSON, returns 0.

    Run as a subprocess in `tmp_path` (the local catalog warehouse is `./warehouse`), because
    `main` owns its SparkSession lifecycle. Run 1 uses `--forms 10-K --ciks a,b`; run 2 has no
    filters and must pick up exactly the rest, so the flag parsing (comma lists, ints) is covered
    end to end. The JSON carries every `EmbedStats` field.
    """
    rows = base_rows()
    rows_json = tmp_path / "rows.json"
    rows_json.write_text(json.dumps(rows), encoding="utf-8")
    seeded = _python(_SEED_SCRIPT, [str(rows_json)], tmp_path)
    assert seeded.returncode == 0, seeded.stderr[-2000:]

    wanted = [r for r in rows if r["form"] == "10-K" and r["cik"] in (CIKS[0], CIKS[1])]
    common = [
        "--env", "local",
        "--chunks-table", "lake.cli.chunks",
        "--output-table", "lake.cli.chunk_embeddings",
        "--model-location", str(model_dir),
        "--rows-per-partition", "20",
        "--batch-size", "16",
    ]  # fmt: skip
    metrics = tmp_path / "m.json"

    first = _python(
        _MAIN_SCRIPT,
        [
            *common,
            "--forms",
            "10-K",
            "--ciks",
            f"{CIKS[0]},{CIKS[1]}",
            "--metrics-out",
            str(metrics),
        ],
        tmp_path,
    )
    assert first.returncode == 0, first.stderr[-2000:]
    m1 = json.loads(metrics.read_text(encoding="utf-8"))
    assert set(m1) >= {f.name for f in dataclasses.fields(EmbedStats)}
    assert m1["chunks_embedded"] == len(wanted)
    assert m1["chunks_selected"] == len(wanted)
    assert m1["chunks_remaining"] == 0
    assert m1["onnx_sha256"] == ARTIFACT_ONNX_SHA256
    assert m1["dim"] == 384
    assert m1["chunks_per_minute"] > 0

    second = _python(_MAIN_SCRIPT, [*common, "--metrics-out", str(metrics)], tmp_path)
    assert second.returncode == 0, second.stderr[-2000:]
    m2 = json.loads(metrics.read_text(encoding="utf-8"))
    assert m2["chunks_embedded"] == len(rows) - len(wanted)
    assert m2["chunks_remaining"] == 0
