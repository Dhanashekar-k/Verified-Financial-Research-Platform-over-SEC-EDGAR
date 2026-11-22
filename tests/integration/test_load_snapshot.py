"""Tests for ``serving/load/load_snapshot.py`` (test-first).

The loader is driven against a REAL Postgres 17 + pgvector in a Testcontainer (hard rule 4: the
database is never mocked). One container per module; every test gets its own fresh database, so
tests cannot see each other's tables. The export is a tiny real Parquet export written by
``tests/support/serving_export.py``.

Design decisions the tests pin (also listed in the hand-off report):

- ``swap(dsn, N)`` when N is already live raises ``LoadError`` and changes nothing (a swap that
  went through would set ``previous_version = live_version = N`` and destroy the rollback target).
- Preflight failures (tampered manifest or Parquet, bad JSON, wrong dim, wrong embedding length)
  surface as ``LoadError``, never a raw ``pydantic`` / ``pyarrow`` / ``TypeError``. Failures inside
  the COPY (constraint violations) may surface as ``LoadError`` or ``psycopg.Error``; either way a
  failed load leaves no ``*_vN`` relation and does not disturb the live version.
- ``LoadReport.validation`` has the integer keys ``fact_lookups_ok`` and ``text_searches_ok``
  (checks passed). ``LoadReport.rows`` is keyed ``facts`` / ``chunks`` / ``companies``;
  ``sizes_bytes`` has ``<table>_v<N>`` (``pg_total_relation_size``) and one key containing
  ``hnsw`` and one containing ``gin`` for the chunk indexes (``pg_relation_size``).
- After a swap the retention drop may be blocked by a reader; the swap is already committed then,
  and ``swap`` must neither hang nor undo the switch.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import numpy as np
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pipelines.common.manifest import Manifest
from serving.load.load_snapshot import (
    LoadError,
    LoadReport,
    load_version,
    main,
    rollback,
    state,
    swap,
)
from tests.support.serving_export import (
    NASTY_ALIASES,
    NASTY_TEXT,
    embedding_matrix,
    export_dir,
    read_table,
    replace_file,
    rewrite_manifest,
    write_export,
)

IMAGE = "pgvector/pgvector:pg17"
TABLES = ("facts", "chunks", "companies")
_VERSIONED = re.compile(r"^(facts|chunks|companies)_v(\d+)$")


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    """One pgvector/pg17 container per module (superuser: the loader runs CREATE EXTENSION)."""
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # older testcontainers
        from testcontainers.postgres import PostgresContainer  # type: ignore[no-redef]
    with PostgresContainer(IMAGE, driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture
def dsn(pg_url: str) -> Iterator[str]:
    """A brand-new EMPTY database (no extensions, no tables) in the module container."""
    name = f"t_{uuid4().hex[:12]}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    yield urlsplit(pg_url)._replace(path=f"/{name}").geturl()
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# --------------------------------------------------------------------------- helpers


def rows(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


def scalar(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> Any:
    return rows(dsn, sql, params)[0][0]


def versioned_tables(dsn: str) -> set[str]:
    found = rows(dsn, "SELECT relname FROM pg_class WHERE relkind IN ('r','p')")
    return {r[0] for r in found if _VERSIONED.match(r[0])}


def expected_tables(*versions: int) -> set[str]:
    return {f"{t}_v{v}" for v in versions for t in TABLES}


def leftovers(dsn: str, version: int) -> list[str]:
    """Every relation (table, index, sequence, ...) whose name belongs to version N."""
    pattern = rf"^(facts|chunks|companies)_v{version}(_.*)?$"
    return [r[0] for r in rows(dsn, "SELECT relname FROM pg_class WHERE relname ~ %s", (pattern,))]


def corpus_state_rows(dsn: str) -> list[tuple[Any, ...]]:
    if scalar(dsn, "SELECT to_regclass('corpus_state')") is None:
        return []
    return rows(dsn, "SELECT id, live_version, previous_version, swapped_at FROM corpus_state")


def viewdef(dsn: str, view: str) -> str | None:
    if scalar(dsn, "SELECT to_regclass(%s)", (view,)) is None:
        return None
    return str(scalar(dsn, "SELECT pg_get_viewdef(%s::regclass)", (view,)))


def fingerprint(dsn: str) -> dict[str, Any]:
    """Everything a swap/rollback may change, for 'changes NOTHING' assertions."""
    has_companies = scalar(dsn, "SELECT to_regclass('companies')") is not None
    return {
        "facts_view": viewdef(dsn, "facts"),
        "chunks_view": viewdef(dsn, "chunks"),
        "corpus_state": corpus_state_rows(dsn),
        "companies": rows(dsn, "SELECT * FROM companies ORDER BY cik") if has_companies else None,
        "state": state(dsn),
        "tables": versioned_tables(dsn),
    }


def assert_serving(dsn: str, manifest: Manifest) -> None:
    """The unversioned views/table serve exactly `manifest`'s version."""
    v = manifest.corpus_version
    assert scalar(dsn, "SELECT count(*) FROM facts") == manifest.facts_rows
    assert scalar(dsn, "SELECT count(*) FROM chunks") == manifest.chunks_rows
    assert scalar(dsn, "SELECT count(*) FROM companies") == manifest.companies_rows
    assert re.search(rf"\bfacts_v{v}\b", viewdef(dsn, "facts") or "")
    assert re.search(rf"\bchunks_v{v}\b", viewdef(dsn, "chunks") or "")
    names = [r[0] for r in rows(dsn, "SELECT name FROM companies")]
    assert names and all(n.endswith(f" v{v}") for n in names), names


def assert_corpus_state(dsn: str, live: int | None, previous: int | None) -> None:
    got = corpus_state_rows(dsn)
    if live is None:
        assert got == []
        assert state(dsn) == (None, None)
        return
    assert len(got) == 1
    row_id, live_v, prev_v, swapped_at = got[0]
    assert (row_id, live_v, prev_v) == (1, live, previous)
    assert swapped_at is not None
    assert abs((datetime.now(UTC) - swapped_at).total_seconds()) < 300
    assert state(dsn) == (live, previous)


def load(dsn: str, root: Path, version: int, **kw: Any) -> LoadReport:
    return load_version(dsn, version=version, source=str(root), **kw)


def index_defs(dsn: str, table: str) -> list[str]:
    """Non-primary-key index definitions of `table`, whitespace-normalised, after 'USING '."""
    found = rows(
        dsn,
        "SELECT i.indexdef FROM pg_indexes i JOIN pg_class c ON c.relname = i.indexname "
        "JOIN pg_index x ON x.indexrelid = c.oid "
        "WHERE i.tablename = %s AND NOT x.indisprimary",
        (table,),
    )
    return [" ".join(r[0].split()).split(" USING ", 1)[1] for r in found]


