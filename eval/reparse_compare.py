"""Compare `curated.filings` before and after a re-parse, filing by filing (read-only).

    uv run python -m eval.reparse_compare --before before.json.gz --after after.json.gz
        [--out docs/milestones/M02-adr031-reparse.json]

Both inputs are lists of `{accession, cik, form, doc_source, parse_status, parse_error, n_sections,
n_chunks}` (an Athena dump of `curated.filings`, plain or gzipped JSON). The gate of a parser
change is read straight off the result: `previously_ok_not_ok` and `previously_ok_changed` must be
0. Also reported: the status transitions, strict / lenient parse success of the HTML filings, the
10-Qs missing both Part I items before and after, and what happened to the companies that had any.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

RANK = {"error": -1, "no_sections": 0, "partial": 1, "ok": 2}


def load(path: Path) -> dict[str, dict[str, Any]]:
    raw = path.read_bytes()
    rows = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
    return {r["accession"]: r for r in rows}


def _both_missing(r: dict[str, Any]) -> bool:
    err = r.get("parse_error") or ""
    return r["parse_status"] == "partial" and "P1-1" in err and "P1-2" in err


def compare(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> dict[str, Any]:
    keys = sorted(after)
    html = [k for k in keys if after[k]["doc_source"] == "edgar_html"]
    ok = sum(after[k]["parse_status"] == "ok" for k in html)
    partial = sum(after[k]["parse_status"] == "partial" for k in html)
    before_ok = sum(before[k]["parse_status"] == "ok" for k in html)

    def shape(r: dict[str, Any]) -> tuple[Any, ...]:
        return (r["parse_status"], r["n_sections"], r["n_chunks"])

    ok_before = [k for k in keys if before[k]["parse_status"] == "ok"]
    q = [k for k in html if after[k]["form"] in ("10-Q", "10-Q/A")]
    affected = [k for k in q if _both_missing(before[k])]
    by_company: dict[str, list[str]] = defaultdict(list)
    for k in affected:
        by_company[after[k]["cik"]].append(k)
    all_q: dict[str, list[str]] = defaultdict(list)
    for k in q:
        all_q[after[k]["cik"]].append(k)
    return {
        "same_accession_set": set(before) == set(after),
        "n_filings_html": len(html),
        "html": {
            "ok_before": before_ok,
            "ok_after": ok,
            "partial_after": partial,
            "no_sections_after": sum(after[k]["parse_status"] == "no_sections" for k in html),
            "strict_before": before_ok / len(html),
            "strict_after": ok / len(html),
            "lenient_after_ok_plus_partial": (ok + partial) / len(html),
        },
        "transitions": {
            f"{a}->{b}": n
            for (a, b), n in sorted(
                Counter((before[k]["parse_status"], after[k]["parse_status"]) for k in keys).items()
            )
        },
        "gate": {
            "status_downgrades": sum(
                RANK[after[k]["parse_status"]] < RANK[before[k]["parse_status"]] for k in keys
            ),
            "previously_ok_not_ok": sum(after[k]["parse_status"] != "ok" for k in ok_before),
            "previously_ok_changed": sum(shape(after[k]) != shape(before[k]) for k in ok_before),
            "form_10k_rows_changed": sum(
                shape(after[k]) != shape(before[k])
                for k in keys
                if after[k]["form"].startswith("10-K")
            ),
            "financebench_rows_changed": sum(
                shape(after[k]) != shape(before[k])
                for k in keys
                if after[k]["doc_source"] == "financebench_pdf"
            ),
        },
        "by_form_after": {
            f: {
                s: sum(1 for k in html if after[k]["form"] == f and after[k]["parse_status"] == s)
                for s in ("ok", "partial", "no_sections")
            }
            for f in ("10-K", "10-K/A", "10-Q", "10-Q/A")
        },
        "ten_q_missing_both_p1": {
            "before": len(affected),
            "after": sum(_both_missing(after[k]) for k in q),
            "recovered_to_ok": sum(after[k]["parse_status"] == "ok" for k in affected),
        },
        "ten_q_partial": {
            "before": sum(before[k]["parse_status"] == "partial" for k in q),
            "after": sum(after[k]["parse_status"] == "partial" for k in q),
        },
        "companies_with_both_missing_before": {
            "n": len(by_company),
            "with_at_least_one_filing_now_ok": sum(
                any(after[k]["parse_status"] == "ok" for k in v) for v in by_company.values()
            ),
            "with_every_affected_filing_now_ok": sum(
                all(after[k]["parse_status"] == "ok" for k in v) for v in by_company.values()
            ),
            "with_every_10q_ok_after": sum(
                all(after[k]["parse_status"] == "ok" for k in all_q[c]) for c in by_company
            ),
            "still_with_a_filing_missing_both": sum(
                any(_both_missing(after[k]) for k in v) for v in by_company.values()
            ),
        },
        "residual_10q_partial_by_error": dict(
            Counter(after[k]["parse_error"] for k in q if after[k]["parse_status"] == "partial")
        ),
        "residual_10q_partial_companies": len(
            {after[k]["cik"] for k in q if after[k]["parse_status"] == "partial"}
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="curated.filings before vs after a re-parse.")
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("docs/milestones/M02-adr031-reparse.json"))
    args = parser.parse_args(argv)
    result = compare(load(args.before), load(args.after))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("html", "gate", "ten_q_missing_both_p1")}, indent=1))
    return 0 if result["gate"]["previously_ok_not_ok"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
