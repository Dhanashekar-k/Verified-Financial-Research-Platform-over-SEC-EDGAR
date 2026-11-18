"""Re-verify FinanceBench companies by reading the cited documents' own cover pages.

    uv run python -m eval.financebench_recheck [--companies Block Amcor "Foot Locker"]
        [--fb data/raw/financebench] [--evidence docs/milestones/M02-financebench-cik-check.json]

`eval.financebench_ciks` accepts a CIK when the submissions show a 10-K/10-Q of the cited year.
This is the stronger check, for companies whose identity is delicate (a rename, a reincorporation,
or only 8-Ks cited). For each cited PDF it reads the cover page (registrant name, SEC file number,
"fiscal year / quarterly period ended" or "date of report") and compares it with the CIK's
submissions, fetched through `SecClient`, ALL forms:

* the filing: a 10-K/10-Q whose `reportDate` equals the cover's period end, or an 8-K whose
  `filingDate` or `reportDate` equals the cover's date of report (exact dates, not years);
* the file number printed on the cover against the filing's `fileNumber` (leading zeros ignored);
* the registrant name on the cover against the filer's current name and every former name
  (`formerNames`), so a renamed company ("Square, Inc." -> "Block, Inc.") matches.

The result is written into the evidence file under `recheck`; nothing else in it changes. A document
whose cover cannot be read is reported as such, never counted as a pass.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pymupdf
from dotenv import load_dotenv

from eval.financebench_ciks import normalize, parse_doc
from pipelines.fetch.list_filings import SUBMISSIONS_PAGE_URL, SUBMISSIONS_URL
from pipelines.fetch.sec_client import SecClient

DEFAULT_COMPANIES = ("Block", "Amcor", "Foot Locker")
FILING_COLUMNS = (
    "accessionNumber", "form", "filingDate", "reportDate", "fileNumber", "primaryDocument",
)  # fmt: skip
_DATE = r"([A-Za-z]+\.?\s+\d{1,2},\s*\d{4})"
_PERIOD = re.compile(
    rf"(?:fiscal\s+year|quarterly\s+period|year|period)\s+ended:?\s*{_DATE}", re.IGNORECASE
)
_EVENT = re.compile(rf"date\s+of\s+report[^:]*:\s*{_DATE}", re.IGNORECASE)
_FILE_NO = re.compile(r"commission\s+file\s+number(?:\(s\))?:?\s*(\d{1,3}-\d{4,6})", re.IGNORECASE)
_NAME = re.compile(r"([A-Z][A-Za-z0-9.,&'\- ]{2,70}?)\s*\(\s*exact\s+name", re.IGNORECASE)
_8K_FILE_NO = re.compile(r"(\d{1,3}-\d{4,6})")


def parse_date(text: str) -> date | None:
    cleaned = " ".join(text.replace(".", "").split())
    for fmt in ("%B %d, %Y", "%B %d,%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _iso(text: str) -> str | None:
    parsed = parse_date(text)
    return parsed.isoformat() if parsed else None


def read_cover(text: str) -> dict[str, Any]:
    """Registrant, file number and period/event date from the first pages of a filing."""
    flat = " ".join(text.split())
    name = _NAME.search(flat)
    file_no = _FILE_NO.search(flat)
    if file_no is None and name:  # an 8-K lists the number after the state, before the IRS number
        tail = _8K_FILE_NO.findall(flat[name.end() : name.end() + 160])
        file_no = re.match(r"(.*)", tail[0]) if tail else None
    period = _PERIOD.search(flat)
    event = _EVENT.search(flat)
    return {
        "registrant": _clean_name(name.group(1)) if name else None,
        "file_number": (file_no.group(1) if file_no else None),
        "period_end": _iso(period.group(1)) if period else None,
        "date_of_report": _iso(event.group(1)) if event else None,
    }  # fmt: skip


def _clean_name(raw: str) -> str:
    """`... Commission File Number 001-37622 SQUARE, INC.` -> `SQUARE, INC.`"""
    tail = re.split(r"\d{1,3}-\d{4,6}", raw)[-1]
    return tail.strip(" ,:")


def same_file_number(a: str | None, b: str | None) -> bool:
    def key(x: str) -> tuple[int, str]:
        left, _, right = x.partition("-")
        return int(left), right

    return bool(a and b and key(a) == key(b))


def all_filings(client: SecClient, cik: int) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The submissions main document and EVERY filing (all forms, all pages) as flat rows."""
    main = client.get_json(SUBMISSIONS_URL.format(cik=cik))
    blocks = [main["filings"]["recent"]]
    for entry in main["filings"].get("files") or []:
        blocks.append(client.get_json(SUBMISSIONS_PAGE_URL.format(name=entry["name"])))
    rows: list[dict[str, str]] = []
    for block in blocks:
        n = len(block["accessionNumber"])
        for i in range(n):
            rows.append({k: (block.get(k) or [""] * n)[i] for k in FILING_COLUMNS})
    return main, rows