# --------------------------------------------------------------------------- API shape


def test_load_error_is_an_exception_and_report_is_frozen() -> None:
    """The documented API: LoadError is an Exception; LoadReport is a frozen dataclass."""
    assert issubclass(LoadError, Exception)
    assert dataclasses.is_dataclass(LoadReport)
    names = {f.name for f in dataclasses.fields(LoadReport)}
    assert names == {"version", "rows", "seconds", "sizes_bytes", "validation"}
    report = LoadReport(version=1, rows={}, seconds={}, sizes_bytes={}, validation={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.version = 2  # type: ignore[misc]


def test_state_is_none_none_on_an_empty_database(dsn: str) -> None:
    """state() before anything was ever loaded or swapped is (None, None), not an error.

    The database has no corpus_state table at all; 'before the first swap' must still answer.
    """
    assert state(dsn) == (None, None)


# --------------------------------------------------------------------------- load: structure


def test_load_creates_versioned_tables_with_manifest_row_counts_and_does_not_swap(
    dsn: str, tmp_path: Path
) -> None:
    """load_version creates facts_v1/chunks_v1/companies_v1 with the manifest's counts and report.

    It must NOT swap: no `facts`/`chunks` views, no live version. It also creates the extensions
    itself (the database started empty).
    """
    m = write_export(tmp_path, 1)
    report = load(dsn, tmp_path, 1)

    assert versioned_tables(dsn) == expected_tables(1)
    assert scalar(dsn, "SELECT count(*) FROM facts_v1") == m.facts_rows == 40
    assert scalar(dsn, "SELECT count(*) FROM chunks_v1") == m.chunks_rows == 30
    assert scalar(dsn, "SELECT count(*) FROM companies_v1") == m.companies_rows == 5
    assert report.version == 1
    assert report.rows == {"facts": 40, "chunks": 30, "companies": 5}

    assert viewdef(dsn, "facts") is None
    assert viewdef(dsn, "chunks") is None
    assert state(dsn) == (None, None)
    exts = {r[0] for r in rows(dsn, "SELECT extname FROM pg_extension")}
    assert {"vector", "pg_trgm"} <= exts


def test_load_creates_contract_ddl_column_types(dsn: str, tmp_path: Path) -> None:
    """The DDL is the contract's: halfvec(384) NOT NULL, generated STORED tsvector, PKs, types.

    Why: the retrieval primitives and the verifier depend on these exact types (smallint years,
    numeric values, bigint[] derived_from).
    """
    write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)

    def col(table: str, column: str) -> tuple[Any, ...]:
        return rows(
            dsn,
            "SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, a.attgenerated "
            "FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attname = %s",
            (table, column),
        )[0]

    assert col("chunks_v1", "embedding")[:2] == ("halfvec(384)", True)
    assert col("chunks_v1", "tsv")[0] == "tsvector"
    assert col("chunks_v1", "tsv")[2] == "s"  # GENERATED ALWAYS ... STORED
    assert col("chunks_v1", "text")[1] is True
    assert col("chunks_v1", "fiscal_year")[0] == "smallint"
    assert col("facts_v1", "value")[:2] == ("numeric", True)
    assert col("facts_v1", "qtrs")[0] == "smallint"
    assert col("facts_v1", "fiscal_year")[0] == "smallint"
    assert col("facts_v1", "derived_from")[0] == "bigint[]"
    assert col("facts_v1", "is_restated")[1] is True
    assert col("companies_v1", "fye_mmdd")[0] == "character(4)"
    assert col("companies_v1", "aliases")[0] == "text[]"

    def pk(table: str) -> list[str]:
        return [
            r[0]
            for r in rows(
                dsn,
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
                "ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = %s::regclass AND i.indisprimary",
                (table,),
            )
        ]

    assert pk("facts_v1") == ["fact_id"]
    assert pk("chunks_v1") == ["chunk_id"]
    assert pk("companies_v1") == ["cik"]


def test_loaded_data_equals_the_parquet_export(dsn: str, tmp_path: Path) -> None:
    """Every column of every row survives the COPY, including nulls, decimals and arrays.

    Why: a COPY text-format loader is easy to get subtly wrong (NULL vs empty string, array
    quoting, decimal scale, dates). The export is the oracle.
    """
    write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)

    facts = read_table(tmp_path, 1, "facts").sort_by("fact_id")
    fcols = [n for n in facts.column_names if n != "anomaly_score"]
    got = rows(
        dsn,
        f"SELECT {', '.join(fcols)}, anomaly_score::double precision "
        "FROM facts_v1 ORDER BY fact_id",
    )
    want = [tuple(r[c] for c in fcols) + (r["anomaly_score"],) for r in facts.to_pylist()]
    assert got == want

    companies = read_table(tmp_path, 1, "companies").sort_by("cik")
    ccols = companies.column_names
    got_c = rows(dsn, f"SELECT {', '.join(ccols)} FROM companies_v1 ORDER BY cik")
    assert got_c == [tuple(r[c] for c in ccols) for r in companies.to_pylist()]

    chunks = read_table(tmp_path, 1, "chunks").sort_by("chunk_id")
    kcols = [n for n in chunks.column_names if n != "embedding"]
    got_k = rows(dsn, f"SELECT {', '.join(kcols)} FROM chunks_v1 ORDER BY chunk_id")
    assert got_k == [tuple(r[c] for c in kcols) for r in chunks.to_pylist()]
    assert scalar(dsn, "SELECT count(*) FROM chunks_v1 WHERE tenant_id IS NOT NULL") == 0


def test_nasty_text_name_and_aliases_survive_byte_for_byte(dsn: str, tmp_path: Path) -> None:
    """Tabs, newlines, CRLF, backslashes, the COPY end marker, quotes and non-ASCII are preserved.

    Also a company name with quotes/tab and an aliases array with commas, quotes, braces,
    backslash, the word NULL and an empty string (classic text[] and COPY escaping traps).
    """
    write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)

    text = scalar(dsn, "SELECT text FROM chunks_v1 WHERE chunk_id = 'chk-000000'")
    assert text.startswith(NASTY_TEXT)
    assert text.encode("utf-8").startswith(NASTY_TEXT.encode("utf-8"))
    assert (
        "\\." in text and "\\N" in text and "\r\n" in text and "\t" in text and "\U0001f600" in text
    )
    assert scalar(dsn, "SELECT length(text) FROM chunks_v1 WHERE chunk_id = 'chk-000000'") == len(
        read_table(tmp_path, 1, "chunks").column("text")[0].as_py()
    )

    aliases = scalar(dsn, "SELECT aliases FROM companies_v1 WHERE cik = 1000")
    assert aliases == NASTY_ALIASES
    name = scalar(dsn, "SELECT name FROM companies_v1 WHERE cik = 1000")
    assert name.startswith('O\'Brien "Sons" & Co\twith tab')
    assert scalar(dsn, "SELECT count(*) FROM companies_v1 WHERE aliases = '{}'") >= 1


