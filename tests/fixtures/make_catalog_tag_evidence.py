"""Write tests/fixtures/catalog_tags_in_fsds.json: do the catalog's tags exist in raw.fsds_tag?

Run from anywhere:  uv run python tests/fixtures/make_catalog_tag_evidence.py

Reads the LOCAL Iceberg table ``warehouse/raw/fsds_tag`` (pyiceberg ``StaticTable`` on the newest
``vN.metadata.json``; read-only, no catalog, no network) and, for every tag the metric catalog
uses (the tags of every tag metric, which are also the leaf tags of every formula metric), records
whether the tag exists, which ``version`` strings (taxonomy + year, e.g. ``us-gaap/2023``) it was
seen under, which taxonomies, and the ``custom`` flag values seen (1 = company extension).

The output is committed evidence: ``tests/unit/engine/test_metric_catalog.py`` fails when a
catalog tag is missing from it or exists in it as a custom-only tag (an invented tag), and when
the tag set drifts from the catalog (regenerate after editing the catalog).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
TABLE_DIR = REPO / "warehouse" / "raw" / "fsds_tag"
OUT = Path(__file__).resolve().parent / "catalog_tags_in_fsds.json"


def latest_metadata(table_dir: Path) -> Path:
    files = list((table_dir / "metadata").glob("v*.metadata.json"))
    if not files:
        raise FileNotFoundError(f"no vN.metadata.json under {table_dir}/metadata")
    return max(files, key=lambda p: int(re.match(r"v(\d+)\.", p.name).group(1)))  # type: ignore[union-attr]


def catalog_tags() -> list[str]:
    sys.path.insert(0, str(REPO))
    from app.engine.plan.catalog import load_catalog

    tags: set[str] = set()
    for metric in load_catalog().values():
        tags.update(metric.tags or [])
    return sorted(tags)


def build_evidence() -> dict[str, Any]:
    from pyiceberg.table import StaticTable

    os.chdir(REPO)  # the table's recorded location is relative to the repo root
    meta = latest_metadata(TABLE_DIR)
    table = StaticTable.from_metadata(str(meta.relative_to(REPO)))
    arrow = table.scan(selected_fields=("tag", "version", "custom")).to_arrow()
    wanted = set(catalog_tags())
    seen: dict[str, dict[str, set[Any]]] = {t: {"versions": set(), "custom": set()} for t in wanted}
    tags = arrow.column("tag").to_pylist()
    versions = arrow.column("version").to_pylist()
    customs = arrow.column("custom").to_pylist()
    for tag, version, custom in zip(tags, versions, customs, strict=True):
        if tag in seen:
            seen[tag]["versions"].add(version)
            seen[tag]["custom"].add(custom)
    out: dict[str, Any] = {}
    for tag in sorted(wanted):
        info = seen[tag]
        vers = sorted(v for v in info["versions"] if v is not None)
        out[tag] = {
            "exists": bool(vers or info["custom"]),
            "versions": vers,
            "taxonomies": sorted({v.split("/")[0] for v in vers}),
            "custom_values": sorted(c for c in info["custom"] if c is not None),
            "custom": 1 in info["custom"],
        }
    return {
        "source": "warehouse/raw/fsds_tag",
        "metadata_file": meta.name,
        "rows_scanned": arrow.num_rows,
        "tags": out,
    }


def main() -> None:
    evidence = build_evidence()
    OUT.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    missing = [t for t, e in evidence["tags"].items() if not e["exists"]]
    print(f"wrote {OUT} ({len(evidence['tags'])} tags, {len(missing)} missing: {missing})")


if __name__ == "__main__":
    main()
