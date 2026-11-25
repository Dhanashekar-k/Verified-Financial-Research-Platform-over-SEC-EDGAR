"""Build tests/fixtures/entities/companies.csv from the frozen reference files under data/.

    uv run python tests/fixtures/make_entities_fixture.py

`data/` is gitignored, so CI cannot read it; the resolver tests read this committed snapshot
instead. Columns: cik, name, ticker, aliases (extra tickers, `|`-separated). Rows are the tracked
CIKs (data/targets/tracked_ciks.csv, S&P 500) plus the FinanceBench companies
(data/financebench_companies.csv); names for FinanceBench-only CIKs come from
data/targets/company_names.csv. Nothing is edited by hand.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests" / "fixtures" / "entities" / "companies.csv"


def main() -> None:
    tracked = {
        int(r["cik"]): (r["name"], r["ticker"])
        for r in csv.DictReader((ROOT / "data/targets/tracked_ciks.csv").open())
    }
    sec_names = {
        int(r["cik"]): (r["name"], r["ticker"])
        for r in csv.DictReader((ROOT / "data/targets/company_names.csv").open())
    }
    fb = [int(r["cik"]) for r in csv.DictReader((ROOT / "data/financebench_companies.csv").open())]
    people = json.loads((ROOT / "data/company_tickers_2026-11-17.json").read_text())
    extra: dict[int, set[str]] = {}
    for v in people.values():
        extra.setdefault(int(v["cik_str"]), set()).add(v["ticker"])
    rows = dict(tracked)
    for cik in fb:
        rows.setdefault(cik, sec_names[cik])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["cik", "name", "ticker", "aliases"])
        for cik in sorted(rows):
            name, ticker = rows[cik]
            others = sorted(extra.get(cik, set()) - {ticker})
            w.writerow([cik, name, ticker, "|".join(others)])
    print(f"wrote {len(rows)} companies to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