def test_embeddings_are_halfvec_of_the_exported_vectors(dsn: str, tmp_path: Path) -> None:
    """The stored halfvec equals the exported float32 vector rounded to half precision, in order."""
    write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)

    table = read_table(tmp_path, 1, "chunks")
    order = np.argsort(np.array(table["chunk_id"].to_pylist()))
    want = embedding_matrix(table)[order].astype(np.float16).astype(np.float64)
    got_rows = rows(dsn, "SELECT embedding::text FROM chunks_v1 ORDER BY chunk_id")
    got = np.array([json.loads(r[0]) for r in got_rows], dtype=np.float64)
    assert got.shape == want.shape == (30, 384)
    np.testing.assert_allclose(got, want, rtol=1e-3, atol=1e-6)
    assert scalar(dsn, "SELECT count(*) FROM chunks_v1 WHERE vector_dims(embedding) <> 384") == 0


def test_tsv_is_generated_from_text(dsn: str, tmp_path: Path) -> None:
    """The generated tsvector is populated: a rare word matches exactly its own chunk."""
    write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)
    hits = rows(
        dsn,
        "SELECT chunk_id FROM chunks_v1 WHERE tsv @@ websearch_to_tsquery('english', 'zqaaex')",
    )
    assert hits == [("chk-000004",)]


# --------------------------------------------------------------------------- load: indexes


def test_indexes_are_built_after_load_with_contract_parameters(dsn: str, tmp_path: Path) -> None:
    """Facts B-trees, chunks GIN + HNSW (m=32, ef_construction=128, cosine ops), btree(cik, fy).

    The partial index is on exactly (cik, tag, fiscal_year, fiscal_period, uom) WHERE is_original.
    """
    write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)

    facts = index_defs(dsn, "facts_v1")
    assert "btree (cik, tag, fiscal_year, fiscal_period, uom) WHERE is_original" in facts
    assert "btree (fact_key)" in facts
    assert "btree (tag, fiscal_year, fiscal_period)" in facts
    assert len(facts) == 3

    chunks = index_defs(dsn, "chunks_v1")
    assert "gin (tsv)" in chunks
    assert "btree (cik, fiscal_year)" in chunks
    hnsw = [d for d in chunks if d.startswith("hnsw ")]
    assert len(hnsw) == 1
    assert "(embedding halfvec_cosine_ops)" in hnsw[0]
    opts = rows(
        dsn,
        "SELECT c.reloptions FROM pg_class c JOIN pg_index x ON x.indexrelid = c.oid "
        "JOIN pg_am am ON am.oid = c.relam WHERE x.indrelid = 'chunks_v1'::regclass "
        "AND am.amname = 'hnsw'",
    )[0][0]
    assert sorted(opts) == ["ef_construction=128", "m=32"]
    assert len(chunks) == 3


def test_hnsw_parameters_are_passed_through(dsn: str, tmp_path: Path) -> None:
    """hnsw_m and hnsw_ef_construction reach CREATE INDEX (so the bench can vary them)."""
    write_export(tmp_path, 1)
    load(dsn, tmp_path, 1, hnsw_m=8, hnsw_ef_construction=32)
    opts = rows(
        dsn,
        "SELECT c.reloptions FROM pg_class c JOIN pg_index x ON x.indexrelid = c.oid "
        "JOIN pg_am am ON am.oid = c.relam WHERE x.indrelid = 'chunks_v1'::regclass "
        "AND am.amname = 'hnsw'",
    )[0][0]
    assert sorted(opts) == ["ef_construction=32", "m=8"]


def test_build_indexes_false_creates_only_primary_keys(dsn: str, tmp_path: Path) -> None:
    """build_indexes=False builds none of the secondary/GIN/HNSW indexes (data still loads)."""
    m = write_export(tmp_path, 1)
    report = load(dsn, tmp_path, 1, build_indexes=False)

    for table in ("facts_v1", "chunks_v1", "companies_v1"):
        assert index_defs(dsn, table) == []
    assert scalar(dsn, "SELECT count(*) FROM chunks_v1") == m.chunks_rows
    assert not any(k.startswith("index_") for k in report.seconds)
    assert not any("hnsw" in k or "gin" in k for k in report.sizes_bytes)


# --------------------------------------------------------------------------- load: report


def test_report_has_timings_sizes_and_validation_counts(dsn: str, tmp_path: Path) -> None:
    """LoadReport carries COPY seconds, index build seconds, sizes and validation counts."""
    write_export(tmp_path, 1)
    report = load(dsn, tmp_path, 1)

    for key in (
        "copy_facts",
        "copy_chunks",
        "copy_companies",
        "index_chunks_hnsw",
        "index_chunks_gin",
    ):
        assert key in report.seconds, report.seconds
        assert isinstance(report.seconds[key], float) and report.seconds[key] >= 0.0

    for table in TABLES:
        assert report.sizes_bytes[f"{table}_v1"] > 0
    hnsw_keys = [k for k in report.sizes_bytes if "hnsw" in k]
    gin_keys = [k for k in report.sizes_bytes if "gin" in k]
    assert len(hnsw_keys) == 1 and len(gin_keys) == 1
    actual_hnsw = scalar(
        dsn,
        "SELECT pg_relation_size(c.oid) FROM pg_class c JOIN pg_index x ON x.indexrelid = c.oid "
        "JOIN pg_am am ON am.oid = c.relam WHERE x.indrelid = 'chunks_v1'::regclass "
        "AND am.amname = 'hnsw'",
    )
    assert report.sizes_bytes[hnsw_keys[0]] == actual_hnsw > 0
    assert all(isinstance(v, int) for v in report.sizes_bytes.values())

    assert report.validation["fact_lookups_ok"] == 20
    assert report.validation["text_searches_ok"] == 20


@pytest.mark.parametrize("sample_checks", [0, 5, 20])
def test_sample_checks_controls_the_number_of_validation_probes(
    dsn: str, tmp_path: Path, sample_checks: int
) -> None:
    """sample_checks=N runs N fact lookups and N text searches and reports N passed."""
    write_export(tmp_path, 1)
    report = load(dsn, tmp_path, 1, sample_checks=sample_checks)
    assert report.validation["fact_lookups_ok"] == sample_checks
    assert report.validation["text_searches_ok"] == sample_checks


