"""Check the FinanceBench draft's gold chunk ids against the served corpus (``chunks`` view).

The draft (``eval/sets/drafts/financebench_evidence_chunks.json``) was built from the local
warehouse. This asks the serving Postgres which of those ids exist in the live ``chunks`` view
(corpus_version from ``corpus_state``), and whether every evidence page of every row still has at
least one served chunk. Read-only; writes a JSON report.

    EDGARQA_SERVING_DSN=... uv run python -m eval.gen.check_fb_chunks \\
        --out docs/m4/fb_chunk_check.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

PAGE_RE = re.compile(r"^fb:(?P<doc>.+)#p(?P<page>\d+)#\d+$")


def page_key(chunk_id: str) -> tuple[str, int]:
    m = PAGE_RE.match(chunk_id)
    if not m:
        raise ValueError(f"not a FinanceBench chunk id: {chunk_id}")
    return m.group("doc"), int(m.group("page"))


def compare(
    gold: dict[str, list[str]], served: set[str], served_fb_ids: set[str] | None = None
) -> dict[str, Any]:
    """Pure comparison, unit-testable: missing gold ids, and rows with an evidence page that has
    no served chunk at all. ``served_fb_ids`` is every served FinanceBench chunk id, so a page
    whose gold chunk was re-chunked away but which still has sibling chunks counts as served
    (page hit@k still works); without it only the gold ids themselves are considered."""
    all_ids = sorted({cid for ids in gold.values() for cid in ids})
    missing = sorted(set(all_ids) - served)
    pool = served_fb_ids if served_fb_ids is not None else served
    served_pages = {page_key(c) for c in pool if PAGE_RE.match(c)}
    rows_without_page: dict[str, list[str]] = {}
    for row_id, ids in sorted(gold.items()):
        pages = sorted({page_key(c) for c in ids})
        lost = [f"{d}#p{p}" for d, p in pages if (d, p) not in served_pages]
        if lost:
            rows_without_page[row_id] = lost
    return {
        "gold_ids": len(all_ids),
        "served": len(all_ids) - len(missing),
        "missing": len(missing),
        "missing_ids": missing,
        "rows": len(gold),
        "rows_with_an_unserved_evidence_page": rows_without_page,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--gold", type=Path, default=Path("eval/sets/drafts/financebench_evidence_chunks.json")
    )
    ap.add_argument("--dsn", default=os.environ.get("EDGARQA_SERVING_DSN"))
    ap.add_argument("--out", type=Path, default=Path("docs/m4/fb_chunk_check.json"))
    args = ap.parse_args(argv)
    if not args.dsn:
        ap.error("set EDGARQA_SERVING_DSN or pass --dsn")
    gold: dict[str, list[str]] = json.loads(args.gold.read_text())["gold_chunk_ids"]
    import psycopg

    all_ids = sorted({cid for ids in gold.values() for cid in ids})
    # read-only at the session level (conn.read_only is a no-op under autocommit)
    with psycopg.connect(
        args.dsn, autocommit=True, options="-c default_transaction_read_only=on"
    ) as conn:
        served = {
            r[0]
            for r in conn.execute(
                "SELECT chunk_id FROM chunks WHERE chunk_id = ANY(%s)", [all_ids]
            ).fetchall()
        }
        state = conn.execute("SELECT live_version FROM corpus_state").fetchone()
        fb_ids = {
            r[0]
            for r in conn.execute(
                "SELECT chunk_id FROM chunks WHERE doc_source = 'financebench_pdf'"
            ).fetchall()
        }
    report = compare(gold, served, fb_ids)
    report["corpus_version"] = state[0] if state else None
    report["financebench_chunks_served"] = len(fb_ids)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"corpus_version {report['corpus_version']}: {report['served']}/{report['gold_ids']} "
        f"gold ids served, {report['missing']} missing, "
        f"{len(report['rows_with_an_unserved_evidence_page'])} rows lost an evidence page"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
