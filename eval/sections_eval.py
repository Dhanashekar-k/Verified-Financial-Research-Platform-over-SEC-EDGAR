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
matches and a heading mistaken for a TOC entry does not. Leading lines shorter than the labeling
harness's body-line threshold (sub-headings) are skipped on the parser side, as the harness skips
them when it proposes the gold sentence.

Where a filing comes from: the fetch store (`--store`, with its `_manifest.jsonl`), or else the
blast-radius cache (`--cache`, files `<ticker>_<accession>.htm.gz`, with their metadata in
`--blast`). The gold template's filings are a stratified sample of S&P 500 10-Ks kept in that cache,
so without the second source every one of them would be reported "filing not in the store".

The report prints accuracy per item and overall against the runbook's TARGET (90%), and says how
many labels it rests on against the runbook's 100: n=50 is a deliberate deviation (M02 report).
Every accuracy carries a 95% percentile bootstrap CI (10,000 resamples, fixed seed). Two are given
overall: one that resamples the labels, and a stricter one that resamples whole FILINGS, because
five labels come from each filing and are not independent.
The exit code is non-zero below the target only with `--min-accuracy`.

Without a gold file the result is `NOT MEASURED` (never an invented number) and the exit code is 0.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import sys
import unicodedata
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from eval.labeling_harness import MIN_BODY_LINE
from pipelines.fetch.fetch_filings import MANIFEST_KEY, FetchRecord
from pipelines.parsing.document import FilingMeta, parse_document

DEFAULT_GOLD = Path("eval/sets/sections_gold.jsonl")
DEFAULT_STORE = Path("data/raw/filings")
DEFAULT_OUT = Path("docs/milestones/M02-sections-eval.json")
DEFAULT_CACHE = Path("data/fixture_candidates/blast")
DEFAULT_BLAST = Path("docs/milestones/M02-blast-radius.json")
PREFIX_CHARS = 60
TARGET = 0.9  # M02 runbook section 6: boundary accuracy >= 90%
RUNBOOK_LABELS = 100  # the runbook asks for 100 labels; the owner chose n=50
RESAMPLES = 10_000  # .claude/rules/eval.md: 95% bootstrap CI, 10,000 resamples
BOOTSTRAP_SEED = 20261120
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_start(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).replace("\xa0", " ").lower()
    return " ".join(_PUNCT.sub("", folded).split())