def test_stopword_only_chunk_does_not_fail_text_validation(dsn: str, tmp_path: Path) -> None:
    """A chunk whose tsvector is empty (only stop words) must not make validation fail.

    Why: real filings contain such chunks. Sample words must come from the chunk's own lexemes
    (or such chunks must be skipped), not from a naive split of the raw text. All chunks are
    sampled here (sample_checks > n_chunks).
    """
    write_export(tmp_path, 1, n_chunks=12, stopword_chunk=True)
    report = load(dsn, tmp_path, 1, sample_checks=20)
    assert report.rows["chunks"] == 12
    assert report.validation["text_searches_ok"] >= 1


def test_non_original_facts_do_not_fail_fact_lookups(dsn: str, tmp_path: Path) -> None:
    """The export has non-original facts (is_original = false); lookups on them still return a row.

    Why: the partial index only covers is_original rows, but validation samples the whole table
    and the lookup must be by (cik, tag, fiscal_year, fiscal_period, uom) without assuming it.
    """
    write_export(tmp_path, 1, n_facts=40)
    report = load(dsn, tmp_path, 1, sample_checks=40)
    assert scalar(dsn, "SELECT count(*) FROM facts_v1 WHERE NOT is_original") > 0
    assert report.validation["fact_lookups_ok"] >= 20


# --------------------------------------------------------------------------- load: idempotent


def test_reloading_the_same_version_replaces_rather_than_appends(dsn: str, tmp_path: Path) -> None:
    """A second load of the same (non-live) version leaves the same counts and the same indexes."""
    m = write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)
    load(dsn, tmp_path, 1)

    assert versioned_tables(dsn) == expected_tables(1)
    assert scalar(dsn, "SELECT count(*) FROM facts_v1") == m.facts_rows
    assert scalar(dsn, "SELECT count(*) FROM chunks_v1") == m.chunks_rows
    assert scalar(dsn, "SELECT count(*) FROM companies_v1") == m.companies_rows
    assert len(index_defs(dsn, "chunks_v1")) == 3


def test_reload_picks_up_changed_export_content(dsn: str, tmp_path: Path) -> None:
    """Reloading version 1 from a CHANGED export yields the new counts (drop + recreate)."""
    write_export(tmp_path, 1, n_chunks=30)
    load(dsn, tmp_path, 1)
    m_new = write_export(tmp_path, 1, n_chunks=22, n_facts=33)
    load(dsn, tmp_path, 1)
    assert scalar(dsn, "SELECT count(*) FROM chunks_v1") == m_new.chunks_rows == 22
    assert scalar(dsn, "SELECT count(*) FROM facts_v1") == m_new.facts_rows == 33


def test_loading_the_live_version_is_refused_and_changes_nothing(dsn: str, tmp_path: Path) -> None:
    """Loading the LIVE version raises LoadError; the serving tables and views are untouched."""
    m = write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)
    swap(dsn, 1)
    before = fingerprint(dsn)

    with pytest.raises(LoadError):
        load(dsn, tmp_path, 1)

    assert fingerprint(dsn) == before
    assert_serving(dsn, m)


# --------------------------------------------------------------------------- load: preflight


def _tamper_delete_chunk_file(root: Path, m: Manifest) -> None:
    (export_dir(root, 1) / m.files["chunks"][1].path).unlink()


def _tamper_footer_count_consistent(root: Path, m: Manifest) -> None:
    def edit(d: dict[str, Any]) -> None:
        d["files"]["chunks"][0]["rows"] += 1
        d["chunks_rows"] += 1

    rewrite_manifest(root, 1, edit)


def _tamper_facts_count_consistent(root: Path, m: Manifest) -> None:
    def edit(d: dict[str, Any]) -> None:
        d["files"]["facts"][0]["rows"] -= 1
        d["facts_rows"] -= 1

    rewrite_manifest(root, 1, edit)


def _tamper_headline_only(root: Path, m: Manifest) -> None:
    rewrite_manifest(root, 1, lambda d: d.__setitem__("chunks_rows", d["chunks_rows"] + 1))


def _tamper_corrupt_parquet(root: Path, m: Manifest) -> None:
    (export_dir(root, 1) / m.files["facts"][0].path).write_bytes(b"this is not parquet")


def _tamper_missing_manifest(root: Path, m: Manifest) -> None:
    (export_dir(root, 1) / "_MANIFEST.json").unlink()


def _tamper_manifest_not_json(root: Path, m: Manifest) -> None:
    (export_dir(root, 1) / "_MANIFEST.json").write_text("{not json", encoding="utf-8")


def _tamper_version_mismatch(root: Path, m: Manifest) -> None:
    rewrite_manifest(root, 1, lambda d: d.__setitem__("corpus_version", 2))


def _tamper_manifest_dim(root: Path, m: Manifest) -> None:
    rewrite_manifest(root, 1, lambda d: d.__setitem__("dim", 768))


@pytest.mark.parametrize(
    "tamper",
    [
        _tamper_delete_chunk_file,
        _tamper_footer_count_consistent,
        _tamper_facts_count_consistent,
        _tamper_headline_only,
        _tamper_corrupt_parquet,
        _tamper_missing_manifest,
        _tamper_manifest_not_json,
        _tamper_version_mismatch,
        _tamper_manifest_dim,
    ],
    ids=lambda f: f.__name__.removeprefix("_tamper_"),
)
def test_preflight_rejects_a_tampered_export_and_creates_no_tables(
    dsn: str, tmp_path: Path, tamper: Callable[[Path, Manifest], None]
) -> None:
    """A tampered/inconsistent export raises LoadError BEFORE any `*_v1` relation is created.

    Covers: a deleted Parquet file, a footer row count that differs from the manifest (kept
    internally consistent so the manifest itself parses), a headline count that disagrees with
    its files, corrupt Parquet, a missing or unparseable manifest, a manifest whose corpus_version
    is not the requested one, and a manifest dim other than 384.
    """
    m = write_export(tmp_path, 1)
    tamper(tmp_path, m)

    with pytest.raises(LoadError):
        load(dsn, tmp_path, 1)

    assert versioned_tables(dsn) == set()
    assert leftovers(dsn, 1) == []
    assert state(dsn) == (None, None)


def test_preflight_rejects_a_manifest_with_another_dim(dsn: str, tmp_path: Path) -> None:
    """A genuine dim-16 export (manifest.dim == 16, vectors of length 16) is refused."""
    write_export(tmp_path, 1, dim=16)
    with pytest.raises(LoadError):
        load(dsn, tmp_path, 1)
    assert leftovers(dsn, 1) == []


