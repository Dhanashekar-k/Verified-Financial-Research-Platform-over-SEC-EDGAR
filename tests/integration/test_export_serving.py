"""Integration tests for ``pipelines/spark/export_serving.py`` (test-first; module not written yet).

The job turns the curated lake tables into the serving export (contract sections 2.2 and 2.3,
ADR-035, ADR-037): ``{export_root}/corpus_version=N/{companies,facts,chunks}/*.parquet`` plus
``_MANIFEST.json``, and records the export in ``{namespace}.corpus_versions``.

Everything runs on the local Spark + Iceberg session from ``conftest`` with the REAL contract
schemas (FACTS, CHUNKS, COMPANIES, CHUNK_EMBEDDINGS) and a local ``export_root``; nothing is
mocked and nothing touches AWS.

How the tests read the output (the implementer can rely on this):

* Values and Parquet footers are read with ``pyarrow`` straight from the files on disk, never
  through Spark, so a write path that only "works" through Spark's own reader is caught.
* Column TYPES are asserted through ``spark.read.parquet(table_dir).dtypes`` (``decimal(28,4)``,
  ``array<bigint>``, ``array<float>``...), the same spelling the contract uses.
* "Every Parquet file" means every ``*.parquet`` below the table directory; dotfiles (``.crc``)
  and ``_SUCCESS`` are ignored. Part-file NAMES are not pinned (Spark puts a UUID in them), only
  that manifest paths are relative to the version directory and start with ``<table>/``.
* Vectors are compared with ``np.array_equal`` on float32: the export copies stored float32
  values, it does not recompute them.

Seed layout (companies / facts / chunks) is documented next to each builder below.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyspark.sql import Row, SparkSession

from pipelines.common.embedding import (
    ARTIFACT_HF_REPO,
    ARTIFACT_HF_REVISION,
    ARTIFACT_ONNX_FILE,
    ARTIFACT_ONNX_SHA256,
    DIM,
    MODEL_NAME,
)
from pipelines.common.manifest import MANIFEST_NAME, Manifest, footer_rows, read_manifest
from pipelines.spark import serving_scope
from pipelines.spark.curated_schemas import (
    CHUNK_EMBEDDINGS,
    CHUNKS,
    COMPANIES,
    CORPUS_VERSIONS,
    FACTS,
)
from pipelines.spark.export_serving import ExportError, export_serving
from pipelines.spark.iceberg_util import latest_snapshot_id

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 22, 3, 12, 0, tzinfo=UTC)
GIT_SHA = "abc1234"
EXTRA_CIKS = (5,)

FACT_COLUMNS = [
    "fact_id", "fact_key", "cik", "tag", "taxonomy", "uom", "qtrs", "period_start", "period_end",
    "fiscal_year", "fiscal_period", "calendar_frame", "value", "accession", "form", "filed",
    "is_latest", "is_original", "is_restated", "anomaly_score", "derived_from",
]  # fmt: skip
FACT_DTYPES = {
    "fact_id": "bigint", "fact_key": "bigint", "cik": "int", "tag": "string",
    "taxonomy": "string", "uom": "string", "qtrs": "int", "period_start": "date",
    "period_end": "date", "fiscal_year": "int", "fiscal_period": "string",
    "calendar_frame": "string", "value": "decimal(28,4)", "accession": "string",
    "form": "string", "filed": "date", "is_latest": "boolean", "is_original": "boolean",
    "is_restated": "boolean", "anomaly_score": "float", "derived_from": "array<bigint>",
}  # fmt: skip
CHUNK_COLUMNS = [
    "chunk_id", "cik", "accession", "form", "fiscal_year", "fiscal_period", "section",
    "section_title", "chunk_index", "text", "n_tokens", "is_table", "source_url", "anchor",
    "page", "doc_source", "tenant_id", "embedding",
]  # fmt: skip
CHUNK_DTYPES = {
    "chunk_id": "string", "cik": "int", "accession": "string", "form": "string",
    "fiscal_year": "int", "fiscal_period": "string", "section": "string",
    "section_title": "string", "chunk_index": "int", "text": "string", "n_tokens": "int",
    "is_table": "boolean", "source_url": "string", "anchor": "string", "page": "int",
    "doc_source": "string", "tenant_id": "int", "embedding": "array<float>",
}  # fmt: skip
COMPANY_COLUMNS = ["cik", "name", "ticker", "sic", "fye_mmdd", "aliases"]
COMPANY_DTYPES = {
    "cik": "int", "name": "string", "ticker": "string", "sic": "int", "fye_mmdd": "string",
    "aliases": "array<string>",
}  # fmt: skip

BIG = Decimal("999999999999999999999999.9999")  # 24 + 4 digits: fills decimal(28,4)


# --------------------------------------------------------------------------------------------
# seeds
# --------------------------------------------------------------------------------------------

# COMPANIES. 1: S&P only (financebench NULL). 2: FinanceBench only (aliases NULL). 3: in neither.
# 4: both flags NULL. 5: in neither (only reachable through extra_ciks). 6: sp500 NULL, fb True.
COMPANY_SEEDS: list[tuple[Any, ...]] = [
    (1, "Apple Inc.", "AAPL", 3571, "0930", True, None, ["Apple Computer"]),
    (2, "FB Only Corp", "FBO", 2834, "1231", False, True, None),
    (3, "Neither Corp", "NEI", None, None, False, False, []),
    (4, "Unflagged Corp", None, None, None, None, None, []),
    (5, "Extra Corp", "EXT", 7372, "0630", False, False, ["Extra", "XTR"]),
    (6, "Null Sp500 Corp", "NSP", 1311, "1231", None, True, []),
]


def fact(fid: int, cik: int = 1, **kw: Any) -> dict[str, Any]:
    """One `curated.facts` row with every contract column set; `kw` overrides."""
    row: dict[str, Any] = {
        "fact_id": fid, "fact_key": fid * 10, "cik": cik, "tag": "Revenues",
        "taxonomy": "us-gaap", "is_custom": False, "uom": "USD", "qtrs": 4,
        "period_start": date(2022, 10, 1), "period_end": date(2023, 9, 30),
        "fiscal_year": 2023, "fiscal_period": "FY", "calendar_frame": "CY2023",
        "value": Decimal("1234.5678"), "accession": f"{cik:010d}-23-000001", "form": "10-K",
        "filed": date(2023, 11, 3), "is_latest": True, "is_original": False, "n_versions": 1,
        "is_restated": False, "dq_flags": [], "derived_from": None, "anomaly_score": 0.5,
    }  # fmt: skip
    row.update(kw)
    return row


# Rows that must be exported when extra_ciks=(5,).
PASSING_FACTS = [
    fact(101),  # is_latest only
    fact(102, is_latest=False, is_original=True),  # is_original only
    fact(103, is_latest=True, is_original=True, is_restated=True),
    fact(104, is_latest=None, is_original=True),  # NULL OR TRUE is TRUE
    fact(105, cik=2, taxonomy="dei", uom="shares", is_restated=None),  # NULL -> false
    fact(106, cik=2, taxonomy="srt", uom="USD/shares"),
    fact(107, cik=6, uom="pure", value=BIG, derived_from=[11, 12], anomaly_score=0.25),
    fact(108, cik=5),  # in scope only through extra_ciks
    fact(109, qtrs=0, period_start=None, anomaly_score=None, value=Decimal("-0.0001")),
]
EXTRA_ONLY_IDS = {108}
# Each row fails exactly one condition.
FAILING_FACTS = [
    fact(201, cik=3),  # company flagged in neither
    fact(202, cik=4),  # both flags NULL
    fact(203, cik=99),  # cik not in companies
    fact(204, taxonomy="ifrs-full"),
    fact(205, taxonomy="msft", is_custom=True),
    fact(206, taxonomy=None),
    fact(207, uom="EUR"),
    fact(208, uom=None),
    fact(209, is_latest=False, is_original=False),
    fact(210, is_latest=None, is_original=None),
    fact(211, is_latest=None, is_original=False),
]


def unit_vector(rng: np.random.Generator) -> np.ndarray:
    v = rng.normal(size=DIM)
    return (v / np.linalg.norm(v)).astype(np.float32)


def chunk(cid: str, **kw: Any) -> dict[str, Any]:
    """One `curated.chunks` row with every contract column set; `kw` overrides."""
    row: dict[str, Any] = {
        "chunk_id": cid, "cik": 1, "accession": "0000000001-23-000001", "form": "10-K",
        "fiscal_year": 2023, "fiscal_period": "FY", "section": "7", "section_title": "MD&A",
        "chunk_index": 0, "text": f"Text of {cid}.", "n_tokens": 100, "is_table": False,
        "source_url": "https://www.sec.gov/Archives/edgar/data/1/x.htm", "anchor": "a1",
        "page": None, "doc_source": "edgar_html", "corpus_version": 9,
    }  # fmt: skip
    row.update(kw)
    return row


# (chunk row, has_embedding)
EXPORTED_CHUNKS = [
    chunk("c1", text='Revenue was $1,234 "million".\nSecond line - café – done.',
          is_table=True, chunk_index=3),
    chunk("c2", form="10-K/A", n_tokens=6),  # boundary: 6 > 5 stays
    chunk("c3", form="10-Q", doc_source="financebench_pdf", cik=2, page=12, anchor=None),
    chunk("c4", form=None, doc_source="financebench_pdf", cik=2, n_tokens=20),
    chunk("c5", form="8-K", doc_source="financebench_pdf", cik=2, n_tokens=7),
    chunk("c6", doc_source=None, n_tokens=30),  # 10-K with NULL doc_source is in scope by form
]  # fmt: skip
# In scope and embedded, but n_tokens <= 5: embedded in the lake, dropped at export (ADR-035).
TINY_CHUNKS = [
    chunk("e1", n_tokens=5),  # boundary: 5 is dropped
    chunk("e2", n_tokens=0, text=""),
    chunk("e3", form="8-K", doc_source="financebench_pdf", cik=2, n_tokens=3),
]  # fmt: skip
# Out of scope: EDGAR quarterly filings. q_emb has a vector (as after a `--scope all` run) but
# must still not be exported; none of these counts as an exclusion (only in-scope chunks do).
QUARTERLY_CHUNKS = [
    chunk("q1", form="10-Q", n_tokens=100),
    chunk("q2", form="10-Q", n_tokens=3),
    chunk("qa", form="10-Q/A", n_tokens=80),
    chunk("q_emb", form="10-Q", n_tokens=50),
]  # fmt: skip
# Out of scope, not quarterly, no vector: not exported, not an error, not in `not_embedded`.
OTHER_CHUNKS = [chunk("o1", form="8-K", n_tokens=40)]

EMBEDDED_IDS = {c["chunk_id"] for c in [*EXPORTED_CHUNKS, *TINY_CHUNKS]} | {"q_emb"}
EXPORTED_IDS = {c["chunk_id"] for c in EXPORTED_CHUNKS}
N_EXCLUDED = len(TINY_CHUNKS)  # in-scope chunks with n_tokens <= 5
N_NOT_EMBEDDED = len(QUARTERLY_CHUNKS)  # edgar_html 10-Q and 10-Q/A, embedded or not


def vector_table() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(7)
    return {cid: unit_vector(rng) for cid in sorted(EMBEDDED_IDS | {"orphan"})}


# --------------------------------------------------------------------------------------------
# fixtures and helpers
# --------------------------------------------------------------------------------------------


@dataclass
class Lake:
    spark: SparkSession
    ns: str
    root: Path
    vectors: dict[str, np.ndarray]

    def table(self, name: str) -> str:
        return f"{self.ns}.{name}"


def write_table(
    spark: SparkSession, table: str, schema: Any, rows: list[dict[str, Any]], *, replace=False
) -> None:
    data = [tuple(r[f.name] for f in schema.fields) for r in rows]
    writer = spark.createDataFrame(data, schema).writeTo(table).using("iceberg")
    if replace:
        writer.createOrReplace()
    else:
        writer.create()


def write_embeddings(lake: Lake, ids: set[str], *, replace: bool = False) -> None:
    rows = [
        {
            "chunk_id": cid,
            "model_name": MODEL_NAME,
            "dim": DIM,
            "embedding": [float(x) for x in lake.vectors[cid]],
            "embedded_at": datetime(2026, 11, 21, 12, 0, 0),
        }
        for cid in sorted(ids)
    ]
    write_table(lake.spark, lake.table("chunk_embeddings"), CHUNK_EMBEDDINGS, rows, replace=replace)


@pytest.fixture
def lake(spark: SparkSession, lake_namespace: str, tmp_path: Path) -> Lake:
    """All four source tables seeded in the throwaway namespace; `root` is a fresh local dir."""
    lk = Lake(spark, lake_namespace, tmp_path / "export", vector_table())
    companies = [
        dict(zip((f.name for f in COMPANIES.fields), r, strict=True)) for r in COMPANY_SEEDS
    ]
    write_table(spark, lk.table("companies"), COMPANIES, companies)
    write_table(spark, lk.table("facts"), FACTS, [*PASSING_FACTS, *FAILING_FACTS])
    write_table(
        spark,
        lk.table("chunks"),
        CHUNKS,
        [*EXPORTED_CHUNKS, *TINY_CHUNKS, *QUARTERLY_CHUNKS, *OTHER_CHUNKS],
    )
    write_embeddings(lk, EMBEDDED_IDS | {"orphan"})  # "orphan" has no chunk row
    return lk


def run_export(lk: Lake, **kw: Any) -> Manifest:
    args: dict[str, Any] = {
        "export_root": str(lk.root),
        "namespace": lk.ns,
        "git_sha": GIT_SHA,
        "extra_ciks": EXTRA_CIKS,
        "now": NOW,
    }
    args.update(kw)
    return export_serving(lk.spark, **args)


def version_dir(lk: Lake, n: int) -> Path:
    return lk.root / f"corpus_version={n}"


def parquet_files(vdir: Path, table: str) -> list[Path]:
    return sorted(p for p in (vdir / table).rglob("*.parquet") if not p.name.startswith("."))


def read_rows(vdir: Path, table: str) -> list[dict[str, Any]]:
    files = parquet_files(vdir, table)
    assert files, f"no parquet files under {vdir / table}"
    return pa.concat_tables([pq.read_table(f) for f in files]).to_pylist()


def by_key(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    out = {r[key]: r for r in rows}
    assert len(out) == len(rows), f"duplicate {key} in export"
    return out


def version_rows(spark: SparkSession, ns: str) -> list[Row]:
    table = f"{ns}.corpus_versions"
    if not spark.catalog.tableExists(table):
        return []
    return spark.table(table).orderBy("corpus_version").collect()


def no_export_dirs(root: Path) -> bool:
    return not root.exists() or not any(root.glob("corpus_version=*"))


def expected_fact_row(seed: dict[str, Any]) -> dict[str, Any]:
    row = {c: seed[c] for c in FACT_COLUMNS}
    row["is_restated"] = bool(row["is_restated"])
    return row


@pytest.fixture
def exported(lake: Lake) -> tuple[Lake, Manifest]:
    return lake, run_export(lake)


# --------------------------------------------------------------------------------------------
# 1. layout, codec, manifest file
# --------------------------------------------------------------------------------------------


def test_writes_the_three_tables_and_a_manifest_that_parses(exported: tuple[Lake, Manifest]):
    """Layout `corpus_version=N/{companies,facts,chunks}/*.parquet` + `_MANIFEST.json`; the
    manifest file parses with the shared `Manifest` model (the loader reads it the same way)."""
    lk, manifest = exported
    vdir = version_dir(lk, 1)

    assert manifest.corpus_version == 1
    for table in ("companies", "facts", "chunks"):
        assert parquet_files(vdir, table), table
    on_disk = read_manifest((vdir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert on_disk == manifest


def test_every_parquet_file_is_zstd_compressed(exported: tuple[Lake, Manifest]):
    """The codec is read from each Parquet footer (not assumed): every column chunk of every row
    group of every file is ZSTD. Size matters because the chunks table carries the vectors."""
    lk, _ = exported
    vdir = version_dir(lk, 1)
    seen = 0
    for table in ("companies", "facts", "chunks"):
        for f in parquet_files(vdir, table):
            md = pq.ParquetFile(f).metadata
            for rg in range(md.num_row_groups):
                for col in range(md.num_columns):
                    assert md.row_group(rg).column(col).compression == "ZSTD", (f.name, col)
                    seen += 1
    assert seen > 0


# --------------------------------------------------------------------------------------------
# 2. facts
# --------------------------------------------------------------------------------------------


def test_facts_filter_exports_exactly_the_passing_rows(exported: tuple[Lake, Manifest]):
    """Rows that fail ONE condition each (company flags, taxonomy, uom, latest/original, incl.
    NULL cases) are dropped; rows that pass (incl. `NULL OR TRUE`, each flag alone, each
    taxonomy and uom, and the extra cik) are all exported, and nothing else."""
    lk, manifest = exported
    ids = {r["fact_id"] for r in read_rows(version_dir(lk, 1), "facts")}

    assert ids == {r["fact_id"] for r in PASSING_FACTS}
    assert manifest.facts_rows == len(PASSING_FACTS)


def test_extra_ciks_is_the_only_way_a_non_flagged_company_gets_facts(lake: Lake):
    """Without `extra_ciks` the fact of cik 5 (flagged in neither list) is not exported; every
    other passing row still is."""
    run_export(lake, extra_ciks=())
    ids = {r["fact_id"] for r in read_rows(version_dir(lake, 1), "facts")}

    assert ids == {r["fact_id"] for r in PASSING_FACTS} - EXTRA_ONLY_IDS


def test_fact_rows_are_exported_unchanged_apart_from_the_null_is_restated(
    exported: tuple[Lake, Manifest],
):
    """Values survive exactly: decimal(28,4) to the last digit (a 28-digit value, a negative
    one), dates, flags, derived_from, NULL anomaly_score; is_restated NULL becomes False."""
    lk, _ = exported
    got = by_key(read_rows(version_dir(lk, 1), "facts"), "fact_id")

    for seed in PASSING_FACTS:
        assert got[seed["fact_id"]] == expected_fact_row(seed), seed["fact_id"]
    assert got[107]["value"] == BIG
    assert got[105]["is_restated"] is False


def test_fact_columns_have_the_contract_order_and_types(exported: tuple[Lake, Manifest]):
    """Output columns are exactly the 21 listed in order (no is_custom, n_versions, dq_flags);
    value stays decimal(28,4), derived_from array<bigint>. Checked in every file's footer and
    through Spark's reader for the type spelling."""
    lk, _ = exported
    vdir = version_dir(lk, 1)

    for f in parquet_files(vdir, "facts"):
        assert pq.ParquetFile(f).schema_arrow.names == FACT_COLUMNS
    assert dict(lk.spark.read.parquet(str(vdir / "facts")).dtypes) == FACT_DTYPES
    assert lk.spark.read.parquet(str(vdir / "facts")).columns == FACT_COLUMNS


