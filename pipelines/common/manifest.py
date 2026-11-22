"""The serving export manifest (`_MANIFEST.json`, contract section 2.3).

Written by `pipelines/spark/export_serving.py`, read by the loader
(`serving/load/load_snapshot.py`).
No Spark import: the loader runs on the serving box without it.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

MANIFEST_NAME = "_MANIFEST.json"
TABLES = ("companies", "facts", "chunks")


class ManifestFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str  # relative to the export prefix, e.g. "chunks/part-00000.parquet"
    rows: int
    bytes: int


class EmbeddingArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hf_repo: str
    hf_revision: str
    onnx_file: str
    onnx_sha256: str


class ChunkExclusions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: str
    count: int


class NotEmbedded(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: str
    count: int
    reason: str


class EmbeddingScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: str
    not_embedded: NotEmbedded


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corpus_version: int
    created_at: str
    git_sha: str
    facts_rows: int
    chunks_rows: int
    companies_rows: int
    embedding_model: str
    embedding_artifact: EmbeddingArtifact
    dim: int
    chunk_exclusions: ChunkExclusions
    embedding_scope: EmbeddingScope
    iceberg_snapshots: dict[str, int]
    files: dict[str, list[ManifestFile]]
    filters: dict[str, str]

    @model_validator(mode="after")
    def _counts_match_files(self) -> Manifest:
        """The headline row counts are the sums of the per-file footer counts."""
        for table, headline in (
            ("facts", self.facts_rows),
            ("chunks", self.chunks_rows),
            ("companies", self.companies_rows),
        ):
            total = sum(f.rows for f in self.files.get(table, []))
            if total != headline:
                raise ValueError(f"{table}_rows {headline} != sum of file rows {total}")
        return self

    def expected_rows(self, table: str) -> int:
        return {
            "facts": self.facts_rows,
            "chunks": self.chunks_rows,
            "companies": self.companies_rows,
        }[table]


def footer_rows(path: Path) -> int:
    """Row count from a local Parquet file's footer (no data is read)."""
    import pyarrow.parquet as pq  # lazy: the Spark export job does not need pyarrow

    return int(pq.ParquetFile(path).metadata.num_rows)


def read_manifest(text: str) -> Manifest:
    return Manifest.model_validate_json(text)