def test_embedding_column_of_another_length_is_refused_and_leaves_nothing(
    dsn: str, tmp_path: Path
) -> None:
    """Manifest claims 384 but the vectors have length 16: LoadError, and no `*_v1` table remains.

    Nothing in the Parquet schema (a variable-length list) reveals this; the loader must catch it
    from the data (or from the halfvec(384) column rejecting it) and clean up.
    """
    write_export(tmp_path, 1, dim=16)
    rewrite_manifest(tmp_path, 1, lambda d: d.__setitem__("dim", 384))

    with pytest.raises(LoadError):
        load(dsn, tmp_path, 1)

    assert leftovers(dsn, 1) == []
    assert state(dsn) == (None, None)


def test_a_missing_source_directory_is_a_load_error(dsn: str, tmp_path: Path) -> None:
    """A source that does not contain corpus_version=N/ raises LoadError and creates nothing."""
    write_export(tmp_path, 2)  # only version 2 exists
    with pytest.raises(LoadError):
        load(dsn, tmp_path, 1)
    with pytest.raises(LoadError):
        load(dsn, tmp_path / "does-not-exist", 1)
    assert versioned_tables(dsn) == set()


def _dup_chunk_id(t: pa.Table) -> pa.Table:
    ids = t["chunk_id"].to_pylist()
    ids[1] = ids[0]
    return t.set_column(
        t.schema.get_field_index("chunk_id"), "chunk_id", pa.array(ids, pa.string())
    )


def _null_embedding(t: pa.Table) -> pa.Table:
    emb = t["embedding"].to_pylist()
    emb[2] = None
    idx = t.schema.get_field_index("embedding")
    return t.set_column(idx, t.schema.field(idx), pa.array(emb, t.schema.field(idx).type))


def _dup_fact_id(t: pa.Table) -> pa.Table:
    ids = t["fact_id"].to_pylist()
    ids[1] = ids[0]
    return t.set_column(t.schema.get_field_index("fact_id"), "fact_id", pa.array(ids, pa.int64()))


def _null_fact_tag(t: pa.Table) -> pa.Table:
    tags = t["tag"].to_pylist()
    tags[0] = None
    return t.set_column(t.schema.get_field_index("tag"), "tag", pa.array(tags, pa.string()))


@pytest.mark.parametrize(
    ("rel_path", "corrupt"),
    [
        ("chunks/part-00000.parquet", _dup_chunk_id),
        ("chunks/part-00000.parquet", _null_embedding),
        ("facts/part-00000.parquet", _dup_fact_id),
        ("facts/part-00000.parquet", _null_fact_tag),
    ],
    ids=["dup_chunk_id", "null_embedding", "dup_fact_id", "null_fact_tag"],
)
def test_failed_load_drops_its_tables_and_leaves_the_live_version_serving(
    dsn: str, tmp_path: Path, rel_path: str, corrupt: Callable[[pa.Table], pa.Table]
) -> None:
    """Bad data (footers match the manifest) fails the load: no `*_v2` relation is left behind.

    The error is a LoadError or a psycopg.Error (a constraint violation inside COPY), but never an
    unrelated exception type. Version 1 is live; its tables, views, companies and corpus_state
    must be exactly as before.
    """
    m1 = write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)
    swap(dsn, 1)
    write_export(tmp_path, 2, n_chunks=36, n_facts=44)
    replace_file(tmp_path, 2, rel_path, corrupt(pq.read_table(export_dir(tmp_path, 2) / rel_path)))
    before = fingerprint(dsn)

    with pytest.raises((LoadError, psycopg.Error)):
        load(dsn, tmp_path, 2)

    assert leftovers(dsn, 2) == []
    assert fingerprint(dsn) == before
    assert_serving(dsn, m1)


# --------------------------------------------------------------------------- swap


def test_swap_switches_views_companies_and_state_in_one_step(dsn: str, tmp_path: Path) -> None:
    """swap(1): views read v1, companies equals companies_v1, state is (live=1, previous=NULL)."""
    m1 = write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)
    swap(dsn, 1)

    assert_serving(dsn, m1)
    assert_corpus_state(dsn, live=1, previous=None)
    assert rows(dsn, "SELECT * FROM companies ORDER BY cik") == rows(
        dsn, "SELECT * FROM companies_v1 ORDER BY cik"
    )
    assert rows(dsn, "SELECT chunk_id FROM chunks ORDER BY chunk_id") == rows(
        dsn, "SELECT chunk_id FROM chunks_v1 ORDER BY chunk_id"
    )
    assert versioned_tables(dsn) == expected_tables(1)


def test_corpus_state_is_a_single_row_table(dsn: str, tmp_path: Path) -> None:
    """corpus_state allows only id = 1 (CHECK) and keeps exactly one row across swaps."""
    write_export(tmp_path, 1)
    write_export(tmp_path, 2, n_chunks=36, n_facts=44, n_companies=6)
    load(dsn, tmp_path, 1)
    swap(dsn, 1)
    load(dsn, tmp_path, 2)
    swap(dsn, 2)

    assert len(corpus_state_rows(dsn)) == 1
    with (
        psycopg.connect(dsn, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute("INSERT INTO corpus_state (id, live_version) VALUES (2, 9)")


def test_swap_to_missing_version_raises_and_changes_nothing(dsn: str, tmp_path: Path) -> None:
    """swap to a never-loaded version: LoadError; views, corpus_state, companies unchanged."""
    m1 = write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)
    swap(dsn, 1)
    before = fingerprint(dsn)

    with pytest.raises(LoadError):
        swap(dsn, 99)

    assert fingerprint(dsn) == before
    assert_serving(dsn, m1)


def test_swap_to_missing_version_before_any_swap_creates_nothing(dsn: str, tmp_path: Path) -> None:
    """On a database with a loaded-but-never-swapped v1, swap(7) fails and nothing becomes live."""
    write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)
    with pytest.raises(LoadError):
        swap(dsn, 7)
    assert viewdef(dsn, "facts") is None
    assert viewdef(dsn, "chunks") is None
    assert state(dsn) == (None, None)


def test_swap_with_a_partially_missing_version_changes_nothing(dsn: str, tmp_path: Path) -> None:
    """If any of the target's three tables is missing, swap raises LoadError, changing nothing.

    Why: a half-dropped version must never become live; companies in particular must not be
    emptied by the failed swap.
    """
    m1 = write_export(tmp_path, 1)
    write_export(tmp_path, 2, n_chunks=36, n_facts=44, n_companies=6)
    load(dsn, tmp_path, 1)
    swap(dsn, 1)
    load(dsn, tmp_path, 2)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE companies_v2")
    before = fingerprint(dsn)

    with pytest.raises(LoadError):
        swap(dsn, 2)

    assert fingerprint(dsn) == before
    assert_serving(dsn, m1)