# --------------------------------------------------------------------------------------------
# 3. chunks
# --------------------------------------------------------------------------------------------


def test_chunks_are_the_in_scope_embedded_chunks_with_more_than_five_tokens(
    exported: tuple[Lake, Manifest],
):
    """Exported = served_predicate AND n_tokens > 5, joined to its vector. The boundary (5 out,
    6 in), FinanceBench chunks of ANY form (10-Q, 8-K, NULL), a 10-K with NULL doc_source, and
    in-scope tiny chunks that HAVE a vector are all covered; EDGAR 10-Q/10-Q/A (even one with a
    vector), an out-of-scope 8-K and an orphan vector are not exported."""
    lk, manifest = exported
    ids = {r["chunk_id"] for r in read_rows(version_dir(lk, 1), "chunks")}

    assert ids == EXPORTED_IDS
    assert manifest.chunks_rows == len(EXPORTED_IDS)


def test_chunk_rows_keep_every_column_and_carry_the_stored_vector(exported: tuple[Lake, Manifest]):
    """Non-vector columns equal the lake's (unicode, quotes, newline, NULL form/anchor/doc_source
    included); `tenant_id` is NULL on every row; `embedding` is the stored float32 vector of
    that chunk (length 384, exact), never another chunk's."""
    lk, _ = exported
    got = by_key(read_rows(version_dir(lk, 1), "chunks"), "chunk_id")

    for seed in EXPORTED_CHUNKS:
        row = got[seed["chunk_id"]]
        for col in CHUNK_COLUMNS:
            if col in ("tenant_id", "embedding"):
                continue
            assert row[col] == seed[col], (seed["chunk_id"], col)
        assert row["tenant_id"] is None
        vec = np.asarray(row["embedding"], dtype=np.float32)
        assert vec.shape == (DIM,)
        assert np.array_equal(vec, lk.vectors[seed["chunk_id"]]), seed["chunk_id"]


