"""us-gaap / dei labels and definitions for the catalog's tags, from the FSDS ``tag`` table.

FSDS ships the taxonomy's standard label (``tlabel``) and documentation (``doc``) for every tag
it uses, so the review packet needs no taxonomy download or web lookup. Reads the local
``raw.fsds_tag`` Iceberg table (current snapshot only) and keeps, per catalog tag, the row from
the newest taxonomy ``version`` (standard tags only: ``custom = 0``).

    uv run python -m eval.gen.fsds_tag_labels --out docs/m4/fsds_tag_labels.json

Output: ``{tag: {"label", "definition", "version", "datatype", "iord", "crdr"}}`` plus a
``_source`` entry (table, metadata file, snapshot id), the shape ``catalog_review
--taxonomy-labels`` reads.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import duckdb

from eval.gen.catalog_review import load_catalog
from eval.gen.iceberg_files import snapshot_files

DEFAULT_TABLE = Path("warehouse/raw/fsds_tag")
DEFAULT_CATALOG = Path("app/engine/plan/metric_catalog.yaml")


def catalog_tags(catalog_path: Path) -> list[str]:
    tags: set[str] = set()
    for m in load_catalog(catalog_path).values():
        tags.update(m.tags or [])
    return sorted(tags)


def build_labels(table_dir: Path, tags: list[str]) -> dict[str, Any]:
    snap = snapshot_files(table_dir)
    rows = (
        duckdb.connect()
        .execute(
            """
        SELECT tag, version, tlabel, doc, datatype, iord, crdr
        FROM read_parquet(?)
        WHERE custom = 0 AND tag IN (SELECT unnest(?::VARCHAR[]))
        QUALIFY row_number() OVER (PARTITION BY tag ORDER BY version DESC) = 1
        ORDER BY tag
        """,
            [snap.files, tags],
        )
        .fetchall()
    )
    out: dict[str, Any] = {
        r[0]: {
            "label": r[2] or "",
            "definition": r[3] or "",
            "version": r[1],
            "datatype": r[4],
            "iord": r[5],
            "crdr": r[6],
        }
        for r in rows
    }
    out["_source"] = {
        "table": str(table_dir),
        "metadata_file": snap.metadata_file,
        "snapshot_id": snap.snapshot_id,
        "missing_tags": sorted(set(tags) - set(out)),
    }
    return out


def athena_fill(labels: dict[str, Any], workgroup: str = "edgarqa") -> None:
    """Fill tags missing from the local table (older taxonomies, e.g. tags deprecated in 2018)
    from the lake's full ``raw.fsds_tag`` with one read-only Athena SELECT."""
    import time

    import boto3

    missing = labels["_source"]["missing_tags"]
    if not missing:
        return
    athena = boto3.client("athena")
    quoted = ",".join("'" + t.replace("'", "") + "'" for t in missing)
    qid = athena.start_query_execution(
        QueryString=(
            "SELECT tag, version, tlabel, doc, datatype, iord, crdr FROM raw.fsds_tag "
            f"WHERE custom = 0 AND tag IN ({quoted})"
        ),
        WorkGroup=workgroup,
    )["QueryExecutionId"]
    while True:
        status = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = status["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    if state != "SUCCEEDED":
        raise RuntimeError(f"Athena query {qid} {state}")
    pages = athena.get_paginator("get_query_results").paginate(QueryExecutionId=qid)
    rows = [row for page in pages for row in page["ResultSet"]["Rows"]][1:]  # [0] is the header
    best: dict[str, list[str]] = {}
    for row in rows:
        vals = [c.get("VarCharValue", "") for c in row["Data"]]
        if vals[0] not in best or vals[1] > best[vals[0]][1]:
            best[vals[0]] = vals
    for tag, v in best.items():
        labels[tag] = {
            "label": v[2],
            "definition": v[3],
            "version": v[1],
            "datatype": v[4],
            "iord": v[5],
            "crdr": v[6],
        }
    labels["_source"]["athena_filled"] = sorted(best)
    labels["_source"]["athena_query_id"] = qid
    labels["_source"]["athena_bytes_scanned"] = status["Statistics"]["DataScannedInBytes"]
    labels["_source"]["missing_tags"] = sorted(set(missing) - set(best))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--table", type=Path, default=DEFAULT_TABLE)
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    ap.add_argument("--out", type=Path, default=Path("docs/m4/fsds_tag_labels.json"))
    ap.add_argument(
        "--athena-fill",
        action="store_true",
        help="fill tags missing locally from the lake's raw.fsds_tag (one Athena SELECT)",
    )
    args = ap.parse_args(argv)
    labels = build_labels(args.table, catalog_tags(args.catalog))
    if args.athena_fill:
        athena_fill(labels)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(labels, indent=2, sort_keys=True) + "\n")
    missing = labels["_source"]["missing_tags"]
    print(f"wrote {args.out}: {len(labels) - 1} tags; missing from FSDS: {missing or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