def test_swapping_the_live_version_again_is_refused_and_changes_nothing(
    dsn: str, tmp_path: Path
) -> None:
    """DECISION: swap(N) when N is live raises LoadError and changes nothing.

    Why: a swap that went through would set previous_version = live_version = N and destroy the
    rollback target. swapped_at must not move either.
    """
    m1 = write_export(tmp_path, 1)
    m2 = write_export(tmp_path, 2, n_chunks=36, n_facts=44, n_companies=6)
    load(dsn, tmp_path, 1)
    swap(dsn, 1)
    load(dsn, tmp_path, 2)
    swap(dsn, 2)
    before = fingerprint(dsn)

    with pytest.raises(LoadError):
        swap(dsn, 2)

    assert fingerprint(dsn) == before
    assert_corpus_state(dsn, live=2, previous=1)
    assert_serving(dsn, m2)
    assert m1.chunks_rows != m2.chunks_rows


def test_swap_drops_every_version_that_is_neither_live_nor_previous(
    dsn: str, tmp_path: Path
) -> None:
    """After swap(3) only versions 3 (live) and 1 (previous) remain; an unswapped v2 is dropped too.

    The retention rule is 'not in (live, previous)', which also removes versions that were loaded
    but never became live.
    """
    write_export(tmp_path, 1)
    write_export(tmp_path, 2, n_chunks=36, n_facts=44, n_companies=6)
    m3 = write_export(tmp_path, 3, n_chunks=25, n_facts=35, n_companies=4)
    load(dsn, tmp_path, 1)
    swap(dsn, 1)
    load(dsn, tmp_path, 2)
    load(dsn, tmp_path, 3)
    assert versioned_tables(dsn) == expected_tables(1, 2, 3)

    swap(dsn, 3)

    assert versioned_tables(dsn) == expected_tables(1, 3)
    assert leftovers(dsn, 2) == []  # indexes went with the tables
    assert_corpus_state(dsn, live=3, previous=1)
    assert_serving(dsn, m3)


def test_swap_does_not_hang_when_a_reader_blocks_the_retention_drop(
    dsn: str, tmp_path: Path
) -> None:
    """A long reader on an old version must not hang swap; the switch itself is already committed.

    Version 1 would be dropped by swap(3). A second connection holds an open transaction that read
    chunks_v1. The drop must give up (lock_timeout) instead of waiting forever. Either returning
    normally or raising LoadError is acceptable, but live=3, previous=2 must be in force.
    """
    write_export(tmp_path, 1)
    write_export(tmp_path, 2, n_chunks=36, n_facts=44, n_companies=6)
    m3 = write_export(tmp_path, 3, n_chunks=25, n_facts=35, n_companies=4)
    for v in (1, 2):
        load(dsn, tmp_path, v)
        swap(dsn, v)
    load(dsn, tmp_path, 3)

    blocker = psycopg.connect(dsn)  # not autocommit: the SELECT keeps its lock until rollback
    try:
        blocker.execute("SELECT count(*) FROM chunks_v1").fetchone()
        started = time.monotonic()
        try:
            swap(dsn, 3)
        except LoadError:
            pass
        elapsed = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()

    assert elapsed < 60
    assert_corpus_state(dsn, live=3, previous=2)
    assert_serving(dsn, m3)


# --------------------------------------------------------------------------- rollback


def test_rollback_without_a_previous_version_raises_and_changes_nothing(
    dsn: str, tmp_path: Path
) -> None:
    """rollback() with no state, and with live=1/previous=NULL, raises LoadError, no change."""
    with pytest.raises(LoadError):
        rollback(dsn)
    assert state(dsn) == (None, None)

    m1 = write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)
    swap(dsn, 1)
    before = fingerprint(dsn)

    with pytest.raises(LoadError):
        rollback(dsn)

    assert fingerprint(dsn) == before
    assert_serving(dsn, m1)


def test_rollback_twice_returns_to_the_newer_version(dsn: str, tmp_path: Path) -> None:
    """The pair is exchanged each time: rollback() -> 1 (state 1/2), rollback() -> 2 (state 2/1)."""
    m1 = write_export(tmp_path, 1)
    m2 = write_export(tmp_path, 2, n_chunks=36, n_facts=44, n_companies=6)
    load(dsn, tmp_path, 1)
    swap(dsn, 1)
    load(dsn, tmp_path, 2)
    swap(dsn, 2)

    assert rollback(dsn) == 1
    assert_corpus_state(dsn, live=1, previous=2)
    assert_serving(dsn, m1)

    assert rollback(dsn) == 2
    assert_corpus_state(dsn, live=2, previous=1)
    assert_serving(dsn, m2)
    assert versioned_tables(dsn) == expected_tables(1, 2)


def test_rollback_to_a_dropped_previous_version_raises_and_changes_nothing(
    dsn: str, tmp_path: Path
) -> None:
    """If the previous version's tables are gone, rollback raises LoadError and nothing changes."""
    m2 = write_export(tmp_path, 2, n_chunks=36, n_facts=44, n_companies=6)
    write_export(tmp_path, 1)
    load(dsn, tmp_path, 1)
    swap(dsn, 1)
    load(dsn, tmp_path, 2)
    swap(dsn, 2)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE chunks_v1")
    before = fingerprint(dsn)

    with pytest.raises(LoadError):
        rollback(dsn)

    assert fingerprint(dsn) == before
    assert_serving(dsn, m2)


# --------------------------------------------------------------------------- end to end


