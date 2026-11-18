"""Resolve FinanceBench companies to CIKs, accepting only what the SEC's own data verifies.

    uv run python -m eval.financebench_ciks [--fb data/raw/financebench]
        [--out data/financebench_companies.csv]
        [--evidence docs/milestones/M02-financebench-cik-check.json]

For each company in the FinanceBench questions:

1. Candidates: CIKs whose SEC name (company_tickers.json, or the filer names loaded from FSDS into
   `curated.companies`, exported to `data/targets/company_names.csv`) matches the company name
   after dropping legal suffixes, or whose ticker equals it.
2. Verification through `SecClient`: a candidate is accepted only if its submissions show, for EVERY
   10-K and 10-Q document FinanceBench cites (`3M_2018_10K`, `PFIZER_2023Q2_10Q`), a filing of that
   form whose report date falls in the cited year. 8-K and earnings documents cannot be checked
   this way and are listed as unchecked (the company's identity rests on its 10-K/10-Q matches).
3. One accepted candidate: written to the CSV (`company,cik`). None, or several tied: the company is
   left OUT of the CSV and listed as unresolved in the evidence file. A CIK is never guessed.
   Exception, flagged `resolved_name_only` in the evidence: a company whose cited documents are
   all 8-Ks or earnings releases (nothing to check against) and whose name matches exactly one CIK.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rapidfuzz import fuzz

from pipelines.fetch.list_filings import SUBMISSIONS_URL, FilingTarget, collect_cik
from pipelines.fetch.sec_client import SecClient, SecRequestError

DEFAULT_OUT = Path("data/financebench_companies.csv")
DEFAULT_EVIDENCE = Path("docs/milestones/M02-financebench-cik-check.json")
MIN_SCORE = 90
PREFIX_SCORE = 95.0
_SUFFIX = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "plc", "ltd", "limited",
    "holdings",
    "the", "de", "new", "ny", "nj", "ma", "mn", "pa", "va", "oh",
}  # fmt: skip
_DOC = re.compile(
    r"^(?P<stem>.+?)_(?P<year>\d{4})(?:Q(?P<q>[1-4]))?_(?P<kind>10K|10Q|8K|EARNINGS)", re.I
)


def normalize(name: str) -> str:
    words = re.sub(r"[^a-z0-9& ]+", " ", name.lower().replace("&", " and ")).split()
    return " ".join(w for w in words if w not in _SUFFIX)


@dataclass(frozen=True)
class Candidate:
    cik: int
    name: str
    score: float


def candidates(company: str, names: list[tuple[int, str, str]]) -> list[Candidate]:
    """(cik, SEC name, ticker) rows whose name or ticker matches `company`, best first."""
    want = normalize(company)
    found: dict[int, Candidate] = {}
    for cik, name, ticker in names:
        got = normalize(name)
        if got == want or ticker.lower() == company.lower():
            score = 100.0
        elif got.startswith(want + " "):  # "amazon" is the first word(s) of "amazon com"
            score = PREFIX_SCORE
        else:
            score = fuzz.ratio(want, got)
        if score >= MIN_SCORE and (cik not in found or score > found[cik].score):
            found[cik] = Candidate(cik, name, score)
    return sorted(found.values(), key=lambda c: (-c.score, c.cik))


def parse_doc(doc_name: str) -> dict[str, Any] | None:
    m = _DOC.match(doc_name)
    if not m:
        return None
    return {"year": int(m["year"]), "kind": m["kind"].upper()}


def verify(filings: list[FilingTarget], docs: list[str]) -> dict[str, Any]:
    """Match each cited 10-K / 10-Q document to a filing of that form reported in the cited year."""
    matched: dict[str, str] = {}
    missing: list[str] = []
    unchecked: list[str] = []
    for doc in docs:
        info = parse_doc(doc)
        if info is None or info["kind"] not in ("10K", "10Q"):
            unchecked.append(doc)
            continue
        form = "10-K" if info["kind"] == "10K" else "10-Q"
        hit = next(
            (
                f
                for f in filings
                if f.form == form and f.report_date and f.report_date.year == info["year"]
            ),
            None,
        )
        if hit:
            matched[doc] = hit.accession
        else:
            missing.append(doc)
    return {"matched": matched, "missing": missing, "unchecked": unchecked,
            "ok": bool(matched) and not missing}  # fmt: skip


def resolve(
    client: SecClient, company: str, docs: list[str], names: list[tuple[int, str, str]]
) -> dict[str, Any]:
    tried: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    for cand in candidates(company, names)[:5]:
        try:
            sec_name = client.get_json(SUBMISSIONS_URL.format(cik=cand.cik)).get("name", "")
            filings = collect_cik(client, cand.cik, from_date=date(2013, 1, 1)).targets
        except SecRequestError as exc:
            tried.append(
                {"cik": cand.cik, "name": cand.name, "score": cand.score, "error": str(exc)}
            )
            continue
        result = verify(filings, docs)
        entry = {
            "cik": cand.cik,
            "sec_name": sec_name,
            "listed_name": cand.name,
            "score": cand.score,
            **result,
        }
        tried.append(entry)
        if result["ok"]:
            accepted.append(entry)
    best = (
        [a for a in accepted if a["score"] == max(x["score"] for x in accepted)] if accepted else []
    )
    chosen = best[0] if len(best) == 1 else None
    status = "resolved" if chosen else ("ambiguous" if best else "unresolved")
    if not accepted:
        # Every cited document is an 8-K or an earnings release, which the submissions cannot
        # confirm. Accept only an exact, unique name match, and say so in the evidence.
        reachable = [t for t in tried if "error" not in t]
        uncheckable = [t for t in reachable if not t["matched"] and not t["missing"]]
        exact = [t for t in uncheckable if t["score"] == 100.0]
        if len(exact) == 1 and len(uncheckable) == len(reachable):
            chosen, status = exact[0], "resolved_name_only"
    return {
        "company": company, "documents": docs, "cik": chosen["cik"] if chosen else None,
        "status": status, "candidates_tried": tried,
    }  # fmt: skip


def load_names(tickers: Path, lake_names: Path | None) -> list[tuple[int, str, str]]:
    rows: dict[tuple[int, str], str] = {}
    for v in json.loads(tickers.read_text()).values():
        rows[(int(v["cik_str"]), v["title"])] = v["ticker"]
    if lake_names and lake_names.is_file():
        with lake_names.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["name"]:
                    rows.setdefault((int(r["cik"]), r["name"]), r["ticker"])
    return [(cik, name, ticker) for (cik, name), ticker in rows.items()]


def main(argv: list[str] | None = None, *, client: SecClient | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve FinanceBench companies to verified CIKs.")
    parser.add_argument("--fb", type=Path, default=Path("data/raw/financebench"))
    parser.add_argument(
        "--tickers", type=Path, default=Path("data/company_tickers_2026-11-17.json")
    )
    parser.add_argument("--lake-names", type=Path, default=Path("data/targets/company_names.csv"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    args = parser.parse_args(argv)

    questions = [
        json.loads(ln)
        for ln in (args.fb / "financebench_open_source.jsonl").read_text().splitlines()
        if ln
    ]
    docs: dict[str, set[str]] = defaultdict(set)
    for q in questions:
        docs[q["company"]].add(q["doc_name"])
    names = load_names(args.tickers, args.lake_names)
    load_dotenv(Path.cwd() / ".env")
    sec = client or SecClient()
    try:
        results = [resolve(sec, c, sorted(d), names) for c, d in sorted(docs.items())]
    finally:
        if client is None:
            sec.close()

    resolved = [r for r in results if r["status"] in ("resolved", "resolved_name_only")]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["company", "cik"])
        writer.writerows((r["company"], r["cik"]) for r in resolved)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(
        json.dumps(
            {"n_companies": len(results), "n_resolved": len(resolved), "results": results}, indent=1
        )
        + "\n"
    )
    for r in results:
        chosen = next((t for t in r["candidates_tried"] if t.get("cik") == r["cik"]), {})
        print(
            f"{r['status']:10} {r['company']:22} cik={r['cik']} "
            f"sec_name={chosen.get('sec_name', '-')}"
        )
    print(f"{len(resolved)}/{len(results)} resolved; wrote {args.out}")
    return 0 if len(resolved) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