def test_chunk_columns_have_the_contract_order_and_types(exported: tuple[Lake, Manifest]):
    """18 columns in contract order; `corpus_version` is NOT exported; tenant_id is int and
    embedding array<float>. Each Parquet footer carries the same names."""
    lk, _ = exported
    vdir = version_dir(lk, 1)

    for f in parquet_files(vdir, "chunks"):
        assert pq.ParquetFile(f).schema_arrow.names == CHUNK_COLUMNS
    df = lk.spark.read.parquet(str(vdir / "chunks"))
    assert df.columns == CHUNK_COLUMNS
    assert dict(df.dtypes) == CHUNK_DTYPES
    assert "corpus_version" not in df.columns


def test_unembedded_quarterly_chunks_do_not_fail_the_export(lake: Lake):
    """ADR-037: EDGAR 10-Q/10-Q/A chunks have no vector by design. The seed has three of them
    (plus an out-of-scope 8-K) with no embedding and the export succeeds; the check only
    applies to in-scope chunks."""
    manifest = run_export(lake)

    assert manifest.chunks_rows == len(EXPORTED_CHUNKS)


# --------------------------------------------------------------------------------------------
# 4. missing embeddings fail BEFORE anything is written
# --------------------------------------------------------------------------------------------


def drop_embedding(lk: Lake, cid: str) -> None:
    write_embeddings(lk, (EMBEDDED_IDS | {"orphan"}) - {cid}, replace=True)