def test_required_scenario_load_swap_rollback_reload_and_retention(
    dsn: str, tmp_path: Path
) -> None:
    """The M03 3.3 scenario, asserting views, corpus_state, state() and surviving tables each step.

    load v1 -> swap(1) -> load v2 -> swap(2) -> rollback() -> reload v2 -> load v3 -> swap(3).
    The point is the retention rule: after the rollback v1 is live and must survive swap(3) as the
    previous version, while v2 (the old live) is dropped.
    """
    m1 = write_export(tmp_path, 1, n_facts=40, n_chunks=30, n_companies=5)
    m2 = write_export(tmp_path, 2, n_facts=55, n_chunks=45, n_companies=7)
    m3 = write_export(tmp_path, 3, n_facts=35, n_chunks=25, n_companies=4)
    assert len({m1.chunks_rows, m2.chunks_rows, m3.chunks_rows}) == 3

    # 1. load v1: tables exist, nothing is served yet.
    load(dsn, tmp_path, 1)
    assert versioned_tables(dsn) == expected_tables(1)
    assert viewdef(dsn, "facts") is None and viewdef(dsn, "chunks") is None
    assert_corpus_state(dsn, live=None, previous=None)

    # 2. swap(1)
    swap(dsn, 1)
    assert_serving(dsn, m1)
    assert_corpus_state(dsn, live=1, previous=None)
    assert versioned_tables(dsn) == expected_tables(1)
    swapped_at_1 = corpus_state_rows(dsn)[0][3]

    # 3. load v2 while v1 serves
    load(dsn, tmp_path, 2)
    assert versioned_tables(dsn) == expected_tables(1, 2)
    assert_serving(dsn, m1)
    assert_corpus_state(dsn, live=1, previous=None)

    # 4. swap(2)
    swap(dsn, 2)
    assert_serving(dsn, m2)
    assert_corpus_state(dsn, live=2, previous=1)
    assert versioned_tables(dsn) == expected_tables(1, 2)
    assert corpus_state_rows(dsn)[0][3] >= swapped_at_1

    # 5. rollback -> v1 live, v2 kept as the new previous
    assert rollback(dsn) == 1
    assert_serving(dsn, m1)
    assert_corpus_state(dsn, live=1, previous=2)
    assert versioned_tables(dsn) == expected_tables(1, 2)

    # 6. reload v2 (the previous version, not live): same counts, no append, views still v1
    load(dsn, tmp_path, 2)
    assert scalar(dsn, "SELECT count(*) FROM facts_v2") == m2.facts_rows
    assert scalar(dsn, "SELECT count(*) FROM chunks_v2") == m2.chunks_rows
    assert scalar(dsn, "SELECT count(*) FROM companies_v2") == m2.companies_rows
    assert versioned_tables(dsn) == expected_tables(1, 2)
    assert_serving(dsn, m1)
    assert_corpus_state(dsn, live=1, previous=2)
    with pytest.raises(LoadError):  # v1 is live: refused
        load(dsn, tmp_path, 1)
    assert_serving(dsn, m1)

    # 7. load v3, swap(3): live 3, previous 1 (the rolled-back live), v2 dropped
    load(dsn, tmp_path, 3)
    assert versioned_tables(dsn) == expected_tables(1, 2, 3)
    swap(dsn, 3)
    assert_serving(dsn, m3)
    assert_corpus_state(dsn, live=3, previous=1)
    assert versioned_tables(dsn) == expected_tables(1, 3)
    assert leftovers(dsn, 2) == []

    # 8. the previous version can be reloaded, the live one cannot
    load(dsn, tmp_path, 1)
    assert scalar(dsn, "SELECT count(*) FROM chunks_v1") == m1.chunks_rows
    with pytest.raises(LoadError):
        load(dsn, tmp_path, 3)
    assert_serving(dsn, m3)
    assert_corpus_state(dsn, live=3, previous=1)


# --------------------------------------------------------------------------- atomicity


def test_concurrent_readers_only_ever_see_whole_versions_during_swap(
    dsn: str, tmp_path: Path
) -> None:
    """Readers looping on `chunks` and `facts` during swap(2) see only v1 or v2 counts.

    Two separate reader connections (one per view; a single statement over both views could
    deadlock on lock ordering regardless of the loader). The set of counts seen must be a subset
    of {v1, v2}, both counts must have been seen, and no reader may raise.
    """
    m1 = write_export(tmp_path, 1, n_facts=40, n_chunks=30)
    m2 = write_export(tmp_path, 2, n_facts=55, n_chunks=45, n_companies=7)
    load(dsn, tmp_path, 1)
    swap(dsn, 1)
    load(dsn, tmp_path, 2)

    expected = {"chunks": (m1.chunks_rows, m2.chunks_rows), "facts": (m1.facts_rows, m2.facts_rows)}
    seen: dict[str, set[int]] = {"chunks": set(), "facts": set()}
    errors: list[str] = []
    stop = threading.Event()
    started = {"chunks": threading.Event(), "facts": threading.Event()}

    def reader(view: str) -> None:
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                while not stop.is_set():
                    seen[view].add(conn.execute(f"SELECT count(*) FROM {view}").fetchone()[0])  # type: ignore[index]
                    started[view].set()
        except Exception as exc:  # noqa: BLE001 - the test asserts there are none
            errors.append(f"{view}: {exc!r}")
            started[view].set()

    threads = [threading.Thread(target=reader, args=(v,), daemon=True) for v in seen]
    for t in threads:
        t.start()
    try:
        assert all(e.wait(10) for e in started.values())
        swap(dsn, 2)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not all(expected[v][1] in seen[v] for v in seen):
            time.sleep(0.01)
    finally:
        stop.set()
        for t in threads:
            t.join(10)

    assert errors == []
    for view in seen:
        assert seen[view] == set(expected[view]), (view, seen[view])


# --------------------------------------------------------------------------- CLI


def test_cli_load_validate_swap_and_write_report(dsn: str, tmp_path: Path) -> None:
    """`main --version 1 --source DIR --swap --report-out FILE`: load, swap, JSON, exit 0."""
    m1 = write_export(tmp_path, 1)
    report_file = tmp_path / "report.json"

    rc = main(
        [
            "--dsn", dsn, "--version", "1", "--source", str(tmp_path),
            "--swap", "--report-out", str(report_file),
        ]
    )  # fmt: skip

    assert rc == 0
    assert_serving(dsn, m1)
    assert_corpus_state(dsn, live=1, previous=None)
    report = json.loads(report_file.read_text("utf-8"))
    assert set(report) >= {"version", "rows", "seconds", "sizes_bytes", "validation"}
    assert report["version"] == 1
    assert report["rows"] == {
        "facts": m1.facts_rows,
        "chunks": m1.chunks_rows,
        "companies": m1.companies_rows,
    }
    assert report["validation"]["fact_lookups_ok"] == 20


def test_cli_without_swap_loads_but_does_not_serve(dsn: str, tmp_path: Path) -> None:
    """Without --swap the CLI only loads (and validates): tables exist, nothing is live."""
    write_export(tmp_path, 1)
    rc = main(["--dsn", dsn, "--version", "1", "--source", str(tmp_path)])
    assert rc == 0
    assert versioned_tables(dsn) == expected_tables(1)
    assert state(dsn) == (None, None)
    assert viewdef(dsn, "chunks") is None


def test_cli_skip_indexes(dsn: str, tmp_path: Path) -> None:
    """--skip-indexes maps to build_indexes=False."""
    write_export(tmp_path, 1)
    assert main(["--dsn", dsn, "--version", "1", "--source", str(tmp_path), "--skip-indexes"]) == 0
    assert index_defs(dsn, "chunks_v1") == []


