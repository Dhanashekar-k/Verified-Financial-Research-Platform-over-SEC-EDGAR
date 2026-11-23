"""FinanceBench set (M04 3.2): the 150 open-source questions as section 6 rows, in drafts only.

    uv run python -m eval.gen.make_financebench_set

Reads ``data/raw/financebench/financebench_open_source.jsonl`` and the LOCAL Iceberg warehouse
(``warehouse/curated/chunks``), only the data files of the table's current Iceberg snapshot
(``eval.gen.iceberg_files``; stale files from older snapshots are ignored). Writes:

- ``eval/sets/drafts/financebench.jsonl``: one ``FinanceBenchRow`` per question. The id is
  ``fb-<5-digit number from financebench_id>``; each evidence page is FinanceBench's 0-based
  ``evidence_page_num`` + 1, the 1-based page that ``curated.chunks.page`` uses (05 1.6).
- ``eval/sets/drafts/financebench_evidence_chunks.json``: row id -> every chunk id on that
  row's evidence pages (the gold for evidence-page hit@k: a hit when any retrieved chunk is
  on a gold page), plus a summary.

Shekar promotes the draft to ``eval/sets/`` (the gold path is hook-protected). The chunk ids
come from the local warehouse; the served corpus (corpus_version=1 on the serving box) must be
cross-checked against them the next time the box is up. The script exits non-zero if any
document has no chunks; evidence pages without chunks are listed but do not fail the run.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import duckdb

from eval.gen.iceberg_files import SnapshotFiles, snapshot_files
from eval.schemas import Evidence, FinanceBenchRow

SOURCE = Path("data/raw/financebench/financebench_open_source.jsonl")
WAREHOUSE_CHUNKS = Path("warehouse/curated/chunks")
OUT_DIR = Path("eval/sets/drafts")
OUT_ROWS = "financebench.jsonl"
OUT_CHUNKS = "financebench_evidence_chunks.json"
CAVEAT = (
    "chunk ids read from the local Iceberg warehouse; cross-check against the served "
    "corpus_version=1 (serving box) before using hit@k numbers from it"
)

_ID_RE = re.compile(r"(\d+)$")


class MissingDocumentsError(RuntimeError):
    """A FinanceBench document has no chunks in the warehouse."""


def parse_evidence(raw: Any) -> list[dict[str, Any]]:
    """Evidence arrives as a list, a JSON string or a Python-literal string."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = ast.literal_eval(raw)
        if isinstance(value, list):
            return value
    raise ValueError(f"evidence is not a list: {str(raw)[:80]!r}")


def row_id(financebench_id: str) -> str:
    match = _ID_RE.search(financebench_id)
    if not match:
        raise ValueError(f"no number in financebench_id {financebench_id!r}")
    return f"fb-{int(match.group(1)):05d}"


def convert(record: dict[str, Any]) -> FinanceBenchRow:
    evidence = [
        Evidence(page=int(item["evidence_page_num"]) + 1, text=str(item["evidence_text"]))
        for item in parse_evidence(record["evidence"])
    ]
    return FinanceBenchRow(
        id=row_id(str(record["financebench_id"])),
        question=str(record["question"]),
        gold_answer=str(record["answer"]),
        company=str(record["company"]),
        doc_name=str(record["doc_name"]),
        evidence=evidence,
        question_type=str(record["question_type"]),
        reasoning=str(record.get("question_reasoning") or ""),
    )


def load_chunk_pages(
    warehouse_chunks: Path, snapshot: SnapshotFiles | None = None
) -> dict[str, dict[int, list[str]]]:
    """doc_name -> page -> sorted chunk ids, for doc_source = 'financebench_pdf'."""
    snap = snapshot or snapshot_files(warehouse_chunks)
    if not snap.files:
        return {}
    rel = duckdb.sql(
        "SELECT chunk_id, page FROM read_parquet(?) WHERE doc_source = 'financebench_pdf'",
        params=[snap.files],
    )
    if rel is None:
        raise RuntimeError("duckdb returned no relation")
    rows = rel.fetchall()
    ids = [str(r[0]) for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate chunk ids in the current snapshot's financebench chunks")
    pages: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    for chunk_id, page in rows:
        doc = str(chunk_id).removeprefix("fb:").split("#", 1)[0]
        pages[doc][int(page)].append(str(chunk_id))
    return {d: {p: sorted(ids) for p, ids in by_page.items()} for d, by_page in pages.items()}


def build(
    records: list[dict[str, Any]], pages: dict[str, dict[int, list[str]]]
) -> tuple[list[FinanceBenchRow], dict[str, list[str]], dict[str, Any]]:
    rows = [convert(r) for r in records]
    ids = [r.id for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate row ids after conversion")
    missing_docs = sorted({r.doc_name for r in rows if r.doc_name not in pages})
    gold: dict[str, list[str]] = {}
    empty_pages: list[dict[str, Any]] = []
    for row in rows:
        chunk_ids: list[str] = []
        for ev in row.evidence:
            found = pages.get(row.doc_name, {}).get(ev.page, [])
            if not found:
                empty_pages.append({"id": row.id, "doc_name": row.doc_name, "page": ev.page})
            chunk_ids.extend(found)
        gold[row.id] = sorted(set(chunk_ids))
    summary = {
        "rows": len(rows),
        "documents": len({r.doc_name for r in rows}),
        "evidence_items": sum(len(r.evidence) for r in rows),
        "rows_without_gold_chunks": sorted(k for k, v in gold.items() if not v),
        "evidence_pages_without_chunks": empty_pages,
        "documents_without_chunks": missing_docs,
        "gold_chunk_ids_total": sum(len(v) for v in gold.values()),
    }
    return rows, gold, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--warehouse-chunks", type=Path, default=WAREHOUSE_CHUNKS)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    if args.out_dir.resolve() == Path("eval/sets").resolve():
        raise SystemExit("refusing to write gold: output must be eval/sets/drafts/")
    records = [json.loads(line) for line in args.source.read_text().splitlines() if line.strip()]
    snap = snapshot_files(args.warehouse_chunks)
    pages = load_chunk_pages(args.warehouse_chunks, snap)
    rows, gold, summary = build(records, pages)
    summary["source"] = {
        "questions": str(args.source),
        "warehouse_chunks": str(args.warehouse_chunks),
        "iceberg_metadata": snap.metadata_file,
        "iceberg_snapshot_id": snap.snapshot_id,
        "data_files": len(snap.files),
        "caveat": CAVEAT,
    }
    if summary["documents_without_chunks"]:
        print(json.dumps(summary, indent=2), file=sys.stderr)
        raise MissingDocumentsError(f"no chunks for {summary['documents_without_chunks']}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / OUT_ROWS).write_text(
        "".join(r.model_dump_json() + "\n" for r in sorted(rows, key=lambda r: r.id))
    )
    (args.out_dir / OUT_CHUNKS).write_text(
        json.dumps({"summary": summary, "gold_chunk_ids": gold}, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