@pytest.mark.parametrize("missing", ["c1", "c3", "c4"])
def test_in_scope_chunk_without_a_vector_raises_before_writing_anything(lake: Lake, missing: str):
    """An in-scope chunk (EDGAR 10-K, FinanceBench 10-Q, FinanceBench NULL form) with
    n_tokens > 5 and no embedding is a gap: `ExportError`, and NO version directory, manifest or
    `corpus_versions` row exists afterwards (the check is a pre-flight, not a rollback)."""
    drop_embedding(lake, missing)

    with pytest.raises(ExportError):
        run_export(lake)

    assert no_export_dirs(lake.root)
    assert version_rows(lake.spark, lake.ns) == []


def test_a_failed_reexport_leaves_the_previous_export_untouched(lake: Lake):
    """After a good export of version 1, a gap appears and `--version 1` is re-run: it raises
    `ExportError` and version 1's files, manifest and corpus_versions row are byte-for-byte
    what they were (a failed re-export must not destroy the last good one)."""
    first = run_export(lake)
    vdir = version_dir(lake, 1)
    before_manifest = (vdir / MANIFEST_NAME).read_bytes()
    before_files = {p: p.read_bytes() for p in vdir.rglob("*.parquet")}
    before_rows = version_rows(lake.spark, lake.ns)
    drop_embedding(lake, "c2")

    with pytest.raises(ExportError):
        run_export(lake, version=1)

    assert (vdir / MANIFEST_NAME).read_bytes() == before_manifest
    assert {p: p.read_bytes() for p in vdir.rglob("*.parquet")} == before_files
    assert version_rows(lake.spark, lake.ns) == before_rows
    assert first.corpus_version == 1


def test_tiny_in_scope_chunk_without_a_vector_is_not_a_gap(lake: Lake):
    """The pre-flight only covers chunks that WOULD be exported (n_tokens > 5): an in-scope
    n_tokens <= 5 chunk lacking a vector is dropped anyway, so no error, and it still counts in
    `chunk_exclusions` because the count is over in-scope chunks, not over vectors."""
    drop_embedding(lake, "e1")

    manifest = run_export(lake)

    assert manifest.chunk_exclusions.count == N_EXCLUDED
    assert manifest.chunks_rows == len(EXPORTED_CHUNKS)