def names_over_time(main: dict[str, Any]) -> list[dict[str, str]]:
    now = [{"name": main.get("name", ""), "from": "", "to": ""}]
    former = [
        {"name": f["name"], "from": f.get("from", ""), "to": f.get("to", "")}
        for f in main.get("formerNames") or []
    ]
    return now + former  # fmt: skip


def names_in_force(main: dict[str, Any], on: str) -> set[str]:
    """Normalised names the filer used on a date (YYYY-MM-DD): former names by their from/to
    dates, the current name from the day after the last rename."""
    former = [
        (f.get("from", "")[:10] or "0000-00-00", f.get("to", "")[:10] or "9999-99-99", f["name"])
        for f in main.get("formerNames") or []
    ]
    current_from = max((to for _, to, _ in former), default="0000-00-00")
    names = {normalize(n) for lo, hi, n in former if lo <= on <= hi}
    if on > current_from or not former:
        names.add(normalize(main.get("name", "")))
    return names


def check_document(
    doc: str, cover: dict[str, Any], main: dict[str, Any], rows: list[dict[str, str]]
) -> dict[str, Any]:
    info = parse_doc(doc) or {}
    kind = info.get("kind")
    result: dict[str, Any] = {"document": doc, "cover": cover}
    if kind in ("10K", "10Q"):
        form = "10-K" if kind == "10K" else "10-Q"
        want = cover["period_end"]
        hit = next(
            (r for r in rows if r["form"] == form and want and r["reportDate"] == want), None
        )
    elif kind == "8K":
        want = cover["date_of_report"]
        hit = next(
            (
                r
                for r in rows
                if r["form"] == "8-K" and want and want in (r["filingDate"], r["reportDate"])
            ),
            None,
        )
    else:
        result["status"] = "unchecked"
        result["reason"] = "an earnings release has no filing date of its own on its cover"
        return result
    if hit is None:
        result["status"] = "no_matching_filing" if want else "cover_unreadable"
        return result
    in_force = names_in_force(main, hit["filingDate"])
    name_ok = bool(cover["registrant"]) and normalize(cover["registrant"]) in in_force
    file_ok = same_file_number(cover["file_number"], hit["fileNumber"])
    result["sec_filing"] = {
        k: hit[k]
        for k in (
            "accessionNumber",
            "form",
            "filingDate",
            "reportDate",
            "fileNumber",
            "primaryDocument",
        )
    }
    result["checks"] = {
        "filing_found_by_exact_date": True,
        "registrant_is_the_name_in_force_on_the_filing_date": name_ok,
        "file_number_matches_sec_metadata": file_ok,
    }
    result["status"] = "confirmed" if name_ok and file_ok else "mismatch"
    return result


