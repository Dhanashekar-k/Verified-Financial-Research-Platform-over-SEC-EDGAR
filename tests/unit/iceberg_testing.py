"""A tiny real Iceberg chunks table in tmp_path (pyiceberg + SQLite catalog, no network).

The table is given the local Spark warehouse's Hadoop layout (``metadata/v<N>.metadata.json``
plus ``version-hint.text``), and ``add_stale_file`` drops a parquet file into ``data/`` that
no snapshot references, like the files older snapshots leave behind.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.catalog.sql import SqlCatalog

SCHEMA = pa.schema(
    [
        ("chunk_id", pa.string()),
        ("page", pa.int32()),
        ("doc_source", pa.string()),
        ("text", pa.string()),
        ("n_tokens", pa.int32()),
    ]
)


def _table(rows: list[dict[str, Any]]) -> pa.Table:
    cols = {name: [r.get(name) for r in rows] for name in SCHEMA.names}
    return pa.table(cols, schema=SCHEMA)


def make_chunks_table(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    catalog = SqlCatalog(
        "test", uri=f"sqlite:///{tmp_path / 'catalog.db'}", warehouse=f"file://{tmp_path / 'wh'}"
    )
    catalog.create_namespace("curated")
    table = catalog.create_table("curated.chunks", schema=SCHEMA)
    table.append(_table(rows))
    table_dir = Path(table.location().removeprefix("file://"))
    current = Path(table.metadata_location.removeprefix("file://"))
    shutil.copy(current, table_dir / "metadata" / "v9.metadata.json")
    (table_dir / "metadata" / "version-hint.text").write_text("9")
    return table_dir


def add_stale_file(table_dir: Path, rows: list[dict[str, Any]]) -> Path:
    path = table_dir / "data" / "stale-from-an-older-snapshot.parquet"
    pq.write_table(_table(rows), path)
    return path