# --------------------------------------------------------------------------------------------
# 5. companies
# --------------------------------------------------------------------------------------------


def test_companies_are_all_exported_with_contract_columns_and_empty_alias_arrays(
    exported: tuple[Lake, Manifest],
):
    """Every company is exported (flags decide only the facts filter); columns are
    cik,name,ticker,sic,fye_mmdd,aliases (no in_sp500/in_financebench); NULL aliases become []
    while other NULLs stay NULL."""
    lk, manifest = exported
    vdir = version_dir(lk, 1)
    got = by_key(read_rows(vdir, "companies"), "cik")

    assert set(got) == {1, 2, 3, 4, 5, 6}
    assert manifest.companies_rows == 6
    for f in parquet_files(vdir, "companies"):
        assert pq.ParquetFile(f).schema_arrow.names == COMPANY_COLUMNS
    assert dict(lk.spark.read.parquet(str(vdir / "companies")).dtypes) == COMPANY_DTYPES
    assert got[1] == {
        "cik": 1, "name": "Apple Inc.", "ticker": "AAPL", "sic": 3571, "fye_mmdd": "0930",
        "aliases": ["Apple Computer"],
    }  # fmt: skip
    assert got[2]["aliases"] == []
    assert got[4]["ticker"] is None and got[4]["sic"] is None
    assert got[5]["aliases"] == ["Extra", "XTR"]


# --------------------------------------------------------------------------------------------
# 6. manifest values
# --------------------------------------------------------------------------------------------


def test_manifest_headline_values_and_row_counts(exported: tuple[Lake, Manifest]):
    """corpus_version, git_sha, created_at (from `now`), the embedding model and dim, and the
    three row counts equal what was written."""
    lk, m = exported
    vdir = version_dir(lk, 1)

    assert m.corpus_version == 1
    assert m.git_sha == GIT_SHA
    assert datetime.fromisoformat(m.created_at.replace("Z", "+00:00")) == NOW
    assert m.embedding_model == MODEL_NAME == "BAAI/bge-small-en-v1.5"
    assert m.dim == DIM == 384
    assert m.facts_rows == len(read_rows(vdir, "facts"))
    assert m.chunks_rows == len(read_rows(vdir, "chunks"))
    assert m.companies_rows == len(read_rows(vdir, "companies"))


def test_created_at_defaults_to_the_current_utc_time(lake: Lake):
    """With no `now`, created_at is an ISO-8601 timestamp of (about) the current UTC time; a
    naive value is read as UTC."""
    before = datetime.now(UTC)
    m = run_export(lake, now=None)
    parsed = datetime.fromisoformat(m.created_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)

    assert abs((parsed - before).total_seconds()) < 300


def test_manifest_pins_the_embedding_artifact(exported: tuple[Lake, Manifest]):
    """The artifact block comes from the `pipelines.common.embedding` constants (the loader and
    the query embedder must use the very same ONNX file)."""
    _, m = exported
    art = m.embedding_artifact

    assert (art.hf_repo, art.hf_revision, art.onnx_file, art.onnx_sha256) == (
        ARTIFACT_HF_REPO,
        ARTIFACT_HF_REVISION,
        ARTIFACT_ONNX_FILE,
        ARTIFACT_ONNX_SHA256,
    )


def test_manifest_chunk_exclusions_count_in_scope_tiny_chunks_only(
    exported: tuple[Lake, Manifest],
):
    """`chunk_exclusions` = {rule 'n_tokens <= 5', count = in-scope chunks with n_tokens <= 5}
    (3 here). The two EDGAR 10-Q chunks with n_tokens <= 5 / out-of-scope rows are NOT counted;
    `chunks_rows` is the count AFTER the exclusion."""
    _, m = exported

    assert m.chunk_exclusions.rule == "n_tokens <= 5"
    assert m.chunk_exclusions.count == N_EXCLUDED == 3
    assert m.chunks_rows == len(EXPORTED_CHUNKS)


def test_manifest_embedding_scope_records_the_not_embedded_decision(
    exported: tuple[Lake, Manifest],
):
    """The scope rule is the shared `serving_scope.SERVED_RULE`; `not_embedded` carries
    `NOT_EMBEDDED_RULE`, the exact number of edgar_html 10-Q/10-Q/A chunks in curated.chunks
    (4: includes one that happens to have a vector; excludes the FinanceBench 10-Q) and a
    non-empty reason."""
    _, m = exported
    scope = m.embedding_scope

    assert scope.rule == serving_scope.SERVED_RULE
    assert scope.not_embedded.rule == serving_scope.NOT_EMBEDDED_RULE
    assert scope.not_embedded.count == N_NOT_EMBEDDED == 4
    assert scope.not_embedded.reason.strip()


def test_manifest_snapshots_are_the_sources_latest_snapshot_ids(exported: tuple[Lake, Manifest]):
    """`iceberg_snapshots` has exactly the four `curated.*` keys (fixed names, whatever the
    namespace) and each value is `latest_snapshot_id` of that source table, so a run can be
    reproduced by time travel. The export does not write to its sources."""
    lk, m = exported

    assert set(m.iceberg_snapshots) == {
        "curated.facts",
        "curated.chunks",
        "curated.chunk_embeddings",
        "curated.companies",
    }
    for key, snap in m.iceberg_snapshots.items():
        table = lk.table(key.removeprefix("curated."))
        assert snap == latest_snapshot_id(lk.spark, table), key
        assert isinstance(snap, int)


def test_manifest_filters_describe_facts_and_chunks(exported: tuple[Lake, Manifest]):
    """`filters` has exactly the keys facts and chunks, each a non-empty human-readable string."""
    _, m = exported

    assert set(m.filters) == {"facts", "chunks"}
    assert all(isinstance(v, str) and v.strip() for v in m.filters.values())


# --------------------------------------------------------------------------------------------
# 6b / 9. files list and footers
# --------------------------------------------------------------------------------------------