def original_cover(client: SecClient, cik: int, hit: dict[str, str]) -> dict[str, Any]:
    """The cover printed on the filing as EDGAR serves it (what the PDF should be a copy of)."""
    from selectolax.lexbor import LexborHTMLParser

    url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{hit['accessionNumber'].replace('-', '')}/{hit['primaryDocument']}"
    )
    tree = LexborHTMLParser(client.get(url).decode("utf-8", "replace"))
    for node in tree.css("script,style,head,ix\\:header"):  # the hidden XBRL header comes first
        node.decompose()
    text = tree.body.text(separator=" ") if tree.body else ""
    return {"url": url, **read_cover(text[:20000])}


def reconcile_with_original(client: SecClient, cik: int, entry: dict[str, Any]) -> None:
    """A file-number mismatch is either 'the PDF is not this filing' or 'the SEC metadata differs
    from what the filing prints'. Read the original's cover to tell which."""
    original = original_cover(client, cik, entry["sec_filing"])
    cover = entry["cover"]
    same = {
        "file_number": same_file_number(cover["file_number"], original["file_number"]),
        "registrant": normalize(cover["registrant"] or "")
        == normalize(original["registrant"] or ""),
        "period_end": cover["period_end"] == original["period_end"],
    }
    entry["original_cover"] = original
    entry["pdf_cover_equals_original_cover"] = same
    if all(same.values()):
        entry["status"] = "confirmed_pdf_matches_original_filing"
        entry["note"] = (
            "the SEC metadata fileNumber differs from the file number printed on the filing "
            "itself; the PDF prints the same as the original filing"
        )


def recheck_company(
    client: SecClient, company: str, cik: int, docs: list[str], fb: Path
) -> dict[str, Any]:
    main, rows = all_filings(client, cik)
    out = []
    for doc in docs:
        with pymupdf.open(fb / "pdfs" / f"{doc}.pdf") as pdf:
            text = " ".join(pdf[i].get_text() for i in range(min(2, len(pdf))))
        entry = check_document(doc, read_cover(text), main, rows)
        name_check = "registrant_is_the_name_in_force_on_the_filing_date"
        if entry["status"] == "mismatch" and entry["checks"][name_check]:
            reconcile_with_original(client, cik, entry)
        out.append(entry)
    return {
        "company": company, "cik": cik, "sec_name_now": main.get("name"),
        "sec_names": names_over_time(main), "documents": out,
        "10k_report_dates": sorted(
            {r["reportDate"] for r in rows if r["form"] == "10-K" and r["reportDate"]}
        ),
        "all_confirmed": all(d["status"] not in ("mismatch", "no_matching_filing") for d in out)
        and any(d["status"].startswith("confirmed") for d in out),
    }  # fmt: skip


def main(argv: list[str] | None = None, *, client: SecClient | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-verify FinanceBench companies from cover pages."
    )
    parser.add_argument("--companies", nargs="*", default=list(DEFAULT_COMPANIES))
    parser.add_argument("--fb", type=Path, default=Path("data/raw/financebench"))
    parser.add_argument(
        "--evidence", type=Path, default=Path("docs/milestones/M02-financebench-cik-check.json")
    )
    args = parser.parse_args(argv)

    evidence = json.loads(args.evidence.read_text())
    by_company = {r["company"]: r for r in evidence["results"]}
    load_dotenv(Path.cwd() / ".env")
    sec = client or SecClient()
    results = []
    try:
        for company in args.companies:
            r = by_company[company]
            if r["cik"] is None:
                print(f"{company}: no CIK in the evidence file; nothing to recheck")
                continue
            rc = recheck_company(sec, company, r["cik"], r["documents"], args.fb)
            results.append(rc)
            print(
                f"{company} (CIK {rc['cik']}, now {rc['sec_name_now']!r}): "
                + ", ".join(f"{d['document']}={d['status']}" for d in rc["documents"])
            )
    finally:
        if client is None:
            sec.close()
    evidence["recheck"] = {
        "method": __doc__.split("\n\n")[1].strip() if __doc__ else "",
        "results": results,
    }
    args.evidence.write_text(json.dumps(evidence, indent=1) + "\n")
    return 0 if results and all(r["all_confirmed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
