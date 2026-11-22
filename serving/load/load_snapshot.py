"""Load a serving export into Postgres and swap it in atomically (M03 section 3.3, ADR-018 design).

    python -m serving.load.load_snapshot --version 1 --source s3://<lake>/exports/serving --swap
    python -m serving.load.load_snapshot --rollback

Order, and why: preflight first (nothing is created if the server, pgvector, the manifest or the
Parquet footers are wrong), then versioned tables `facts_vN`, `chunks_vN`, `companies_vN` are
dropped and recreated (a rerun never appends), bulk loaded with COPY, and only THEN indexed (the
HNSW build is much slower on a table that is loaded row by row). The load is validated against the
manifest before it can be swapped. `swap` replaces the `facts` / `chunks` views and the
`corpus_state` row in ONE transaction, so a reader sees the old or the new corpus, never a mix;
tables of versions other than (live, previous) are dropped afterwards.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from pipelines.common.manifest import MANIFEST_NAME, TABLES, Manifest, read_manifest

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).resolve().parents[1] / "sql" / "templates"
DIM = 384
MIN_PGVECTOR = (0, 8, 0)
SWAP_LOCK_TIMEOUT = "5s"
BATCH_ROWS = 20_000
_TABLE_RE = re.compile(r"^(facts|chunks|companies)_v(\d+)$")
_MEM_RE = re.compile(r"^\d+(kB|MB|GB)$")
LOADED_MARK = "edgarqa:loaded-and-validated"  # table comment written as the LAST step of a load

# Column order of each export table = the COPY column list (`tsv` is generated, not loaded).
COLUMNS = {
    "facts": [
        "fact_id", "fact_key", "cik", "tag", "taxonomy", "uom", "qtrs", "period_start",
        "period_end",
        "fiscal_year", "fiscal_period", "calendar_frame", "value", "accession", "form", "filed",
        "is_latest", "is_original", "is_restated", "anomaly_score", "derived_from",
    ],
    "chunks": [
        "chunk_id", "cik", "accession", "form", "fiscal_year", "fiscal_period", "section",
        "section_title", "chunk_index", "text", "n_tokens", "is_table", "source_url", "anchor",
        "page", "doc_source", "tenant_id", "embedding",
    ],
    "companies": ["cik", "name", "ticker", "sic", "fye_mmdd", "aliases"],
}  # fmt: skip


class LoadError(Exception):
    """The export or the database is not in a state that can be loaded or swapped safely."""


@dataclass(frozen=True)
class LoadReport:
    version: int
    rows: dict[str, int]
    seconds: dict[str, float]
    sizes_bytes: dict[str, int]
    validation: dict[str, object]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------------------- export access


def _open_fs(source: str) -> tuple[pafs.FileSystem, str]:
    """A pyarrow filesystem and the path of the export prefix (local directory or s3://)."""
    if "://" in source:
        fs, path = pafs.FileSystem.from_uri(source)
        return fs, path
    return pafs.LocalFileSystem(), str(Path(source).resolve())


def _read_manifest(fs: pafs.FileSystem, root: str, version: int) -> Manifest:
    path = f"{root}/corpus_version={version}/{MANIFEST_NAME}"
    try:
        with fs.open_input_stream(path) as handle:
            text = handle.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LoadError(f"cannot read {path}: {exc}") from exc
    try:
        manifest = read_manifest(text)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        raise LoadError(f"{path} is not a valid manifest: {exc}") from exc
    if manifest.corpus_version != version:
        raise LoadError(f"manifest says version {manifest.corpus_version}, expected {version}")
    return manifest


def _check_export(fs: pafs.FileSystem, root: str, manifest: Manifest) -> None:
    """Preflight on the files alone: dim, footer row counts, embedding length."""
    if manifest.dim != DIM:
        raise LoadError(f"manifest dim {manifest.dim} != {DIM}")
    base = f"{root}/corpus_version={manifest.corpus_version}"
    for table in TABLES:
        total = 0
        for f in manifest.files.get(table, []):
            try:
                with fs.open_input_file(f"{base}/{f.path}") as handle:
                    total += pq.ParquetFile(handle).metadata.num_rows
            except (OSError, pa.ArrowException) as exc:
                raise LoadError(f"missing or unreadable export file {f.path}: {exc}") from exc
        if total != manifest.expected_rows(table):
            raise LoadError(
                f"{table}: Parquet footers hold {total} rows, manifest says "
                f"{manifest.expected_rows(table)}"
            )
    first = manifest.files["chunks"][0].path if manifest.files.get("chunks") else None
    if first:
        with fs.open_input_file(f"{base}/{first}") as handle:
            batch = next(pq.ParquetFile(handle).iter_batches(batch_size=64), None)
        if batch is not None and batch.num_rows:
            lengths = set(pc.list_value_length(batch.column("embedding")).to_pylist())
            if lengths != {DIM}:
                raise LoadError(
                    f"embedding lengths in the export are {sorted(map(str, lengths))}, not {DIM}"
                )


# ----------------------------------------------------------------------------- COPY conversion


def _pg_array(values: list[Any] | None, quote: bool) -> str | None:
    if values is None:
        return None
    if not quote:
        return "{" + ",".join("NULL" if v is None else str(v) for v in values) + "}"
    parts = []
    for v in values:
        if v is None:
            parts.append("NULL")
        else:
            escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'"{escaped}"')
    return "{" + ",".join(parts) + "}"


