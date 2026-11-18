"""How many filings does the CURRENT parser miss a required item on? (read-only measurement)

    uv run python -m eval.blast_radius [--seed 20261118] [--n 150]
        [--cache data/fixture_candidates/blast]
        [--out docs/milestones/M02-blast-radius.json]

Samples S&P 500 10-Ks (not amendments) with a report date in FY2020-2025 from
`data/targets/filings.csv`, stratified by SIC division so utilities (SIC 49xx) and financials
(SIC 60xx-67xx), where multi-registrant co-filings cluster, are well represented. Each filing is
fetched through `SecClient` (cached gzipped under `--cache`), parsed with `parse_document`, and
analysed WITHOUT changing the parser:

* required items: which of 1, 1A, 7, 7A, 8 the parser did not find;
* cause of each miss: `heading_paragraph_not_chosen` (an unlinked paragraph that reads like the
  item's heading exists and the chain skipped it: a real chain defect), `toc_or_index_only` (the
  only heading-like text is a row of a table, or link text: a table of contents or a
  cross-reference index, so the BODY heading is not recognised at all), `mention_only` (the item is
  named only in prose or cross-references) and `no_mention` (the filing never writes "Item X");
* multi-registrant: more than one distinct `dei:EntityCentralIndexKey` on the cover page;
* combined headings such as "Items 1 and 2";
* `share_before_first_heading`: the share of the document's text that precedes the first detected
  heading and so is never chunked (about 2% for a complete filing: cover page and TOC).

The sample is seeded and the 50 filings of the earlier fixture survey are included first, so a
rerun after a parser change measures the same filings.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import statistics
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from selectolax.lexbor import LexborHTMLParser

from pipelines.fetch.fetch_filings import read_targets
from pipelines.fetch.list_filings import FilingTarget
from pipelines.fetch.sec_client import SecClient
from pipelines.parsing.document import FilingMeta, parse_document
from pipelines.parsing.html_blocks import html_to_blocks
from pipelines.parsing.sections import _candidates, detect_sections

REQUIRED = ("1", "1A", "7", "7A", "8")
CORE = ("1", "1A", "7", "8")
DEFAULT_OUT = Path("docs/milestones/M02-blast-radius.json")
DEFAULT_CACHE = Path("data/fixture_candidates/blast")
_COMBINED = re.compile(r"^\s*items?\s+\d+[a-c]?\s*(?:and|&|,)\s*\d", re.IGNORECASE)


def sector_group(sic: int | None) -> str:
    if sic is None:
        return "unknown"
    if 4900 <= sic < 5000:
        return "utilities"
    if 6000 <= sic < 6800:
        return "financials"
    return "other"


def registrants(raw: bytes) -> dict[str, list[str]]:
    """Distinct cover-page registrant CIKs and names (inline XBRL `dei:` facts)."""
    tree = LexborHTMLParser(raw.decode("utf-8", "replace"))
    ciks: list[str] = []
    names: list[str] = []
    for node in tree.css("ix\\:nonnumeric"):
        name = node.attributes.get("name")
        value = " ".join(node.text(strip=True).split())
        if name == "dei:EntityCentralIndexKey" and value and value not in ciks:
            ciks.append(value)
        elif name == "dei:EntityRegistrantName" and value and value not in names:
            names.append(value)
    return {"ciks": ciks, "names": names}


def analyse_filing(raw: bytes, meta: FilingMeta) -> dict[str, Any]:
    blocks = html_to_blocks(raw)
    sections = detect_sections(blocks, "10-K")
    found = [s.code for s in sections]
    total_chars = sum(len(b.text) for b in blocks)
    # The parser chunks only text after its first detected heading: the rest (cover, TOC, and any
    # section whose heading was missed before that point) is not indexed.
    before = (
        sum(len(b.text) for b in blocks[: sections[0].heading_index]) if sections else total_chars
    )

    all_candidates = _candidates(blocks, "10-K")
    candidates = Counter(c.code for c in all_candidates)
    paragraphs = Counter(
        c.code for c in all_candidates if blocks[c.block].kind == "paragraph" and not c.is_link
    )
    text = " ".join(b.text for b in blocks)
    missing = [c for c in REQUIRED if c not in found]
    causes = {}
    for code in missing:
        mentions = len(re.findall(rf"(?i)\bitem\s*{code}\b", text))
        if paragraphs[code]:
            causes[code] = "heading_paragraph_not_chosen"
        elif candidates[code]:
            causes[code] = "toc_or_index_only"
        elif mentions:
            causes[code] = "mention_only"
        else:
            causes[code] = "no_mention"
    filing, _ = parse_document(raw, meta)
    reg = registrants(raw)
    return {
        "parse_status": filing.parse_status,
        "text_chars": total_chars,
        "share_before_first_heading": before / total_chars if total_chars else None,
        "sections_found": found,
        "missing_required": missing,
        "missing_core": [c for c in missing if c in CORE],
        "miss_causes": causes,
        "candidates_per_missing": {c: candidates[c] for c in missing},
        "paragraph_candidates_per_missing": {c: paragraphs[c] for c in missing},
        "combined_heading": any(_COMBINED.match(b.text) for b in blocks if len(b.text) < 200),
        "registrant_ciks": reg["ciks"],
        "registrant_names": reg["names"],
        "multi_registrant": len(reg["ciks"]) > 1,
    }


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    failing = [r for r in rows if r["missing_required"]]
    by_item = Counter(c for r in rows for c in r["missing_required"])
    groups: dict[str, dict[str, int]] = {}
    for r in rows:
        g = groups.setdefault(r["group"], {"n": 0, "missing_required": 0, "multi_registrant": 0})
        g["n"] += 1
        g["missing_required"] += bool(r["missing_required"])
        g["multi_registrant"] += r["multi_registrant"]
    multi = [r for r in rows if r["multi_registrant"]]
    return {
        "n": len(rows),
        "n_missing_any_required": len(failing),
        "n_missing_any_core": sum(bool(r["missing_core"]) for r in rows),
        "missing_by_item": {c: by_item.get(c, 0) for c in REQUIRED},
        "n_multi_registrant": len(multi),
        "multi_registrant_that_miss": sum(bool(r["missing_required"]) for r in multi),
        "failing_that_are_multi_registrant": sum(r["multi_registrant"] for r in failing),
        "n_combined_heading": sum(r["combined_heading"] for r in rows),
        "by_group": dict(sorted(groups.items())),
        "by_cause": dict(Counter(v for r in failing for v in r["miss_causes"].values())),
        "median_share_before_first_heading_complete": _median(
            [
                r["share_before_first_heading"]
                for r in rows
                if not r["missing_required"] and r.get("share_before_first_heading") is not None
            ]
        ),
        "max_share_before_first_heading_complete": max(
            (
                r["share_before_first_heading"]
                for r in rows
                if not r["missing_required"] and r.get("share_before_first_heading") is not None
            ),
            default=None,
        ),
        "parse_status": dict(Counter(r["parse_status"] for r in rows)),
    }


def choose_sample(
    targets: list[FilingTarget],
    sic_of: dict[int, int | None],
    prior: list[str],
    n: int,
    seed: int,
    want: dict[str, int],
) -> list[FilingTarget]:
    """The prior filings first, then random distinct CIKs until each sector group reaches `want`."""
    pool = [
        t
        for t in targets
        if t.form == "10-K"
        and t.report_date
        and date(2020, 1, 1) <= t.report_date <= date(2025, 12, 31)
    ]
    by_acc = {t.accession: t for t in pool}
    chosen = [by_acc[a] for a in prior if a in by_acc]
    used = {t.cik for t in chosen}
    rng = random.Random(seed)
    by_cik: dict[int, list[FilingTarget]] = {}
    for t in pool:
        by_cik.setdefault(t.cik, []).append(t)
    for group in ("utilities", "financials"):
        have = sum(sector_group(sic_of.get(t.cik)) == group for t in chosen)
        ciks = sorted(c for c in by_cik if c not in used and sector_group(sic_of.get(c)) == group)
        rng.shuffle(ciks)
        for cik in ciks[: max(0, want.get(group, 0) - have)]:
            chosen.append(rng.choice(by_cik[cik]))
            used.add(cik)
    rest = sorted(c for c in by_cik if c not in used)
    rng.shuffle(rest)
    for cik in rest[: max(0, n - len(chosen))]:
        chosen.append(rng.choice(by_cik[cik]))
    return chosen


def load_sic(namespace: str = "lake.curated") -> dict[int, int | None]:
    from pipelines.common.spark_session import spark_session

    spark = spark_session("local", app_name="blast_radius_sic")
    try:
        return {
            r.cik: r.sic
            for r in spark.table(f"{namespace}.companies").select("cik", "sic").collect()
        }
    finally:
        spark.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure which required items the parser misses.")
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--n", type=int, default=150)
    parser.add_argument("--want-utilities", type=int, default=25)
    parser.add_argument("--want-financials", type=int, default=35)
    parser.add_argument(
        "--prior", type=Path, default=Path("docs/milestones/M02-fixture-survey.json")
    )
    parser.add_argument("--targets", type=Path, default=Path("data/targets/filings.csv"))
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    load_dotenv(Path.cwd() / ".env")
    prior = [f["accession"] for f in json.loads(args.prior.read_text())["filings"]]
    sic_of = load_sic()
    sample = choose_sample(
        read_targets(args.targets), sic_of, prior, args.n, args.seed,
        {"utilities": args.want_utilities, "financials": args.want_financials},
    )  # fmt: skip
    tickers = {
        int(r["cik_str"]): r["ticker"]
        for r in json.loads(Path("data/company_tickers_2026-11-17.json").read_text()).values()
    }
    args.cache.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    with SecClient() as client:
        for t in sample:
            path = args.cache / f"{tickers.get(t.cik, t.cik)}_{t.accession}.htm.gz"
            if path.is_file():
                raw = gzip.decompress(path.read_bytes())
            else:
                raw = client.get(t.url)
                path.write_bytes(gzip.compress(raw, mtime=0))
            meta = FilingMeta(
                accession=t.accession, cik=t.cik, form="10-K", filed=t.filed,
                period_of_report=t.report_date, primary_doc_url=t.url,
            )  # fmt: skip
            rows.append(
                {
                    "ticker": tickers.get(t.cik, "?"), "cik": t.cik, "sic": sic_of.get(t.cik),
                    "group": sector_group(sic_of.get(t.cik)),
                    "report_year": t.report_date.year if t.report_date else None,
                    "accession": t.accession, "bytes": len(raw), "url": t.url,
                    "sgml_wrapper": "<TYPE>" in raw[:600].decode("utf-8", "replace").upper(),
                    **analyse_filing(raw, meta),
                }
            )  # fmt: skip
            print(
                f"{len(rows):3d}/{len(sample)} {rows[-1]['ticker']:6} {t.accession} "
                f"{rows[-1]['missing_required']}",
                flush=True,
            )
    result = {
        "seed": args.seed,
        "note": "Read-only measurement of the current parser; required items are 1, 1A, 7, 7A, 8.",
        "summary": summarize(rows),
        "filings": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
