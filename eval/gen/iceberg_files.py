"""The data files of an Iceberg table's CURRENT snapshot (local warehouse, read-only).

Globbing ``<table>/data/**/*.parquet`` also picks up files that older snapshots wrote and the
current one no longer references (the local chunks table had 5 such files holding 38,922
duplicate rows). Reading the snapshot's file list through pyiceberg avoids that.

The local Spark warehouse uses the Hadoop layout: ``metadata/version-hint.text`` names
``v<N>.metadata.json``. Without a hint, the newest ``*.metadata.json`` by last-updated time
is used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pyiceberg.table import StaticTable


@dataclass(frozen=True)
class SnapshotFiles:
    files: list[str]
    snapshot_id: int | None
    metadata_file: str


def current_metadata_file(table_dir: Path) -> Path:
    meta = table_dir / "metadata"
    hint = meta / "version-hint.text"
    if hint.exists():
        path = meta / f"v{hint.read_text().strip()}.metadata.json"
        if not path.exists():
            raise FileNotFoundError(f"version-hint points at missing {path}")
        return path
    candidates = sorted(meta.glob("*.metadata.json"))
    if not candidates:
        raise FileNotFoundError(f"no Iceberg metadata under {meta}")
    return max(candidates, key=lambda p: int(json.loads(p.read_text())["last-updated-ms"]))


def snapshot_files(table_dir: Path) -> SnapshotFiles:
    """Absolute local paths of the current snapshot's data files (no delete files expected)."""
    metadata = current_metadata_file(table_dir)
    table = StaticTable.from_metadata(str(metadata.resolve()))
    files = sorted(
        task.file.file_path.removeprefix("file://") for task in table.scan().plan_files()
    )
    snapshot = table.current_snapshot()
    return SnapshotFiles(
        files=files,
        snapshot_id=None if snapshot is None else snapshot.snapshot_id,
        metadata_file=metadata.name,
    )
