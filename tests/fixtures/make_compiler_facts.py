"""One-off dev script: extract REAL facts for the plan-compiler tests (never run in CI).

Reads the local Iceberg table ``warehouse/curated/facts`` (hadoop catalog, current snapshot)
READ-ONLY and writes ``tests/fixtures/compiler_facts.jsonl``: one JSON object per fact with every
column of the serving ``facts_v7`` table (05_data_contracts.md section 3). ``value`` is a decimal
STRING, dates are ISO strings, ``fact_id`` is the real signed bigint. Rows are never edited: the
script only chooses WHICH real rows to keep, by (cik, tag, fiscal_year, fiscal_period) selection,
and keeps every stored version of a selected fact (the original and the restated latest).

Run once from the repo root::

    uv run python tests/fixtures/make_compiler_facts.py

The five companies were chosen because together their real data holds every scenario the compiler
tests need (see ``COVERAGE`` below, printed with the real values after each run).
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
from pyiceberg.table import StaticTable

REPO = Path(__file__).resolve().parents[2]
TABLE_DIR = REPO / "warehouse" / "curated" / "facts"
OUT = Path(__file__).with_name("compiler_facts.jsonl")
HARD_CAP = 300

# the facts_v7 columns, in DDL order (05 section 3)
COLUMNS = (
    "fact_id fact_key cik tag taxonomy uom qtrs period_start period_end fiscal_year "
    "fiscal_period calendar_frame value accession form filed is_latest is_original is_restated "
    "anomaly_score derived_from"
).split()

REV_C = "RevenueFromContractWithCustomerExcludingAssessedTax"
REV_T = "Revenues"
COST_1 = "CostOfRevenue"
COST_2 = "CostOfGoodsAndServicesSold"
GP = "GrossProfit"
OPINC = "OperatingIncomeLoss"
NI = "NetIncomeLoss"
EPS = "EarningsPerShareDiluted"
ASSETS = "Assets"
SHARES = "CommonStockSharesOutstanding"

TI, INSULET, DOLLAR_TREE, AFLAC, INOGEN = 97476, 1145197, 935703, 4977, 1294133

P = tuple[int, str]  # (fiscal_year, fiscal_period)

# (cik, tags, periods): every stored version of every matching fact is kept.
SELECTION: list[tuple[int, Sequence[str], Sequence[P]]] = [
    # Texas Instruments: one revenue tag, both cost tags agreeing, GrossProfit reported, EPS and
    # instants, a derived Q4 (and its 9M parent), a Q2 with H1/9M rows next to it.
    (
        TI,
        [REV_C],
        [
            (2021, "FY"),
            (2022, "FY"),
            (2021, "Q2"),
            (2022, "Q2"),
            (2022, "H1"),
            (2022, "Q3"),
            (2022, "9M"),
            (2022, "Q4"),
            (2021, "Q4"),
        ],
    ),
    (TI, [COST_1, COST_2], [(2021, "FY"), (2022, "FY"), (2021, "Q2"), (2022, "Q2"), (2022, "Q4")]),
    (TI, [GP], [(2021, "FY"), (2022, "FY"), (2022, "Q2"), (2022, "Q4")]),
    (TI, [OPINC], [(2021, "FY"), (2022, "FY"), (2022, "Q2")]),
    (TI, [NI], [(2021, "FY"), (2022, "FY")]),
    (TI, [EPS], [(2021, "FY"), (2022, "FY"), (2022, "Q2"), (2021, "Q4"), (2022, "Q4")]),
    (TI, [ASSETS], [(2021, "FY"), (2022, "FY"), (2022, "Q2")]),
    # Insulet: revenue tags agree (FY2020, FY2021 as filed), conflict settled by the gross-profit
    # identity (FY2022, Q2/Q3/Q4), FY2021 contract revenue restated, a restated derived Q4,
    # the full Q2/H1/Q3/9M/FY ladder, a net loss quarter (non-positive growth base).
    (
        INSULET,
        [REV_C, REV_T],
        [
            (2020, "FY"),
            (2021, "FY"),
            (2022, "FY"),
            (2021, "Q2"),
            (2021, "H1"),
            (2021, "Q3"),
            (2021, "9M"),
            (2021, "Q4"),
            (2022, "Q2"),
            (2022, "Q3"),
        ],
    ),
    (
        INSULET,
        [COST_1, COST_2],
        [
            (2021, "FY"),
            (2022, "FY"),
            (2021, "Q2"),
            (2021, "Q3"),
            (2021, "Q4"),
            (2022, "Q2"),
            (2022, "Q3"),
        ],
    ),
    (
        INSULET,
        [GP],
        [
            (2021, "FY"),
            (2022, "FY"),
            (2021, "Q2"),
            (2021, "Q3"),
            (2021, "Q4"),
            (2022, "Q2"),
            (2022, "Q3"),
        ],
    ),
    (INSULET, [OPINC], [(2020, "FY"), (2021, "FY"), (2022, "FY")]),
    (INSULET, [NI], [(2021, "Q2"), (2022, "Q2"), (2021, "Q3"), (2022, "Q3"), (2022, "FY")]),
    # Dollar Tree: revenue tags differ by 0.043% (FY2021) and 0.048% (FY2022), inside the
    # 4-significant-figure tolerance, but by 0.073% (FY2023) and 0.071% (Q2 2023), outside it,
    # with no GrossProfit fact to settle them; no GrossProfit at all (fallback formula); a net
    # loss; no R&D at all; restated instant share counts.
    (
        DOLLAR_TREE,
        [REV_C, REV_T],
        [(2021, "FY"), (2022, "FY"), (2023, "FY"), (2023, "Q2"), (2022, "Q2")],
    ),
    (DOLLAR_TREE, [COST_1, COST_2], [(2021, "FY"), (2022, "FY"), (2023, "FY"), (2022, "Q2")]),
    (DOLLAR_TREE, [OPINC], [(2021, "FY"), (2022, "FY"), (2023, "FY")]),
    (DOLLAR_TREE, [NI], [(2021, "FY"), (2022, "FY"), (2023, "FY")]),
    (DOLLAR_TREE, [SHARES], [(2021, "FY"), (2022, "FY"), (2022, "Q2"), (2023, "Q2")]),
    # Aflac: one revenue tag, no OperatingIncomeLoss at all (missing formula input), restated
    # net income / revenue / EPS / total assets (original and latest both stored), a restated
    # derived Q4 (original and latest) with its 9M parent.
    (AFLAC, [REV_C, REV_T], [(2020, "FY"), (2021, "FY"), (2022, "FY"), (2021, "Q4"), (2021, "9M")]),
    (AFLAC, [NI], [(2020, "FY"), (2021, "FY"), (2022, "FY"), (2021, "Q4"), (2021, "9M")]),
    (AFLAC, [EPS], [(2020, "FY"), (2021, "FY"), (2022, "FY")]),
    (AFLAC, [ASSETS], [(2021, "FY"), (2022, "FY")]),
    # Inogen: both revenue tags AND both cost tags conflict, GrossProfit reported settles both,
    # operating losses (non-positive growth base), negative EPS.
    (
        INOGEN,
        [REV_C, REV_T],
        [(2020, "FY"), (2021, "FY"), (2022, "FY"), (2023, "FY"), (2021, "Q2")],
    ),
    (INOGEN, [COST_1, COST_2], [(2020, "FY"), (2021, "FY"), (2022, "FY"), (2021, "Q2")]),
    (INOGEN, [GP], [(2020, "FY"), (2021, "FY"), (2022, "FY"), (2021, "Q2")]),
    (INOGEN, [OPINC], [(2020, "FY"), (2021, "FY"), (2022, "FY")]),
    (INOGEN, [NI], [(2020, "FY"), (2021, "FY")]),
    (INOGEN, [EPS], [(2021, "FY"), (2022, "FY")]),
]


def _current_metadata() -> Path:
    hint = int((TABLE_DIR / "metadata" / "version-hint.text").read_text().strip())
    return TABLE_DIR / "metadata" / f"v{hint}.metadata.json"


def _data_files(ciks: Sequence[int]) -> list[str]:
    """Data files of the CURRENT snapshot that can hold the ciks (pyiceberg StaticTable)."""
    os.chdir(REPO)  # the table's own paths are relative to the repo root
    table = StaticTable.from_metadata(str(_current_metadata()))
    scan = table.scan(row_filter=f"cik in ({','.join(map(str, ciks))})")
    return [t.file.file_path for t in scan.plan_files()]


def _iso(v: object) -> object:
    return v.isoformat() if isinstance(v, date) else v


def extract() -> list[dict[str, object]]:
    ciks = sorted({c for c, _, _ in SELECTION})
    files = _data_files(ciks)
    con = duckdb.connect()
    listing = ",".join("'" + f + "'" for f in files)
    con.execute(
        f"CREATE VIEW facts AS SELECT * FROM read_parquet([{listing}]) "
        f"WHERE cik IN ({','.join(map(str, ciks))})"
    )
    wanted = [
        (cik, tag, fy, fp) for cik, tags, periods in SELECTION for tag in tags for fy, fp in periods
    ]
    con.execute("CREATE TABLE wanted(cik INT, tag TEXT, fiscal_year INT, fiscal_period TEXT)")
    con.executemany("INSERT INTO wanted VALUES (?, ?, ?, ?)", wanted)
    cur = con.execute(
        "SELECT f.* FROM facts f JOIN wanted w USING (cik, tag, fiscal_year, fiscal_period) "
        "ORDER BY f.cik, f.tag, f.fiscal_year, f.qtrs, f.fiscal_period, f.is_original DESC, "
        "f.fact_id"
    )
    names = [d[0] for d in cur.description]
    rows: list[dict[str, object]] = []
    for rec in cur.fetchall():
        r = dict(zip(names, rec, strict=True))
        r["anomaly_score"] = None if r.get("anomaly_score") is None else float(r["anomaly_score"])
        out: dict[str, object] = {}
        for col in COLUMNS:
            v = r[col]
            if isinstance(v, Decimal):
                v = str(v)
            elif col == "derived_from" and v is not None:
                v = [int(x) for x in v]
            out[col] = _iso(v)
        rows.append(out)
    return rows


def coverage(rows: list[dict[str, object]]) -> dict[str, list[str]]:
    """Which real instances each company supplies, computed from the extracted rows."""
    by_key: dict[tuple[object, ...], list[dict[str, object]]] = defaultdict(list)
    for r in rows:
        by_key[(r["cik"], r["tag"], r["fiscal_year"], r["fiscal_period"])].append(r)
    out: dict[str, list[str]] = defaultdict(list)
    for (cik, tag, fy, fp), rs in sorted(by_key.items(), key=lambda kv: str(kv[0])):
        label = f"{cik}:{tag[:24]}:{fy}{fp}"
        if any(r["is_restated"] and r["is_original"] != r["is_latest"] for r in rs):
            out["restated (2 versions)"].append(label)
        if any(r["derived_from"] for r in rs):
            out["derived Q4"].append(label)
        if any(int(str(r["fact_id"])) < 0 for r in rs):
            out["negative fact_id"].append(label)
        if any(r["tag"] in (EPS,) for r in rs):
            out["EPS uom=" + str(rs[0]["uom"])].append(label)
        if rs[0]["uom"] == "shares":
            out["shares"].append(label)
        if int(str(rs[0]["qtrs"])) == 0:
            out["instant qtrs=0"].append(label)
        if any(int(str(r["qtrs"])) in (2, 3) for r in rs):
            out["YTD qtrs 2/3"].append(label)
    return out


def main() -> int:
    rows = extract()
    if len(rows) > HARD_CAP:
        print(f"fixture has {len(rows)} rows, cap is {HARD_CAP}", file=sys.stderr)
        return 1
    ids = [r["fact_id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate fact_id in the fixture"
    OUT.write_text("".join(json.dumps(r, sort_keys=False) + "\n" for r in rows))
    per_cik: dict[object, int] = defaultdict(int)
    for r in rows:
        per_cik[r["cik"]] += 1
    print(f"wrote {len(rows)} rows to {OUT.relative_to(REPO)}; rows per cik: {dict(per_cik)}")
    for case, labels in sorted(coverage(rows).items()):
        shown = ", ".join(sorted(set(labels))[:6])
        print(f"- {case}: {len(set(labels))} fact groups, e.g. {shown}")
    assert re.fullmatch(r"-?\d+", str(rows[0]["fact_id"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
