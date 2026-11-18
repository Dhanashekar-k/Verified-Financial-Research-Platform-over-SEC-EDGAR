"""Section-boundary accuracy against the owner's gold labels (M02 section 3.5).

    uv run python -m eval.sections_eval [--gold eval/sets/sections_gold.jsonl]
        [--store data/raw/filings] [--min-accuracy 0.9]
        [--out docs/milestones/M02-sections-eval.json]

Gold rows (the owner writes them; the hook protects the file once written):
    {"accession": "0000320193-23-000106", "section": "1A", "first_sentence": "...",
     "labeled_by": "..."}
`section` is the code the parser uses (10-K: 1, 1A, 7, 7A, 8; 10-Q: P1-2, P2-1A, ...). The
parser's answer is the start of the section's first chunk, built by the same `parse_document` the
Spark job calls, from the stored HTML. A section counts as correct when the first
`--prefix-chars` normalised characters of the gold sentence equal the start of that chunk
(lower case, no punctuation or quotes, one space between words), so a long gold sentence still
matches and a heading mistaken for a TOC entry does not.

Without a gold file the result is `NOT MEASURED` (never an invented number) and the exit code is 0.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from pipelines.fetch.fetch_filings import MANIFEST_KEY, FetchRecord
from pipelines.parsing.document import FilingMeta, parse_document

DEFAULT_GOLD = Path("eval/sets/sections_gold.jsonl")
DEFAULT_STORE = Path("data/raw/filings")
DEFAULT_OUT = Path("docs/milestones/M02-sections-eval.json")
PREFIX_CHARS = 60
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_start(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).replace("\xa0", " ").lower()
    return " ".join(_PUNCT.sub("", folded).split())


def load_gold(path: Path) -> list[dict[str, str]]:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        missing = {"accession", "section", "first_sentence"} - row.keys()
        if missing:
            raise ValueError(f"{path} line {n}: missing {sorted(missing)}")
        if not normalize_start(str(row["first_sentence"])):
            # an empty label would normalise to "" and match EVERY parser start
            raise ValueError(f"{path} line {n}: first_sentence is blank")
        rows.append(row)
    return rows


def _manifest(store: Path) -> dict[str, FetchRecord]:
    path = store / MANIFEST_KEY
    if not path.is_file():
        return {}
    records = (FetchRecord.model_validate_json(ln) for ln in path.read_text().splitlines() if ln)
    return {r.accession: r for r in records}


def parser_starts(store: Path, record: FetchRecord) -> dict[str, str]:
    """section code -> text of its first chunk, parsed from the stored HTML."""
    raw = gzip.decompress((store / str(record.cik) / f"{record.accession}.htm.gz").read_bytes())
    meta = FilingMeta(
        accession=record.accession, cik=record.cik, form=record.form, filed=record.filed,
        period_of_report=record.report_date or date.min, primary_doc_url=record.url,
    )  # fmt: skip
    _, chunks = parse_document(raw, meta)
    return {c.section: c.text for c in chunks if c.chunk_index == 0}


def evaluate(
    gold: list[dict[str, str]], store: Path, prefix_chars: int = PREFIX_CHARS
) -> dict[str, Any]:
    manifest = _manifest(store)
    cache: dict[str, dict[str, str]] = {}
    by_section: dict[str, dict[str, int]] = {}
    failures: list[dict[str, str]] = []
    correct = 0
    for row in gold:
        acc, section = row["accession"], row["section"]
        stat = by_section.setdefault(section, {"n": 0, "correct": 0})
        stat["n"] += 1
        record = manifest.get(acc)
        reason, found = None, ""
        if record is None or not (store / str(record.cik) / f"{acc}.htm.gz").is_file():
            reason = "filing not in the store"
        else:
            starts = cache.setdefault(acc, parser_starts(store, record))
            if section not in starts:
                reason = "section not found"
            else:
                found = starts[section]
                want = normalize_start(row["first_sentence"])[:prefix_chars]
                if not normalize_start(found).startswith(want):
                    reason = "start differs"
        if reason is None:
            correct += 1
            stat["correct"] += 1
        else:
            failures.append(
                {
                    "accession": acc,
                    "section": section,
                    "reason": reason,
                    "gold_start": row["first_sentence"][:120],
                    "parser_start": found[:120],
                }
            )
    n = len(gold)
    return {
        "n": n,
        "correct": correct,
        "accuracy": correct / n if n else None,
        "prefix_chars": prefix_chars,
        "by_section": dict(sorted(by_section.items())),
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Section-boundary accuracy against gold labels.")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--prefix-chars", type=int, default=PREFIX_CHARS)
    parser.add_argument("--min-accuracy", type=float, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    if not args.gold.is_file():
        print(f"section-boundary accuracy: NOT MEASURED ({args.gold} does not exist yet)")
        return 0
    result = evaluate(load_gold(args.gold), args.store, args.prefix_chars)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"{result['correct']}/{result['n']} section starts correct")
    for section, stat in result["by_section"].items():
        print(f"  {section:>6}: {stat['correct']}/{stat['n']}")
    for f in result["failures"]:
        print(f"  FAIL {f['accession']} {f['section']}: {f['reason']}")
        print(f"       gold:   {f['gold_start']!r}\n       parser: {f['parser_start']!r}")
    if args.min_accuracy is not None and (result["accuracy"] or 0) < args.min_accuracy:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