def test_manifest_files_list_every_parquet_file_with_footer_rows_and_size(
    exported: tuple[Lake, Manifest],
):
    """Per table, `files` lists exactly the Parquet files on disk (no more, no fewer), paths
    relative to the version dir (`<table>/...parquet`), `rows` from the file's footer and
    `bytes` its size on disk."""
    lk, m = exported
    vdir = version_dir(lk, 1)

    assert set(m.files) == {"companies", "facts", "chunks"}
    for table, entries in m.files.items():
        listed = {e.path for e in entries}
        on_disk = {p.relative_to(vdir).as_posix() for p in parquet_files(vdir, table)}
        assert listed == on_disk and len(listed) == len(entries)
        for e in entries:
            assert e.path.startswith(f"{table}/") and e.path.endswith(".parquet")
            assert e.rows == footer_rows(vdir / e.path)
            assert e.bytes == (vdir / e.path).stat().st_size > 0


def test_footer_row_sums_equal_the_manifest_counts_for_every_table(
    exported: tuple[Lake, Manifest],
):
    """The loader's check, done here independently with pyarrow: for each table the footers'
    num_rows sum equals the manifest headline count and the sum over `files`."""
    lk, m = exported
    vdir = version_dir(lk, 1)

    for table in ("companies", "facts", "chunks"):
        footers = sum(pq.ParquetFile(f).metadata.num_rows for f in parquet_files(vdir, table))
        assert footers == m.expected_rows(table) == sum(e.rows for e in m.files[table]), table


# --------------------------------------------------------------------------------------------
# 7. versioning and corpus_versions
# --------------------------------------------------------------------------------------------


def seed_versions(lk: Lake, versions: list[int]) -> None:
    rows = [
        {
            "corpus_version": v, "created_at": datetime(2026, 11, 1), "git_sha": "old",
            "facts_rows": 1, "chunks_rows": 2, "chunks_excluded": 3, "embedding_model": "m",
            "embedding_artifact_sha256": "s", "dim": 384, "facts_snapshot_id": 1,
            "chunks_snapshot_id": 2, "chunk_embeddings_snapshot_id": 3, "export_uri": "x",
        }
        for v in versions
    ]  # fmt: skip
    write_table(lk.spark, lk.table("corpus_versions"), CORPUS_VERSIONS, rows)


def test_first_export_creates_corpus_versions_with_the_contract_schema_as_version_1(lake: Lake):
    """With no `corpus_versions` table, version None is 1 and the table is created with the
    `CORPUS_VERSIONS` columns and types."""
    assert not lake.spark.catalog.tableExists(lake.table("corpus_versions"))

    m = run_export(lake)

    assert m.corpus_version == 1
    schema = lake.spark.table(lake.table("corpus_versions")).schema
    assert [(f.name, f.dataType.simpleString()) for f in schema.fields] == [
        (f.name, f.dataType.simpleString()) for f in CORPUS_VERSIONS.fields
    ]


def test_an_existing_empty_corpus_versions_table_also_gives_version_1(lake: Lake):
    """An empty (not missing) table: max is NULL, so the first version is 1, and the existing
    table is reused."""
    seed_versions(lake, [])

    m = run_export(lake)

    assert m.corpus_version == 1
    assert [r.corpus_version for r in version_rows(lake.spark, lake.ns)] == [1]


def test_version_none_is_max_plus_one_and_ignores_directories_on_disk(lake: Lake):
    """The version comes from `corpus_versions` (max + 1), not from what is in the export
    root: a stray `corpus_version=7` directory with an empty table still gives version 1
    and is left alone; after rows 1..3 exist, the next export is 4."""
    stray = lake.root / "corpus_version=7"
    stray.mkdir(parents=True)
    (stray / "keep.txt").write_text("x")
    seed_versions(lake, [])
    assert run_export(lake).corpus_version == 1
    assert (stray / "keep.txt").read_text() == "x"

    assert run_export(lake).corpus_version == 2
    assert run_export(lake).corpus_version == 3
    assert run_export(lake).corpus_version == 4
    assert [r.corpus_version for r in version_rows(lake.spark, lake.ns)] == [1, 2, 3, 4]


@pytest.mark.parametrize("version", [3, 4])
def test_explicit_version_may_be_the_current_max_or_the_next_one(lake: Lake, version: int):
    """With versions 1..3 recorded, `version=3` (re-export of the max) and `version=4` (next)
    are accepted and the manifest and directory carry that number."""
    seed_versions(lake, [1, 2, 3])

    m = run_export(lake, version=version)

    assert m.corpus_version == version
    assert (version_dir(lake, version) / MANIFEST_NAME).is_file()


@pytest.mark.parametrize("version", [-1, 0, 1, 2, 5, 99])
def test_explicit_version_other_than_max_or_next_raises_and_writes_nothing(
    lake: Lake, version: int
):
    """With versions 1..3 recorded, anything but 3 or 4 (a gap, an old version, zero or a
    negative number) is a `ValueError`: no directory, no manifest, `corpus_versions` unchanged."""
    seed_versions(lake, [1, 2, 3])
    before = version_rows(lake.spark, lake.ns)

    with pytest.raises(ValueError):
        run_export(lake, version=version)

    assert no_export_dirs(lake.root)
    assert version_rows(lake.spark, lake.ns) == before


def test_explicit_version_on_an_empty_table_must_be_1(lake: Lake):
    """No versions yet: the only valid explicit version is 1; 2 is a gap -> ValueError and
    nothing written (no table rows, no directory)."""
    with pytest.raises(ValueError):
        run_export(lake, version=2)

    assert no_export_dirs(lake.root)
    assert version_rows(lake.spark, lake.ns) == []
    assert run_export(lake, version=1).corpus_version == 1


def test_corpus_versions_row_mirrors_the_manifest(exported: tuple[Lake, Manifest]):
    """One row for the version with the manifest's counts, `chunks_excluded` =
    chunk_exclusions.count, the artifact sha256, dim, model, git_sha, the three snapshot ids
    and an export_uri that points at the version directory."""
    lk, m = exported
    (row,) = version_rows(lk.spark, lk.ns)

    assert row.corpus_version == 1
    assert row.created_at is not None
    assert row.git_sha == m.git_sha == GIT_SHA
    assert row.facts_rows == m.facts_rows
    assert row.chunks_rows == m.chunks_rows
    assert row.chunks_excluded == m.chunk_exclusions.count == N_EXCLUDED
    assert row.embedding_model == m.embedding_model
    assert row.embedding_artifact_sha256 == m.embedding_artifact.onnx_sha256
    assert row.dim == m.dim
    assert row.facts_snapshot_id == m.iceberg_snapshots["curated.facts"]
    assert row.chunks_snapshot_id == m.iceberg_snapshots["curated.chunks"]
    assert row.chunk_embeddings_snapshot_id == m.iceberg_snapshots["curated.chunk_embeddings"]
    assert row.export_uri.rstrip("/").endswith(f"{lk.root.name}/corpus_version=1")


