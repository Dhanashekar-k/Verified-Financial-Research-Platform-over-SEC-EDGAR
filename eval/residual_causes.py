"""Why do the still-`partial` 10-Qs miss P1-1 / P1-2? (read-only measurement, parser-level facts)

    uv run python -m eval.residual_causes --items items.jsonl --out residual.json [--workers 6]

`items.jsonl` has `{"accession", "cik", "form", "path", "missing"}` per line (`missing` is the
filing's `parse_error` list, e.g. `["P1-1", "P1-2"]`). For each filing the CURRENT detector's
candidate headings are listed with their weights (the characters up to the next candidate, as in
`detect_sections`). A missing code has a *body-like candidate* when some candidate coded with it is
followed by at least `BODY_CHARS` of text: a heading with a real section behind it, as opposed to a
contents-table row (weight about 0). Each filing falls into one cause, in this order:

* `no_body_heading`: no body-like candidate for ANY missing code. The body prints no such heading
  (JPMorgan, Colgate), or one the heading regex cannot read.
* `heading_lost_in_chain`: a body-like candidate exists for a missing code but the chain did not
  choose it (order conflict with another heading, or a split heading read as something else).

and, independently, `p1_1_missing_only` / `p1_2_missing_only` / `both` by which codes are missing.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from pipelines.parsing.html_blocks import html_to_blocks
from pipelines.parsing.sections import _candidates

BODY_CHARS = 3000


def classify(item: dict[str, Any]) -> dict[str, Any]:
    raw = Path(item["path"]).read_bytes()
    blocks = html_to_blocks(gzip.decompress(raw) if item["path"].endswith(".gz") else raw)
    cands = _candidates(blocks, "10-Q")
    cum = [0]
    for b in blocks:
        cum.append(cum[-1] + len(b.text))
    n = len(blocks)
    body_like: dict[str, int] = {}
    for i, c in enumerate(cands):
        nxt = cands[i + 1].block if i + 1 < len(cands) else n
        chars = max(0, cum[nxt] - cum[c.block + 1]) if nxt > c.block else 0
        if chars >= BODY_CHARS:
            body_like[c.code] = max(body_like.get(c.code, 0), chars)
    missing = list(item["missing"])
    has_body = [m for m in missing if m in body_like]
    out = {k: item[k] for k in ("accession", "cik", "form")}
    out["missing"] = missing
    out["which"] = "both" if len(missing) == 2 else f"{missing[0]}_only"
    out["n_candidates"] = len(cands)
    out["missing_with_body_like_candidate"] = has_body
    out["cause"] = "heading_lost_in_chain" if has_body else "no_body_heading"
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Residual P1-1/P1-2 misses by cause.")
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    items = [json.loads(ln) for ln in args.items.read_text().splitlines() if ln.strip()]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(classify, items, chunksize=8))
    by_cik: dict[int, Counter[str]] = {}
    for r in rows:
        by_cik.setdefault(r["cik"], Counter())[r["cause"]] += 1
    summary = {
        "n": len(rows),
        "by_cause": dict(Counter(r["cause"] for r in rows)),
        "by_which": dict(Counter(r["which"] for r in rows)),
        "by_cause_and_which": {
            f"{c}|{w}": n for (c, w), n in Counter((r["cause"], r["which"]) for r in rows).items()
        },
        "companies": len(by_cik),
        "companies_all_filings_no_body_heading": sum(
            1 for c in by_cik.values() if set(c) == {"no_body_heading"}
        ),
        "body_chars_threshold": BODY_CHARS,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=1, sort_keys=True) + "\n"
    )
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
