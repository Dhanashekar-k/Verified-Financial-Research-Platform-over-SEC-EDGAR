"""Unit tests for ``pipelines/common/manifest.py`` (the serving export manifest, contract 2.3).

The model is shared by the export job (writer) and the serving loader (reader, no Spark), so what
it rejects is what keeps a hand-edited or half-written manifest from being loaded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from pipelines.common.manifest import (
    MANIFEST_NAME,
    TABLES,
    Manifest,
    footer_rows,
    read_manifest,
)


def payload(
    facts: list[int] | None = None,
    chunks: list[int] | None = None,
    companies: list[int] | None = None,
) -> dict[str, Any]:
    """A valid manifest as a dict; the file row counts decide the headline counts."""
    facts, chunks, companies = facts or [3, 4], chunks or [5], companies or [2]

    def files(table: str, rows: list[int]) -> list[dict[str, Any]]:
        return [
            {"path": f"{table}/part-{i:05d}.parquet", "rows": n, "bytes": 100 + n}
            for i, n in enumerate(rows)
        ]

    return {
        "corpus_version": 7,
        "created_at": "2026-12-04T03:12:00Z",
        "git_sha": "abc1234",
        "facts_rows": sum(facts),
        "chunks_rows": sum(chunks),
        "companies_rows": sum(companies),
        "embedding_model": "BAAI/bge-small-en-v1.5",
        "embedding_artifact": {
            "hf_repo": "Qdrant/bge-small-en-v1.5-onnx-Q",
            "hf_revision": "a" * 40,
            "onnx_file": "model_optimized.onnx",
            "onnx_sha256": "b" * 64,
        },
        "dim": 384,
        "chunk_exclusions": {"rule": "n_tokens <= 5", "count": 3},
        "embedding_scope": {
            "rule": "form IN ('10-K','10-K/A') OR doc_source = 'financebench_pdf'",
            "not_embedded": {
                "rule": "doc_source = 'edgar_html' AND form IN ('10-Q','10-Q/A')",
                "count": 4,
                "reason": "serving is 10-K-only",
            },
        },
        "iceberg_snapshots": {"curated.facts": 1, "curated.chunks": 2},
        "files": {
            "companies": files("companies", companies),
            "facts": files("facts", facts),
            "chunks": files("chunks", chunks),
        },
        "filters": {"facts": "f", "chunks": "c"},
    }


def test_a_valid_payload_parses_and_exposes_expected_rows():
    """`expected_rows(table)` returns the headline count of each of the three tables."""
    m = Manifest.model_validate(payload())

    assert [m.expected_rows(t) for t in TABLES] == [m.companies_rows, m.facts_rows, m.chunks_rows]
    assert (m.facts_rows, m.chunks_rows, m.companies_rows) == (7, 5, 2)


def test_expected_rows_rejects_an_unknown_table():
    """Only companies, facts and chunks exist; anything else is a KeyError, not a silent 0."""
    m = Manifest.model_validate(payload())

    with pytest.raises(KeyError):
        m.expected_rows("filings")


@pytest.mark.parametrize("where", ["top", "files_entry", "artifact", "exclusions", "scope"])
def test_unknown_fields_are_rejected_everywhere(where: str):
    """extra='forbid' at every level: a misspelled or newer field fails loudly instead of being
    dropped, so the loader never reads a manifest it only half understands."""
    data = payload()
    target = {
        "top": data,
        "files_entry": data["files"]["facts"][0],
        "artifact": data["embedding_artifact"],
        "exclusions": data["chunk_exclusions"],
        "scope": data["embedding_scope"]["not_embedded"],
    }[where]
    target["surprise"] = 1

    with pytest.raises(ValidationError):
        Manifest.model_validate(data)


@pytest.mark.parametrize("table", ["facts", "chunks", "companies"])
@pytest.mark.parametrize("delta", [1, -1])
def test_a_headline_count_that_differs_from_the_file_sum_is_rejected(table: str, delta: int):
    """`<table>_rows` must equal the sum of that table's file rows, off by one either way."""
    data = payload()
    data[f"{table}_rows"] += delta

    with pytest.raises(ValidationError, match=f"{table}_rows"):
        Manifest.model_validate(data)


def test_a_nonzero_count_with_no_files_for_the_table_is_rejected():
    """A table missing from `files` sums to 0, so a nonzero headline count cannot pass."""
    data = payload()
    del data["files"]["chunks"]

    with pytest.raises(ValidationError, match="chunks_rows"):
        Manifest.model_validate(data)


@pytest.mark.parametrize("field", ["corpus_version", "created_at", "git_sha", "files", "filters"])
def test_required_fields_are_required(field: str):
    """Dropping any required field fails validation (no silent defaults for provenance)."""
    data = payload()
    del data[field]

    with pytest.raises(ValidationError):
        Manifest.model_validate(data)


def test_json_round_trip_is_lossless_and_has_the_contract_keys():
    """dump -> read_manifest gives an equal model; the JSON has exactly the section 2.3 keys,
    including the nested `files` entries (path, rows, bytes)."""
    m = Manifest.model_validate(payload())

    text = m.model_dump_json()

    assert read_manifest(text) == m
    data = json.loads(text)
    assert set(data) == set(payload())
    assert set(data["files"]["facts"][0]) == {"path", "rows", "bytes"}


def test_read_manifest_rejects_text_that_is_not_json():
    """A truncated file (an interrupted write) is a ValidationError, never a partial model."""
    text = Manifest.model_validate(payload()).model_dump_json()

    with pytest.raises(ValidationError):
        read_manifest(text[: len(text) // 2])


def test_manifest_name_is_the_contract_file_name():
    """The loader and the exporter agree on the file name."""
    assert MANIFEST_NAME == "_MANIFEST.json"
    assert TABLES == ("companies", "facts", "chunks")


def test_footer_rows_reads_the_row_count_from_the_parquet_footer(tmp_path: Path):
    """`footer_rows` returns num_rows from the footer of a real Parquet file (here written in
    two row groups, so a single group's count would be wrong)."""
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"x": list(range(10))}), path, row_group_size=4)

    assert footer_rows(path) == 10


@given(
    files=st.lists(st.integers(min_value=0, max_value=10_000), min_size=1, max_size=6),
    off_by=st.integers(min_value=-5, max_value=5),
)
def test_property_valid_iff_the_headline_equals_the_file_sum(files: list[int], off_by: int):
    """For any split of rows over files, the manifest validates exactly when the headline equals
    the sum of the file counts."""
    data = payload(facts=files)
    data["facts_rows"] = sum(files) + off_by

    if off_by == 0:
        assert Manifest.model_validate(data).facts_rows == sum(files)
    else:
        with pytest.raises(ValidationError):
            Manifest.model_validate(data)