# --------------------------------------------------------------------------------------------
# 8. idempotent re-export
# --------------------------------------------------------------------------------------------


def comparable(m: Manifest) -> dict[str, Any]:
    """The manifest minus the part-file names (Spark puts a UUID in them)."""
    data = m.model_dump()
    data["files"] = {t: sum(e["rows"] for e in es) for t, es in data["files"].items()}
    return data


def test_reexporting_the_same_version_gives_the_same_result_and_one_row(lake: Lake):
    """Run twice with `version=1`: same manifest (counts, scope, snapshots, per-table file row
    totals), same exported rows, and `corpus_versions` still holds exactly one row for 1."""
    first = run_export(lake)
    rows_first = {t: sorted(map(repr, read_rows(version_dir(lake, 1), t)), key=str)
                  for t in ("companies", "facts", "chunks")}  # fmt: skip

    second = run_export(lake, version=1)

    assert comparable(second) == comparable(first)
    assert {t: sorted(map(repr, read_rows(version_dir(lake, 1), t)), key=str)
            for t in ("companies", "facts", "chunks")} == rows_first  # fmt: skip
    assert [r.corpus_version for r in version_rows(lake.spark, lake.ns)] == [1]
    assert list(lake.root.glob("corpus_version=*")) == [version_dir(lake, 1)]


def test_reexport_overwrites_the_table_directory_instead_of_appending(lake: Lake):
    """Stray files planted in a table directory (a copied Parquet file and a text file) are gone
    after the re-export, in every table directory I plant them in: the directory is replaced,
    never appended to, so the row totals cannot double."""
    run_export(lake)
    vdir = version_dir(lake, 1)
    strays = []
    for table in ("facts", "chunks", "companies"):
        real = parquet_files(vdir, table)[0]
        for name in ("stray.parquet", "stray.txt"):
            target = vdir / table / name
            if name.endswith(".parquet"):
                shutil.copy(real, target)
            else:
                target.write_text("stale")
            strays.append(target)
    assert all(p.exists() for p in strays)

    m = run_export(lake, version=1)

    assert not any(p.exists() for p in strays)
    for table in ("facts", "chunks", "companies"):
        total = sum(pq.ParquetFile(f).metadata.num_rows for f in parquet_files(vdir, table))
        assert total == m.expected_rows(table)


def test_reexport_after_the_lake_changed_replaces_files_manifest_and_row(lake: Lake):
    """Add a passing fact, re-export version 1: the facts table has the new row (and no
    duplicates), the manifest and the single corpus_versions row carry the new count and the
    NEW facts snapshot id; the older version-2 row of another export is untouched."""
    run_export(lake)
    second = run_export(lake)  # version 2
    v2_row = [r for r in version_rows(lake.spark, lake.ns) if r.corpus_version == 2]
    lake.spark.createDataFrame([tuple(fact(150)[f.name] for f in FACTS.fields)], FACTS).writeTo(
        lake.table("facts")
    ).append()
    new_snapshot = latest_snapshot_id(lake.spark, lake.table("facts"))

    third = run_export(lake, version=2)

    assert third.facts_rows == second.facts_rows + 1
    assert third.iceberg_snapshots["curated.facts"] == new_snapshot
    ids = [r["fact_id"] for r in read_rows(version_dir(lake, 2), "facts")]
    assert sorted(ids) == sorted([*(r["fact_id"] for r in PASSING_FACTS), 150])
    rows = version_rows(lake.spark, lake.ns)
    assert [r.corpus_version for r in rows] == [1, 2]
    assert rows[1].facts_rows == third.facts_rows
    assert rows[1].facts_snapshot_id == new_snapshot
    assert rows[0].facts_rows == second.facts_rows  # version 1 untouched
    assert v2_row[0].facts_rows == second.facts_rows


def test_export_reads_the_sources_without_changing_them(lake: Lake):
    """The export never writes to the four source tables: their latest snapshot ids are the
    same before and after (so the manifest's ids really are the exported state)."""
    tables = ["facts", "chunks", "chunk_embeddings", "companies"]
    before = {t: latest_snapshot_id(lake.spark, lake.table(t)) for t in tables}

    run_export(lake)

    assert {t: latest_snapshot_id(lake.spark, lake.table(t)) for t in tables} == before


# --------------------------------------------------------------------------------------------
# 10. main(): own process, own warehouse (main owns its SparkSession lifecycle)
# --------------------------------------------------------------------------------------------

_SEED_SCRIPT = """
import sys
from datetime import date
from decimal import Decimal
from pipelines.common.spark_session import spark_session
from pipelines.spark.curated_schemas import CHUNK_EMBEDDINGS, CHUNKS, COMPANIES, FACTS

def rows(schema, items):
    out = []
    for item in items:
        d = {f.name: None for f in schema.fields} | item
        out.append(tuple(d[f.name] for f in schema.fields))
    return out

spark = spark_session("local", app_name="seed")
spark.sql("CREATE NAMESPACE IF NOT EXISTS lake.cli")
def put(name, schema, items):
    spark.createDataFrame(rows(schema, items), schema).writeTo(f"lake.cli.{name}").using(
        "iceberg").create()
put("companies", COMPANIES, [
    {"cik": 1, "name": "A", "in_sp500": True, "aliases": []},
    {"cik": 3, "name": "C", "in_sp500": False, "in_financebench": False, "aliases": []},
    {"cik": 5, "name": "E", "in_sp500": False, "aliases": []},
])
base = dict(taxonomy="us-gaap", uom="USD", is_latest=True, is_original=False,
            value=Decimal("1.0000"), qtrs=4, period_end=date(2023, 9, 30))
put("facts", FACTS, [
    {**base, "fact_id": 1, "fact_key": 1, "cik": 1},
    {**base, "fact_id": 2, "fact_key": 2, "cik": 5},
    {**base, "fact_id": 3, "fact_key": 3, "cik": 3},
])
put("chunks", CHUNKS, [{"chunk_id": "k1", "cik": 1, "form": "10-K", "n_tokens": 50,
                        "doc_source": "edgar_html", "text": "t"}])
MODEL = "BAAI/bge-small-en-v1.5"
put("chunk_embeddings", CHUNK_EMBEDDINGS, [{"chunk_id": "k1", "model_name": MODEL, "dim": 384,
                                            "embedding": [0.0] * 383 + [1.0]}])
spark.stop()
"""