def _vectors_as_text(column: pa.ChunkedArray | pa.Array) -> pa.Array:
    """`[x1,...,x384]` per row. Cast to float16 first: halfvec stores half precision anyway."""
    arr = column.combine_chunks() if isinstance(column, pa.ChunkedArray) else column
    n = len(arr)
    if arr.null_count:
        raise LoadError(f"{arr.null_count} NULL embeddings in the export")
    lengths = set(pc.list_value_length(arr).to_pylist())
    if lengths != {DIM}:
        raise LoadError(f"embedding lengths {sorted(lengths)} found, every row must have {DIM}")
    flat = np.asarray(arr.flatten().to_numpy(zero_copy_only=False), dtype=np.float32)
    matrix = flat.reshape(n, DIM).astype(np.float16)
    return pa.array(["[" + ",".join(map(repr, row.tolist())) + "]" for row in matrix], pa.string())


def _csv_bytes(table: str, batch: pa.RecordBatch) -> bytes:
    """One record batch as quoted CSV for `COPY ... (FORMAT csv, NULL '')`.

    Every non-null value is quoted, so an empty string is `""` (an empty string) while a NULL is an
    unquoted empty field. Nested columns become Postgres literals first.
    """
    arrays: list[pa.Array] = []
    for name in COLUMNS[table]:
        col = batch.column(name)
        if name == "embedding":
            col = _vectors_as_text(col)
        elif name == "derived_from":
            col = pa.array([_pg_array(v, quote=False) for v in col.to_pylist()], pa.string())
        elif name == "aliases":
            col = pa.array([_pg_array(v or [], quote=True) for v in col.to_pylist()], pa.string())
        arrays.append(col)
    sink = io.BytesIO()
    options = pacsv.WriteOptions(include_header=False, quoting_style="all_valid")
    pacsv.write_csv(pa.table(arrays, names=COLUMNS[table]), sink, write_options=options)
    return sink.getvalue()


def _copy_table(
    conn: psycopg.Connection[Any], fs: pafs.FileSystem, base: str, manifest: Manifest, table: str,
    target: str,
) -> int:  # fmt: skip
    cols = ", ".join(COLUMNS[table])
    loaded = 0
    with (
        conn.cursor() as cur,
        cur.copy(f"COPY {target} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')") as copy,
    ):
        for f in manifest.files[table]:
            with fs.open_input_file(f"{base}/{f.path}") as handle:
                for batch in pq.ParquetFile(handle).iter_batches(batch_size=BATCH_ROWS):
                    copy.write(_csv_bytes(table, batch))
                    loaded += batch.num_rows
    return loaded


# ----------------------------------------------------------------------------- database helpers


def _parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def _preflight_server(conn: psycopg.Connection[Any]) -> None:
    major = int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000  # type: ignore[index]
    if major != 17:
        raise LoadError(f"PostgreSQL {major} found, 17 required")
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    version = _parse_version(row[0]) if row else ()
    if version < MIN_PGVECTOR:
        raise LoadError(f"pgvector {row[0] if row else 'missing'} found, >= 0.8.0 required")