def test_cli_dsn_defaults_to_the_environment(
    dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --dsn the CLI uses EDGARQA_PG_DSN."""
    m1 = write_export(tmp_path, 1)
    monkeypatch.setenv("EDGARQA_PG_DSN", dsn)
    assert main(["--version", "1", "--source", str(tmp_path), "--swap"]) == 0
    assert_serving(dsn, m1)


def test_cli_rollback(dsn: str, tmp_path: Path) -> None:
    """`main --rollback` returns 0 and exchanges live/previous (no --version/--source needed)."""
    m1 = write_export(tmp_path, 1)
    write_export(tmp_path, 2, n_chunks=36, n_facts=44, n_companies=6)
    for v in (1, 2):
        assert main(["--dsn", dsn, "--version", str(v), "--source", str(tmp_path), "--swap"]) == 0
    assert_corpus_state(dsn, live=2, previous=1)

    assert main(["--dsn", dsn, "--rollback"]) == 0

    assert_corpus_state(dsn, live=1, previous=2)
    assert_serving(dsn, m1)


def test_cli_rollback_without_previous_fails(dsn: str, tmp_path: Path) -> None:
    """`--rollback` with nothing to roll back to: non-zero exit (or LoadError), state unchanged."""
    write_export(tmp_path, 1)
    assert main(["--dsn", dsn, "--version", "1", "--source", str(tmp_path), "--swap"]) == 0
    before = fingerprint(dsn)
    try:
        rc = main(["--dsn", dsn, "--rollback"])
    except LoadError:
        rc = 1
    assert rc != 0
    assert fingerprint(dsn) == before


def test_cli_bad_source_fails_and_changes_nothing(dsn: str, tmp_path: Path) -> None:
    """A source without the export: non-zero exit (or LoadError), no tables, nothing swapped."""
    try:
        rc = main(["--dsn", dsn, "--version", "1", "--source", str(tmp_path / "nope"), "--swap"])
    except LoadError:
        rc = 1
    assert rc != 0
    assert versioned_tables(dsn) == set()
    assert state(dsn) == (None, None)
    assert viewdef(dsn, "chunks") is None


def test_cli_failed_load_does_not_swap_over_a_live_version(dsn: str, tmp_path: Path) -> None:
    """`--swap` with a tampered v2 export fails; v1 stays live and v2 leaves no tables."""
    m1 = write_export(tmp_path, 1)
    assert main(["--dsn", dsn, "--version", "1", "--source", str(tmp_path), "--swap"]) == 0
    m2 = write_export(tmp_path, 2, n_chunks=36, n_facts=44, n_companies=6)
    (export_dir(tmp_path, 2) / m2.files["chunks"][0].path).unlink()

    try:
        rc = main(["--dsn", dsn, "--version", "2", "--source", str(tmp_path), "--swap"])
    except LoadError:
        rc = 1

    assert rc != 0
    assert leftovers(dsn, 2) == []
    assert_corpus_state(dsn, live=1, previous=None)
    assert_serving(dsn, m1)


# --------------------------------------------------------------------------------------------
# Stage C review fixes
# --------------------------------------------------------------------------------------------


def test_a_partially_loaded_version_without_the_completion_mark_is_never_swapped(
    dsn: str, tmp_path: Path
):
    """A load killed half-way leaves all three tables but no completion mark: swap and rollback
    must refuse them, whatever the row counts."""
    import psycopg

    write_export(tmp_path, 1)
    load_version(dsn, version=1, source=str(tmp_path))
    with psycopg.connect(dsn, autocommit=True) as conn:  # simulate the kill: remove the mark
        for table in ("facts_v1", "chunks_v1", "companies_v1"):
            conn.execute(f"COMMENT ON TABLE {table} IS NULL")

    with pytest.raises(LoadError, match="not loaded"):
        swap(dsn, 1)

    assert state(dsn) == (None, None)


def test_a_bad_maintenance_work_mem_is_refused_before_any_sql(dsn: str, tmp_path: Path):
    write_export(tmp_path, 1)

    with pytest.raises(LoadError, match="maintenance_work_mem"):
        load_version(
            dsn, version=1, source=str(tmp_path), maintenance_work_mem="1GB'; DROP TABLE x; --"
        )


def test_the_report_lists_server_notices_such_as_the_hnsw_memory_warning(dsn: str, tmp_path: Path):
    write_export(tmp_path, 1)

    report = load_version(dsn, version=1, source=str(tmp_path))

    assert isinstance(report.validation["server_notices"], list)


def test_rollback_prunes_versions_that_are_neither_live_nor_previous(dsn: str, tmp_path: Path):
    import psycopg

    for v in (1, 2):
        write_export(tmp_path, v)
        load_version(dsn, version=v, source=str(tmp_path))
        swap(dsn, v)
    write_export(tmp_path, 3)
    load_version(dsn, version=3, source=str(tmp_path))  # loaded, never swapped
    rollback(dsn)

    with psycopg.connect(dsn, autocommit=True) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE 'chunks_v%'"
            )
        }
    assert tables == {"chunks_v1", "chunks_v2"}


def test_nul_characters_in_text_are_removed_counted_and_never_drop_a_row(dsn: str, tmp_path: Path):
    """Postgres cannot store 0x00; one in the export used to abort the whole COPY."""
    manifest = write_export(tmp_path, 1)
    first = manifest.files["chunks"][0].path
    table = read_table(tmp_path, 1, "chunks")
    texts = table["text"].to_pylist()
    titles = table["section_title"].to_pylist()
    texts[0] = "before\x00after"
    texts[1] = "\x00\x00lead"
    titles[0] = "T\x00itle"
    table = table.set_column(
        table.schema.get_field_index("text"), "text", pa.array(texts, pa.string())
    )
    table = table.set_column(
        table.schema.get_field_index("section_title"),
        "section_title",
        pa.array(titles, pa.string()),
    )
    # keep the one-file layout: replace only the first file with the rows it already held
    keep = pq.read_metadata(tmp_path / "corpus_version=1" / first).num_rows
    replace_file(tmp_path, 1, first, table.slice(0, keep))

    report = load_version(dsn, version=1, source=str(tmp_path))

    removed = report.validation["nul_characters_removed"]
    assert removed["chunks.text"] == {"rows": 2, "chars": 3}
    assert removed["chunks.section_title"] == {"rows": 1, "chars": 1}
    stored = scalar(
        dsn, "SELECT text FROM chunks_v1 WHERE chunk_id = %s", (table["chunk_id"][0].as_py(),)
    )
    assert stored == "beforeafter"
    assert report.rows["chunks"] == manifest.chunks_rows


def test_swap_check_reads_the_view_target_from_its_definition(dsn: str, tmp_path: Path):
    """The real-data swap check identifies the table a view reads (views have no tableoid)."""
    from serving.bench.swap_check import view_target

    write_export(tmp_path, 1)
    load_version(dsn, version=1, source=str(tmp_path))
    swap(dsn, 1)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert view_target(conn, "chunks") == "chunks_v1"
        assert view_target(conn, "facts") == "facts_v1"
    write_export(tmp_path, 2)
    load_version(dsn, version=2, source=str(tmp_path))
    swap(dsn, 2)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert view_target(conn, "chunks") == "chunks_v2"