_MAIN_SCRIPT = (
    "import sys; from pipelines.spark.export_serving import main; sys.exit(main(sys.argv[1:]))"
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


def test_main_exports_with_the_documented_flags_and_is_rerunnable(tmp_path: Path):
    """`main` parses --env/--export-root/--namespace/--git-sha/--extra-ciks, exports version 1
    and returns 0; `--version 1` re-runs it (still 0, still one version directory). Facts of
    cik 1 (flagged) and cik 5 (`--extra-ciks 5`) are exported, cik 3's is not. Run in a
    subprocess because `main` stops its own SparkSession."""
    seeded = _python(_SEED_SCRIPT, [], tmp_path)
    assert seeded.returncode == 0, seeded.stderr[-2000:]
    root = tmp_path / "export"
    common = [
        "--env", "local", "--export-root", str(root), "--namespace", "lake.cli",
        "--git-sha", "cafe123", "--extra-ciks", "5",
    ]  # fmt: skip

    first = _python(_MAIN_SCRIPT, common, tmp_path)
    assert first.returncode == 0, first.stderr[-3000:]
    manifest = read_manifest((root / "corpus_version=1" / MANIFEST_NAME).read_text("utf-8"))
    assert (manifest.corpus_version, manifest.git_sha) == (1, "cafe123")
    assert (manifest.facts_rows, manifest.chunks_rows, manifest.companies_rows) == (2, 1, 3)
    ids = {r["fact_id"] for r in read_rows(root / "corpus_version=1", "facts")}
    assert ids == {1, 2}

    again = _python(_MAIN_SCRIPT, [*common, "--version", "1"], tmp_path)
    assert again.returncode == 0, again.stderr[-3000:]
    assert sorted(p.name for p in root.glob("corpus_version=*")) == ["corpus_version=1"]
    assert (
        read_manifest((root / "corpus_version=1" / MANIFEST_NAME).read_text("utf-8")).facts_rows
        == 2
    )


# --------------------------------------------------------------------------------------------
# review fixes: embeddings of the pinned model only, safe export roots, half-written re-exports
# --------------------------------------------------------------------------------------------


def _append_embedding(lk: Lake, cid: str, *, model: str = MODEL_NAME, dim: int = DIM) -> None:
    row = {
        "chunk_id": cid,
        "model_name": model,
        "dim": dim,
        "embedding": [float(x) for x in lk.vectors[cid]],
        "embedded_at": datetime(2026, 11, 22, 12, 0, 0),
    }
    df = lk.spark.createDataFrame(
        [tuple(row[f.name] for f in CHUNK_EMBEDDINGS.fields)], CHUNK_EMBEDDINGS
    )
    df.writeTo(lk.table("chunk_embeddings")).append()


def test_a_chunk_with_two_embeddings_of_the_pinned_model_is_an_error(lake: Lake):
    _append_embedding(lake, "c1")  # c1 is an exported chunk that already has a vector

    with pytest.raises(ExportError, match="more than one"):
        run_export(lake)

    assert no_export_dirs(lake.root)


def test_embeddings_of_another_model_or_dimension_are_ignored_not_exported(lake: Lake):
    # A second, different-model vector for c1 must neither be exported nor count as ambiguity.
    _append_embedding(lake, "c1", model="some/other-model")

    manifest = run_export(lake)

    rows = by_key(read_rows(version_dir(lake, 1), "chunks"), "chunk_id")
    assert np.allclose(np.asarray(rows["c1"]["embedding"], dtype=np.float32), lake.vectors["c1"])
    assert manifest.chunks_rows == len(rows)


def test_a_chunk_whose_only_embedding_is_another_model_counts_as_missing(lake: Lake):
    drop_embedding(lake, "c1")
    _append_embedding(lake, "c1", model="some/other-model")

    with pytest.raises(ExportError, match="no embedding"):
        run_export(lake)


@pytest.mark.parametrize(
    "root",
    ["", "   ", "s3://", "s3://bucket", "s3://bucket/", "s3a://bucket/x", "s3n://bucket/x",
     "file:///tmp/x", "relative/path", "/"],
)  # fmt: skip
def test_unsafe_export_roots_are_refused_before_anything_is_written(lake: Lake, root: str):
    with pytest.raises(ValueError):
        run_export(lake, export_root=root)

    assert no_export_dirs(lake.root)
    assert version_rows(lake.spark, lake.ns) == []


def test_a_trailing_slash_on_the_export_root_is_normalised(lake: Lake):
    run_export(lake, export_root=str(lake.root) + "/")

    assert (version_dir(lake, 1) / MANIFEST_NAME).is_file()


def test_a_reexport_that_dies_after_the_first_table_leaves_no_manifest(
    lake: Lake, monkeypatch: pytest.MonkeyPatch
):
    """Fault injection: the second table's write fails. The old manifest must be gone, so the
    half-overwritten version is visibly incomplete and the loader cannot pick it up."""
    from pipelines.spark import export_serving as module

    run_export(lake)
    vdir = version_dir(lake, 1)
    assert (vdir / MANIFEST_NAME).is_file()
    real = module._write_table
    calls: list[str] = []

    def flaky(spark: Any, df: Any, name: str, out: str) -> Any:
        calls.append(name)
        if len(calls) == 2:
            raise RuntimeError("simulated S3 failure")
        return real(spark, df, name, out)

    monkeypatch.setattr(module, "_write_table", flaky)

    with pytest.raises(RuntimeError, match="simulated"):
        run_export(lake, version=1)

    assert not (vdir / MANIFEST_NAME).exists()