def bootstrap_ci(
    outcomes: Sequence[int], resamples: int = RESAMPLES, seed: int = BOOTSTRAP_SEED
) -> tuple[float, float]:
    """95% percentile bootstrap CI of the mean of 0/1 `outcomes`, resampling the labels."""
    n = len(outcomes)
    if n == 0:
        raise ValueError("a bootstrap needs at least one label")
    rng = random.Random(seed)
    means = sorted(sum(outcomes[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    return means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]


def cluster_bootstrap_ci(
    groups: Sequence[Sequence[int]], resamples: int = RESAMPLES, seed: int = BOOTSTRAP_SEED
) -> tuple[float, float]:
    """The same CI, resampling whole groups (filings) so a filing's labels move together."""
    k = len(groups)
    if k == 0:
        raise ValueError("a bootstrap needs at least one group")
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        picked = [groups[rng.randrange(k)] for _ in range(k)]
        total = sum(len(g) for g in picked)
        means.append(sum(sum(g) for g in picked) / total)
    means.sort()
    return means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]


def skip_leading_headings(text: str) -> str:
    """Drop the short lines (under `MIN_BODY_LINE` characters) a chunk opens with.

    The labeling harness proposes `first_sentence` from the first body line of at least
    `MIN_BODY_LINE` characters, so it skips sub-headings such as "Company Background" or
    "Overview". The parser's first chunk keeps them (`Company Background\nThe Company designs ...`).
    Comparing like with like keeps a correctly placed boundary from scoring as wrong. A chunk of
    short lines only is left as it is.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if len(line) >= MIN_BODY_LINE:
            return "\n".join(lines[i:])
    return text


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


def _blast_sources(cache: Path | None, blast: Path | None) -> dict[str, tuple[Path, FilingMeta]]:
    """accession -> (cached file, metadata) for the blast-radius sample of 10-Ks.

    Only what section detection needs is real (cik, url); the filing date is a placeholder because
    this module compares section TEXT, which does not depend on it.
    """
    if cache is None or blast is None or not blast.is_file():
        return {}
    out: dict[str, tuple[Path, FilingMeta]] = {}
    for f in json.loads(blast.read_text(encoding="utf-8"))["filings"]:
        path = cache / f"{f['ticker']}_{f['accession']}.htm.gz"
        if path.is_file():
            out[f["accession"]] = (
                path,
                FilingMeta(
                    accession=f["accession"], cik=int(f["cik"]), form="10-K", filed=date.min,
                    period_of_report=None, primary_doc_url=f["url"],
                ),
            )  # fmt: skip
    return out


def _store_sources(store: Path) -> dict[str, tuple[Path, FilingMeta]]:
    out: dict[str, tuple[Path, FilingMeta]] = {}
    for acc, r in _manifest(store).items():
        path = store / str(r.cik) / f"{acc}.htm.gz"
        if path.is_file():
            out[acc] = (
                path,
                FilingMeta(
                    accession=acc, cik=r.cik, form=r.form, filed=r.filed,
                    period_of_report=r.report_date or date.min, primary_doc_url=r.url,
                ),
            )  # fmt: skip
    return out


def parser_starts(path: Path, meta: FilingMeta) -> dict[str, str]:
    """section code -> text of its first chunk, parsed from the stored HTML."""
    _, chunks = parse_document(gzip.decompress(path.read_bytes()), meta)
    return {c.section: c.text for c in chunks if c.chunk_index == 0}


def evaluate(
    gold: list[dict[str, str]],
    store: Path,
    prefix_chars: int = PREFIX_CHARS,
    cache_dir: Path | None = None,
    blast: Path | None = None,
) -> dict[str, Any]:
    sources = _blast_sources(cache_dir, blast) | _store_sources(store)  # the fetch store wins
    cache: dict[str, dict[str, str]] = {}
    by_section: dict[str, dict[str, int]] = {}
    failures: list[dict[str, str]] = []
    outcomes: list[tuple[str, str, int]] = []  # (accession, section, 1 if correct)
    correct = 0
    for row in gold:
        acc, section = row["accession"], row["section"]
        stat = by_section.setdefault(section, {"n": 0, "correct": 0})
        stat["n"] += 1
        source = sources.get(acc)
        reason, found = None, ""
        if source is None:
            reason = "filing not in the store"
        else:
            starts = cache.setdefault(acc, parser_starts(*source))
            if section not in starts:
                reason = "section not found"
            else:
                found = starts[section]
                want = normalize_start(row["first_sentence"])[:prefix_chars]
                if not normalize_start(skip_leading_headings(found)).startswith(want):
                    reason = "start differs"
        outcomes.append((acc, section, int(reason is None)))
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
    by_filing: dict[str, list[int]] = {}
    by_item: dict[str, list[int]] = {}
    for acc, section, ok in outcomes:
        by_filing.setdefault(acc, []).append(ok)
        by_item.setdefault(section, []).append(ok)
    return {
        "n": n,
        "n_filings": len(by_filing),
        "correct": correct,
        "accuracy": correct / n if n else None,
        "ci95": bootstrap_ci([o for _, _, o in outcomes]) if n else None,
        "ci95_by_filing": cluster_bootstrap_ci(list(by_filing.values())) if n else None,
        "ci95_by_section": {k: bootstrap_ci(v) for k, v in sorted(by_item.items())},
        "resamples": RESAMPLES,
        "target": TARGET,
        "meets_target": bool(n) and correct / n >= TARGET,
        "runbook_labels": RUNBOOK_LABELS,
        "prefix_chars": prefix_chars,
        "by_section": dict(sorted(by_section.items())),
        "accuracy_by_section": {k: v["correct"] / v["n"] for k, v in sorted(by_section.items())},
        "failures": failures,
    }


def render(result: dict[str, Any]) -> str:
    """The console report: accuracy per item and overall against the target, with n and 95% CIs."""
    verdict = lambda ok: "PASS" if ok else "FAIL"  # noqa: E731
    pct = lambda x: f"{100 * x:.1f}%"  # noqa: E731
    target = result["target"]
    lo, hi = result["ci95"]
    flo, fhi = result["ci95_by_filing"]
    lines = [
        f"section-boundary accuracy: {result['correct']}/{result['n']} = {pct(result['accuracy'])} "
        f"(target >= {100 * target:.0f}%: {verdict(result['meets_target'])})",
        f"  95% bootstrap CI ({result['resamples']:,} resamples): labels {pct(lo)} to {pct(hi)}; "
        f"filings {pct(flo)} to {pct(fhi)} ({result['n_filings']} filings, 5 labels each are not "
        "independent: read the second)",
        f"  n = {result['n']} labels; the runbook asks for {result['runbook_labels']}. "
        "A deliberate deviation: judge the result as n=50.",
    ]
    if flo < target:
        lines.append(
            f"  The lower CI bound ({pct(flo)}) is under the target: this sample cannot show "
            f">= {100 * target:.0f}% with confidence, whatever the point estimate says."
        )
    for section, stat in result["by_section"].items():
        acc = result["accuracy_by_section"][section]
        slo, shi = result["ci95_by_section"][section]
        lines.append(
            f"  item {section:>3}: {stat['correct']:>2}/{stat['n']:<2} = {100 * acc:5.1f}%  "
            f"[{100 * slo:5.1f}, {100 * shi:5.1f}]  {verdict(acc >= target)}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Section-boundary accuracy against gold labels.")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--blast", type=Path, default=DEFAULT_BLAST)
    parser.add_argument("--prefix-chars", type=int, default=PREFIX_CHARS)
    parser.add_argument("--min-accuracy", type=float, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    if not args.gold.is_file():
        print(f"section-boundary accuracy: NOT MEASURED ({args.gold} does not exist yet)")
        return 0
    result = evaluate(load_gold(args.gold), args.store, args.prefix_chars, args.cache, args.blast)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(render(result))
    for f in result["failures"]:
        print(f"  FAIL {f['accession']} {f['section']}: {f['reason']}")
        print(f"       gold:   {f['gold_start']!r}\n       parser: {f['parser_start']!r}")
    if args.min_accuracy is not None and (result["accuracy"] or 0) < args.min_accuracy:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
