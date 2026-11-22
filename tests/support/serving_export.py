"""Writes a tiny serving export (contract section 2.3) to a local directory, for loader tests.

Layout: ``{root}/corpus_version={N}/{companies,facts,chunks}/part-*.parquet`` plus a valid
``_MANIFEST.json`` (paths in the manifest are relative to ``corpus_version={N}/``). Real Parquet
footer row counts and file sizes are recorded, so the loader's preflight sees a genuine export.

The data is deterministic in ``(version, seed)`` and is built to be adversarial for a COPY-based
loader: the first chunk's text holds tabs, newlines, CRLF, backslashes, the COPY end marker
``\\.``, a literal ``\\N``, quotes and non-ASCII; one company name and its aliases carry quotes,
commas, braces and tabs; facts mix NULL and non-NULL optional columns, include a 28-digit decimal
and non-original rows.

Nothing here talks to Postgres.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from pipelines.common.embedding import (
    ARTIFACT_HF_REPO,
    ARTIFACT_HF_REVISION,
    ARTIFACT_ONNX_FILE,
    ARTIFACT_ONNX_SHA256,
    DIM,
    MODEL_NAME,
    l2_normalize,
)
from pipelines.common.manifest import (
    MANIFEST_NAME,
    ChunkExclusions,
    EmbeddingArtifact,
    EmbeddingScope,
    Manifest,
    ManifestFile,
    NotEmbedded,
    footer_rows,
)

FACTS_SCHEMA = pa.schema(
    [
        ("fact_id", pa.int64()),
        ("fact_key", pa.int64()),
        ("cik", pa.int32()),
        ("tag", pa.string()),
        ("taxonomy", pa.string()),
        ("uom", pa.string()),
        ("qtrs", pa.int32()),
        ("period_start", pa.date32()),
        ("period_end", pa.date32()),
        ("fiscal_year", pa.int32()),
        ("fiscal_period", pa.string()),
        ("calendar_frame", pa.string()),
        ("value", pa.decimal128(28, 4)),
        ("accession", pa.string()),
        ("form", pa.string()),
        ("filed", pa.date32()),
        ("is_latest", pa.bool_()),
        ("is_original", pa.bool_()),
        ("is_restated", pa.bool_()),
        ("anomaly_score", pa.float32()),
        ("derived_from", pa.list_(pa.int64())),
    ]
)

CHUNKS_SCHEMA = pa.schema(
    [
        ("chunk_id", pa.string()),
        ("cik", pa.int32()),
        ("accession", pa.string()),
        ("form", pa.string()),
        ("fiscal_year", pa.int32()),
        ("fiscal_period", pa.string()),
        ("section", pa.string()),
        ("section_title", pa.string()),
        ("chunk_index", pa.int32()),
        ("text", pa.string()),
        ("n_tokens", pa.int32()),
        ("is_table", pa.bool_()),
        ("source_url", pa.string()),
        ("anchor", pa.string()),
        ("page", pa.int32()),
        ("doc_source", pa.string()),
        ("tenant_id", pa.int32()),
        ("embedding", pa.list_(pa.float32())),
    ]
)

COMPANIES_SCHEMA = pa.schema(
    [
        ("cik", pa.int32()),
        ("name", pa.string()),
        ("ticker", pa.string()),
        ("sic", pa.int32()),
        ("fye_mmdd", pa.string()),
        ("aliases", pa.list_(pa.string())),
    ]
)

SCHEMAS = {"facts": FACTS_SCHEMA, "chunks": CHUNKS_SCHEMA, "companies": COMPANIES_SCHEMA}

# Text with every character a COPY text-format loader can mangle. Chunk 0 carries it.
NASTY_TEXT = (
    "Tab:\there. Newline:\nsecond line. CRLF:\r\nthird line. "
    "Backslash: C:\\path\\new\\table, literal \\N and \\\\ and an end marker line:\n\\.\nepilogue. "
    'Quotes: \'single\' "double" `tick` and a CSV-like a,b,"c""d". '
    "Unicode: caf\u00e9 na\u00efve \u65e5\u672c\u8a9e \U0001f600. The word NULL and null."
)
NASTY_COMPANY_NAME = 'O\'Brien "Sons" & Co\twith tab'
NASTY_ALIASES = ["A, B", 'C"D', "{x}", "e'f", "back\\slash", "NULL", ""]

STOPWORD_TEXT = "the of and to a is these about which there would their"

_TOPICS = ("litigation", "cybersecurity", "liquidity", "inflation", "tariffs", "goodwill")
_TAGS = (
    ("Revenues", "us-gaap", "USD", 4),
    ("NetIncomeLoss", "us-gaap", "USD", 4),
    ("Assets", "us-gaap", "USD", 0),
    ("EntityCommonStockSharesOutstanding", "dei", "shares", 0),
)
_FISCAL_PERIODS = ("FY", "Q1", "Q2", "Q3")
_FYE = ("1231", "0930", "0630")
FIRST_CIK = 1000


def export_dir(root: Path, version: int) -> Path:
    """``{root}/corpus_version={version}``: the directory that holds the tables and the manifest."""
    return Path(root) / f"corpus_version={version}"


def manifest_path(root: Path, version: int) -> Path:
    return export_dir(root, version) / MANIFEST_NAME


def company_ciks(n_companies: int) -> list[int]:
    return [FIRST_CIK + k for k in range(n_companies)]


def rare_word(i: int) -> str:
    """A letters-only word that occurs in exactly one chunk's default text (chunk index `i`)."""
    letters = "".join(chr(97 + (i // 26**k) % 26) for k in (2, 1, 0))
    return f"zq{letters}x"


def _split(n: int, parts: int) -> list[range]:
    parts = max(1, min(parts, n)) if n else 1
    edges = np.linspace(0, n, parts + 1).astype(int)
    return [range(int(a), int(b)) for a, b in zip(edges[:-1], edges[1:], strict=True)]


def _companies_table(version: int, n_companies: int) -> pa.Table:
    rows = []
    for k, cik in enumerate(company_ciks(n_companies)):
        name = NASTY_COMPANY_NAME + f" v{version}" if k == 0 else f"Company {cik} v{version}"
        rows.append(
            {
                "cik": cik,
                "name": name,
                "ticker": None if k % 4 == 3 else f"T{k:03d}",
                "sic": None if k % 3 == 2 else 3500 + k,
                "fye_mmdd": _FYE[k % len(_FYE)],
                "aliases": NASTY_ALIASES if k == 0 else ([] if k % 2 else [f"Alias {cik}"]),
            }
        )
    return pa.Table.from_pylist(rows, schema=COMPANIES_SCHEMA)


def _facts_table(version: int, n_facts: int, n_companies: int) -> pa.Table:
    ciks = company_ciks(n_companies)
    rows = []
    for i in range(n_facts):
        tag, taxonomy, uom, qtrs = _TAGS[i % len(_TAGS)]
        fy = 2020 + (i // len(_TAGS)) % 5
        fp = _FISCAL_PERIODS[(i // 3) % len(_FISCAL_PERIODS)]
        end = date(fy, 12, 31)
        if i == 0:
            value = Decimal("123456789012345678901234.5678")  # 28 significant digits
        elif i == 1:
            value = Decimal("-0.0001")
        else:
            value = Decimal(f"{1000 + i * 37}.{(i * 7919) % 10000:04d}")
        rows.append(
            {
                "fact_id": version * 1_000_000 + i + 1,
                "fact_key": version * 1_000_000 + i // 2 + 1,
                "cik": ciks[i % len(ciks)],
                "tag": tag,
                "taxonomy": taxonomy,
                "uom": uom,
                "qtrs": qtrs,
                "period_start": date(fy, 1, 1) if qtrs else None,
                "period_end": end,
                "fiscal_year": fy,
                "fiscal_period": fp,
                "calendar_frame": f"CY{fy}" if i % 3 else None,
                "value": value,
                "accession": f"0000000000-{fy % 100:02d}-{i:06d}",
                "form": "10-K",
                "filed": date(fy + 1, 2, 1 + i % 27),
                "is_latest": i % 6 != 5,
                "is_original": i % 5 != 4,  # some non-original rows: lookups must not assume
                "is_restated": i % 11 == 10,
                "anomaly_score": None if i % 4 else float(np.float32(i) / np.float32(7)),
                "derived_from": [version * 1_000_000 + i, version * 1_000_000 + i + 1]
                if i % 7 == 6
                else None,
            }
        )
    return pa.Table.from_pylist(rows, schema=FACTS_SCHEMA)


def _embedding_array(matrix: np.ndarray) -> pa.Array:
    n, dim = matrix.shape
    offsets = pa.array(np.arange(0, (n + 1) * dim, dim, dtype=np.int32), pa.int32())
    values = pa.array(np.ascontiguousarray(matrix, dtype=np.float32).ravel(), pa.float32())
    return pa.ListArray.from_arrays(offsets, values)


def default_embeddings(version: int, seed: int, n: int, dim: int) -> np.ndarray:
    """(n, dim) float32 unit vectors, deterministic in (version, seed)."""
    rng = np.random.default_rng(seed * 1000 + version)
    return l2_normalize(rng.normal(size=(n, dim)))


def _chunks_table(
    version: int,
    n_chunks: int,
    n_companies: int,
    seed: int,
    dim: int,
    chunk_ciks: Sequence[int] | None,
    chunk_fiscal_years: Sequence[int] | None,
    texts: Mapping[int, str] | None,
    embeddings: np.ndarray | None,
    stopword_chunk: bool,
    nasty_text: bool,
) -> pa.Table:
    ciks = company_ciks(n_companies)
    if embeddings is None:
        matrix = default_embeddings(version, seed, n_chunks, dim)
    else:
        matrix = l2_normalize(np.asarray(embeddings))
        if matrix.shape != (n_chunks, dim):
            raise ValueError(f"embeddings shape {matrix.shape} != {(n_chunks, dim)}")
    if chunk_ciks is not None and len(chunk_ciks) != n_chunks:
        raise ValueError("chunk_ciks must have one entry per chunk")
    if chunk_fiscal_years is not None and len(chunk_fiscal_years) != n_chunks:
        raise ValueError("chunk_fiscal_years must have one entry per chunk")

    cols: dict[str, list[Any]] = {name: [] for name in CHUNKS_SCHEMA.names if name != "embedding"}
    for i in range(n_chunks):
        cik = chunk_ciks[i] if chunk_ciks is not None else ciks[i % len(ciks)]
        fy = chunk_fiscal_years[i] if chunk_fiscal_years is not None else 2020 + i % 4
        topic = _TOPICS[i % len(_TOPICS)]
        if texts is not None and i in texts:
            text = texts[i]
        elif i == 0 and nasty_text:
            text = f"{NASTY_TEXT} Marker {rare_word(i)}."
        elif stopword_chunk and i == n_chunks - 1:
            text = STOPWORD_TEXT
        else:
            text = (
                f"Chunk {i}. The registrant reports revenue and operating results for fiscal "
                f"{fy}. Risk factor on {topic}. Unique marker {rare_word(i)}."
            )
        cols["chunk_id"].append(f"chk-{i:06d}")
        cols["cik"].append(cik)
        cols["accession"].append(f"0000000000-{fy % 100:02d}-{i:06d}")
        cols["form"].append("10-K")
        cols["fiscal_year"].append(fy)
        cols["fiscal_period"].append("FY")
        cols["section"].append(None if i % 5 == 4 else f"item_{1 + i % 3}")
        cols["section_title"].append(None if i % 5 == 4 else f"Item {1 + i % 3}")
        cols["chunk_index"].append(i)
        cols["text"].append(text)
        cols["n_tokens"].append(len(text.split()))
        cols["is_table"].append(i % 9 == 8)
        cols["source_url"].append(f"https://example.test/{cik}/{i}.htm")
        cols["anchor"].append(None if i % 2 else f"a{i}")
        cols["page"].append(None if i % 3 else i)
        cols["doc_source"].append("edgar_html")
        cols["tenant_id"].append(None)
    arrays = {name: pa.array(vals, CHUNKS_SCHEMA.field(name).type) for name, vals in cols.items()}
    arrays["embedding"] = _embedding_array(matrix)
    return pa.table([arrays[n] for n in CHUNKS_SCHEMA.names], schema=CHUNKS_SCHEMA)


def _write_parts(table_dir: Path, table: pa.Table, n_files: int) -> list[Path]:
    table_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for j, rows in enumerate(_split(table.num_rows, n_files)):
        path = table_dir / f"part-{j:05d}.parquet"
        pq.write_table(table.slice(rows.start, len(rows)), path, compression="zstd")
        paths.append(path)
    return paths


def _manifest_file(base: Path, path: Path) -> ManifestFile:
    return ManifestFile(
        path=path.relative_to(base).as_posix(),
        rows=footer_rows(path),
        bytes=path.stat().st_size,
    )


def _build_manifest(base: Path, version: int, dim: int, files: dict[str, list[Path]]) -> Manifest:
    mfiles = {
        t: [_manifest_file(base, p) for p in files[t]] for t in ("companies", "facts", "chunks")
    }
    total = {t: sum(f.rows for f in fs) for t, fs in mfiles.items()}
    return Manifest(
        corpus_version=version,
        created_at="2026-11-22T00:00:00Z",
        git_sha="abc1234",
        facts_rows=total["facts"],
        chunks_rows=total["chunks"],
        companies_rows=total["companies"],
        embedding_model=MODEL_NAME,
        embedding_artifact=EmbeddingArtifact(
            hf_repo=ARTIFACT_HF_REPO,
            hf_revision=ARTIFACT_HF_REVISION,
            onnx_file=ARTIFACT_ONNX_FILE,
            onnx_sha256=ARTIFACT_ONNX_SHA256,
        ),
        dim=dim,
        chunk_exclusions=ChunkExclusions(rule="n_tokens <= 5", count=0),
        embedding_scope=EmbeddingScope(
            rule="form IN ('10-K','10-K/A') OR doc_source = 'financebench_pdf'",
            not_embedded=NotEmbedded(
                rule="doc_source = 'edgar_html' AND form IN ('10-Q','10-Q/A')",
                count=0,
                reason="serving is 10-K-only; quarterly numbers come from curated.facts (ADR-037)",
            ),
        ),
        iceberg_snapshots={
            "curated.facts": 11,
            "curated.chunks": 12,
            "curated.chunk_embeddings": 13,
        },
        files=mfiles,
        filters={"facts": "test facts filter", "chunks": "test chunks filter"},
    )


def _write_manifest(root: Path, version: int, manifest: Manifest) -> None:
    manifest_path(root, version).write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


def write_export(
    root: Path,
    version: int,
    n_facts: int = 40,
    n_chunks: int = 30,
    n_companies: int = 5,
    seed: int = 0,
    dim: int = DIM,
    *,
    fact_files: int = 2,
    chunk_files: int = 2,
    chunk_ciks: Sequence[int] | None = None,
    chunk_fiscal_years: Sequence[int] | None = None,
    texts: Mapping[int, str] | None = None,
    embeddings: np.ndarray | None = None,
    stopword_chunk: bool = False,
    nasty_text: bool = True,
) -> Manifest:
    """Write ``{root}/corpus_version={version}/...`` and return its (already written) manifest.

    Optional per-chunk overrides (all length ``n_chunks``): ``chunk_ciks``, ``chunk_fiscal_years``,
    ``embeddings`` ((n_chunks, dim), re-normalised); ``texts`` maps chunk index to text.
    ``stopword_chunk`` makes the LAST chunk consist only of English stop words (its tsvector is
    empty). ``dim`` sets both the vector length and ``manifest.dim``. Chunk ids are
    ``chk-000000``...; fact ids embed the version.
    """
    base = export_dir(root, version)
    files = {
        "companies": _write_parts(base / "companies", _companies_table(version, n_companies), 1),
        "facts": _write_parts(
            base / "facts", _facts_table(version, n_facts, n_companies), fact_files
        ),
        "chunks": _write_parts(
            base / "chunks",
            _chunks_table(
                version,
                n_chunks,
                n_companies,
                seed,
                dim,
                chunk_ciks,
                chunk_fiscal_years,
                texts,
                embeddings,
                stopword_chunk,
                nasty_text,
            ),
            chunk_files,
        ),
    }
    manifest = _build_manifest(base, version, dim, files)
    _write_manifest(root, version, manifest)
    return manifest


def read_table(root: Path, version: int, name: str) -> pa.Table:
    """The whole exported table `name`, files concatenated in manifest order."""
    base = export_dir(root, version)
    manifest = Manifest.model_validate_json(manifest_path(root, version).read_text("utf-8"))
    tables = [pq.read_table(base / f.path) for f in manifest.files[name]]
    return pa.concat_tables(tables)


def embedding_matrix(table: pa.Table) -> np.ndarray:
    """(n, dim) float32 matrix of a chunks table's ``embedding`` column."""
    return np.array([np.asarray(v, dtype=np.float32) for v in table["embedding"].to_pylist()])


def rewrite_manifest(root: Path, version: int, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Load ``_MANIFEST.json`` as a plain dict, let `mutate` edit it in place, write it back.

    Deliberately bypasses the Pydantic model so a test can write an INVALID manifest.
    """
    path = manifest_path(root, version)
    data = json.loads(path.read_text("utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def replace_file(root: Path, version: int, rel_path: str, table: pa.Table) -> Manifest:
    """Overwrite one exported Parquet file and make the manifest consistent with the new file.

    Used to craft exports that are structurally valid (footers == manifest) but whose DATA is bad
    (duplicate key, NULL embedding): only the COPY or the validation step can catch them.
    """
    base = export_dir(root, version)
    pq.write_table(table, base / rel_path, compression="zstd")
    old = Manifest.model_validate_json(manifest_path(root, version).read_text("utf-8"))
    files = {t: [base / f.path for f in fs] for t, fs in old.files.items()}
    manifest = _build_manifest(base, version, old.dim, files)
    _write_manifest(root, version, manifest)
    return manifest