def _ensure_state_table(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS corpus_state (id int PRIMARY KEY DEFAULT 1 CHECK (id = 1), "
        "live_version int NOT NULL, previous_version int, swapped_at timestamptz)"
    )


def _fetch_state(conn: psycopg.Connection[Any]) -> tuple[int | None, int | None]:
    exists = conn.execute("SELECT to_regclass('corpus_state')").fetchone()
    if not exists or exists[0] is None:
        return (None, None)
    row = conn.execute(
        "SELECT live_version, previous_version FROM corpus_state WHERE id = 1"
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def state(dsn: str) -> tuple[int | None, int | None]:
    """`(live_version, previous_version)`; `(None, None)` before the first swap."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        return _fetch_state(conn)


def _versions_present(conn: psycopg.Connection[Any]) -> dict[int, set[str]]:
    found: dict[int, set[str]] = {}
    for (name,) in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    ):
        m = _TABLE_RE.match(name)
        if m:
            found.setdefault(int(m.group(2)), set()).add(m.group(1))
    return found


def _drop_version(conn: psycopg.Connection[Any], version: int) -> None:
    for table in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}_v{int(version)}")


def _index_statements(version: int, m: int, ef: int) -> list[tuple[str, str]]:
    text = (TEMPLATES / "indexes.sql").read_text(encoding="utf-8")
    pairs = re.findall(r"-- name: (\w+)\n(.*?)(?=\n-- name:|\Z)", text, flags=re.S)
    return [(name, sql.strip().format(v=int(version), m=int(m), ef=int(ef))) for name, sql in pairs]


def _create_tables(conn: psycopg.Connection[Any], version: int) -> None:
    for name in ("facts", "chunks", "companies"):
        sql = (TEMPLATES / f"{name}.sql").read_text(encoding="utf-8")
        conn.execute(sql.format(v=int(version)))


# ----------------------------------------------------------------------------- load


def load_version(
    dsn: str,
    *,
    version: int,
    source: str,
    build_indexes: bool = True,
    hnsw_m: int = 16,
    hnsw_ef_construction: int = 64,
    maintenance_work_mem: str = "1GB",
    sample_checks: int = 20,
) -> LoadReport:
    """Load `corpus_version=<version>` of `source` into `*_v<version>`; does not swap."""
    if not _MEM_RE.match(maintenance_work_mem):
        raise LoadError(
            f"maintenance_work_mem {maintenance_work_mem!r} must look like 3GB or 512MB"
        )
    fs, root = _open_fs(source)
    manifest = _read_manifest(fs, root, version)
    base = f"{root}/corpus_version={version}"
    rows: dict[str, int] = {}
    seconds: dict[str, float] = {}
    sizes: dict[str, int] = {}
    validation: dict[str, object] = {}
    notices: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.add_notice_handler(lambda diag: notices.append(diag.message_primary or ""))
        _preflight_server(conn)
        _check_export(fs, root, manifest)
        live, _previous = _fetch_state(conn)
        if live == version:
            raise LoadError(f"version {version} is live; load a new version or roll back first")
        _drop_version(conn, version)
        try:
            _create_tables(conn, version)
            for table in ("companies", "facts", "chunks"):
                started = time.perf_counter()
                with conn.transaction():
                    n = _copy_table(conn, fs, base, manifest, table, f"{table}_v{version}")
                seconds[f"copy_{table}"] = time.perf_counter() - started
                rows[table] = n
                if n != manifest.expected_rows(table):
                    raise LoadError(
                        f"{table}: loaded {n} rows, manifest says {manifest.expected_rows(table)}"
                    )
            conn.execute(f"ANALYZE facts_v{version}")
            conn.execute(f"ANALYZE chunks_v{version}")
            if build_indexes:
                conn.execute(f"SET maintenance_work_mem = '{maintenance_work_mem}'")
                conn.execute("SET max_parallel_maintenance_workers = 2")
                for name, sql in _index_statements(version, hnsw_m, hnsw_ef_construction):
                    started = time.perf_counter()
                    conn.execute(sql)  # type: ignore[arg-type]
                    seconds[f"index_{name}"] = time.perf_counter() - started
            validation = _validate(conn, version, manifest, sample_checks)
            validation["server_notices"] = [n for n in notices if n]
            sizes = _sizes(conn, version)
            # Last step: a version without this mark is partial (a killed load); never swapped in.
            for table in TABLES:
                conn.execute(f"COMMENT ON TABLE {table}_v{int(version)} IS '{LOADED_MARK}'")
        except BaseException:
            _drop_version(conn, version)
            raise
    log.info("loaded version %d: %s", version, rows)
    return LoadReport(
        version=version, rows=rows, seconds=seconds, sizes_bytes=sizes, validation=validation
    )


def _validate(
    conn: psycopg.Connection[Any], version: int, manifest: Manifest, checks: int
) -> dict[str, object]:
    """Row counts equal the manifest; sampled fact lookups and text searches return rows."""
    out: dict[str, object] = {}
    for table in TABLES:
        n = conn.execute(f"SELECT count(*) FROM {table}_v{version}").fetchone()[0]  # type: ignore[index]
        if n != manifest.expected_rows(table):
            raise LoadError(
                f"{table}_v{version} holds {n} rows, manifest says {manifest.expected_rows(table)}"
            )
        out[f"{table}_rows"] = n
    rng = random.Random(version)
    sample = conn.execute(
        f"SELECT cik, tag, fiscal_year, fiscal_period, uom FROM facts_v{version} "
        f"WHERE is_original ORDER BY fact_id LIMIT 2000"
    ).fetchall()
    chosen = rng.sample(sample, min(checks, len(sample)))
    ok = 0
    for cik, tag, fy, fp, uom in chosen:
        hit = conn.execute(
            f"SELECT 1 FROM facts_v{version} WHERE cik = %s AND tag = %s AND fiscal_year = %s "
            "AND fiscal_period = %s AND uom = %s AND is_original LIMIT 1",
            (cik, tag, fy, fp, uom),
        ).fetchone()
        if not hit:
            raise LoadError(f"fact lookup found nothing for {(cik, tag, fy, fp, uom)}")
        ok += 1
    out["fact_lookups_ok"] = ok
    words = conn.execute(
        f"SELECT text FROM chunks_v{version} ORDER BY chunk_id LIMIT 2000"
    ).fetchall()
    searched = 0
    for (text,) in rng.sample(words, min(checks, len(words))):
        word = _searchable_word(conn, text)
        if word is None:  # e.g. a chunk made only of stopwords: nothing to search for
            continue
        hit = conn.execute(
            f"SELECT 1 FROM chunks_v{version} "
            "WHERE tsv @@ websearch_to_tsquery('english', %s) LIMIT 1",
            (word,),
        ).fetchone()
        if not hit:
            raise LoadError(f"text search for {word!r} found nothing")
        searched += 1
    out["text_searches_ok"] = searched
    return out


def _searchable_word(conn: psycopg.Connection[Any], text: str) -> str | None:
    """A word of `text` that survives the English text-search configuration (not a stopword)."""
    for candidate in re.findall(r"[A-Za-z]{5,}", text)[:30]:
        row = conn.execute(
            "SELECT numnode(websearch_to_tsquery('english', %s))", (candidate,)
        ).fetchone()
        if row and row[0] > 0:
            return candidate
    return None


def _sizes(conn: psycopg.Connection[Any], version: int) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for table in TABLES:
        sizes[f"{table}_v{version}"] = conn.execute(
            f"SELECT pg_total_relation_size('{table}_v{version}')"
        ).fetchone()[0]  # type: ignore[index]
    own = re.compile(rf"^(facts|chunks)_v{int(version)}_")
    for (name,) in conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
    ).fetchall():
        if not own.match(name):
            continue
        sizes[f"index:{name}"] = conn.execute(
            "SELECT pg_relation_size(%s::regclass)", (name,)
        ).fetchone()[0]  # type: ignore[index]
    return sizes


# ----------------------------------------------------------------------------- swap / rollback


def _require_tables(conn: psycopg.Connection[Any], version: int) -> None:
    have = _versions_present(conn).get(version, set())
    missing = set(TABLES) - have
    if missing:
        raise LoadError(f"version {version} is not loaded (missing {sorted(missing)})")
    for table in TABLES:
        mark = conn.execute(
            "SELECT obj_description(to_regclass(%s), 'pg_class')", (f"{table}_v{int(version)}",)
        ).fetchone()
        if not mark or mark[0] != LOADED_MARK:
            raise LoadError(
                f"version {version} is not loaded: {table}_v{version} was never marked complete "
                "(a load that was killed half-way leaves such a table); reload it"
            )


def _point_at(conn: psycopg.Connection[Any], version: int) -> None:
    """Views and the unversioned companies table now read `version` (call inside a transaction)."""
    conn.execute(f"CREATE OR REPLACE VIEW facts AS SELECT * FROM facts_v{int(version)}")
    conn.execute(f"CREATE OR REPLACE VIEW chunks AS SELECT * FROM chunks_v{int(version)}")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS companies (cik int PRIMARY KEY, name text NOT NULL, "
        "ticker text, "
        "sic int, fye_mmdd char(4), aliases text[] NOT NULL DEFAULT '{}')"
    )
    conn.execute("DELETE FROM companies")
    conn.execute(f"INSERT INTO companies SELECT * FROM companies_v{int(version)}")


def _retain(conn: psycopg.Connection[Any]) -> list[int]:
    """Drop every version that is neither live nor previous; returns the versions dropped."""
    live, previous = _fetch_state(conn)
    keep = {v for v in (live, previous) if v is not None}
    dropped: list[int] = []
    for version in sorted(_versions_present(conn)):
        if version in keep:
            continue
        try:
            with conn.transaction():
                conn.execute(f"SET LOCAL lock_timeout = '{SWAP_LOCK_TIMEOUT}'")
                _drop_version(conn, version)
            dropped.append(version)
        except psycopg.errors.LockNotAvailable:
            log.warning("could not drop version %d (in use); it stays until the next swap", version)
    return dropped


def swap(dsn: str, version: int) -> None:
    """Make `version` live in one transaction (views, companies, corpus_state), then prune."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        _ensure_state_table(conn)
        live, _ = _fetch_state(conn)
        if live == version:
            raise LoadError(f"version {version} is already live")
        _require_tables(conn, version)
        with conn.transaction():
            conn.execute(f"SET LOCAL lock_timeout = '{SWAP_LOCK_TIMEOUT}'")
            _point_at(conn, version)
            conn.execute(
                "INSERT INTO corpus_state (id, live_version, previous_version, swapped_at) "
                "VALUES (1, %s, NULL, now()) ON CONFLICT (id) DO UPDATE SET "
                "previous_version = corpus_state.live_version, "
                "live_version = EXCLUDED.live_version, swapped_at = EXCLUDED.swapped_at",
                (version,),
            )
        _retain(conn)


def rollback(dsn: str) -> int:
    """Swap live and previous in one transaction; returns the new live version."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        _ensure_state_table(conn)
        with conn.transaction():
            row = conn.execute(
                "SELECT live_version, previous_version FROM corpus_state WHERE id = 1 FOR UPDATE"
            ).fetchone()
            if not row or row[1] is None:
                raise LoadError("nothing to roll back to: there is no previous version")
            live, previous = row
            _require_tables(conn, previous)
            conn.execute(f"SET LOCAL lock_timeout = '{SWAP_LOCK_TIMEOUT}'")
            _point_at(conn, previous)
            conn.execute(
                "UPDATE corpus_state SET live_version = %s, previous_version = %s, "
                "swapped_at = now() "
                "WHERE id = 1",
                (previous, live),
            )
        _retain(conn)
        return int(previous)


# ----------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load a serving export and swap it in.")
    parser.add_argument("--dsn", default=os.environ.get("EDGARQA_PG_DSN"))
    parser.add_argument("--version", type=int)
    parser.add_argument("--source", help="export prefix: a directory or s3://bucket/prefix")
    parser.add_argument("--swap", action="store_true", help="swap the version in after loading")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--skip-indexes", action="store_true")
    parser.add_argument("--maintenance-work-mem", default="1GB")
    parser.add_argument("--report-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.dsn:
        print("no --dsn and EDGARQA_PG_DSN is not set", file=sys.stderr)
        return 2
    try:
        if args.rollback:
            live = rollback(args.dsn)
            print(json.dumps({"rolled_back_to": live}))
            return 0
        if args.version is None or not args.source:
            print("--version and --source are required to load", file=sys.stderr)
            return 2
        report = load_version(
            args.dsn,
            version=args.version,
            source=args.source,
            build_indexes=not args.skip_indexes,
            maintenance_work_mem=args.maintenance_work_mem,
        )
        if args.swap:
            started = time.perf_counter()
            swap(args.dsn, args.version)
            report.seconds["swap"] = time.perf_counter() - started
    except (LoadError, psycopg.Error) as exc:
        print(f"load failed: {exc}", file=sys.stderr)
        return 1
    if args.report_out:
        args.report_out.write_text(json.dumps(report.to_dict(), indent=1) + "\n", encoding="utf-8")
    print(json.dumps(report.to_dict()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
