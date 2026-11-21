"""Measure the parse over the full corpus in the lake (curated.filings / curated.chunks) via Athena.

    AWS_PROFILE=edgarqa uv run --with "botocore[crt]" python -m eval.lake_parse_report
        [--seed 20261121] [--out docs/milestones/M02-fullcorpus-metrics.json]

`eval/parse_report.py` measures a local store of 135 filings. This one measures what the Glue job
actually wrote, so the numbers are out-of-sample for everything the parser was tuned on. Output
(JSON, the only source for the report text):

* filings by form and `parse_status`; strict success (`ok` / all) and lenient (`ok` + `partial`);
* `parse_error` breakdown and the "cross-reference" signature of ADR-028: 10-Qs missing BOTH
  `P1-1` and `P1-2`, by company; 10-Ks whose text sits mostly under Item 15;
* chunk counts, token histogram (bins of 50), table / anchor / page counts per `doc_source`;
* 20 random chunks (full text) for a human to read and 50 random chunk `source_url`s in the
  shape `eval.check_source_urls` reads (`html.url_sample`).

Sampling is deterministic: chunks are ordered by md5(chunk_id || seed).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import boto3

WORKGROUP = "edgarqa"
REGION = "us-east-2"
DEFAULT_OUT = Path("docs/milestones/M02-fullcorpus-metrics.json")


class Athena:
    def __init__(self) -> None:
        self.client = boto3.client("athena", region_name=REGION)
        self.scanned = 0

    def rows(self, sql: str) -> list[dict[str, str | None]]:
        qid = self.client.start_query_execution(QueryString=sql, WorkGroup=WORKGROUP)[
            "QueryExecutionId"
        ]
        while True:
            ex = self.client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = ex["Status"]["State"]
            if state == "SUCCEEDED":
                break
            if state in ("FAILED", "CANCELLED"):
                raise RuntimeError(f"{state}: {ex['Status'].get('StateChangeReason')}\n{sql}")
            time.sleep(2)
        self.scanned += ex["Statistics"].get("DataScannedInBytes", 0)
        out: list[dict[str, str | None]] = []
        header: list[str] | None = None
        for page in self.client.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
            for row in page["ResultSet"]["Rows"]:
                vals = [c.get("VarCharValue") for c in row["Data"]]
                if header is None:
                    header = [v or "" for v in vals]
                else:
                    out.append(dict(zip(header, vals, strict=True)))
        return out


def ints(rows: list[dict[str, str | None]], *int_cols: str) -> list[dict[str, Any]]:
    return [
        {k: (int(v) if k in int_cols and v is not None else v) for k, v in r.items()} for r in rows
    ]


def measure(a: Athena, seed: int) -> dict[str, Any]:
    html = "doc_source = 'edgar_html'"
    by_form = ints(
        a.rows(
            f"SELECT form, parse_status, count(*) n FROM curated.filings WHERE {html} "
            "GROUP BY 1, 2 ORDER BY 1, 2"
        ),
        "n",
    )
    total = sum(r["n"] for r in by_form)
    ok = sum(r["n"] for r in by_form if r["parse_status"] == "ok")
    partial = sum(r["n"] for r in by_form if r["parse_status"] == "partial")
    errors = ints(
        a.rows(
            f"SELECT parse_status, parse_error, count(*) n FROM curated.filings WHERE {html} "
            "AND parse_status <> 'ok' GROUP BY 1, 2 ORDER BY 3 DESC"
        ),
        "n",
    )

    # ADR-028 signature: a 10-Q missing BOTH Part I items. Counted per company.
    both = "parse_error LIKE '%P1-1%' AND parse_error LIKE '%P1-2%' AND parse_status = 'partial'"
    n_companies_total = int(
        a.rows(f"SELECT count(DISTINCT cik) c FROM curated.filings WHERE {html}")[0]["c"] or 0
    )
    per_company = ints(
        a.rows(
            "WITH q AS (SELECT cik, accession, filed, "
            f"  CASE WHEN {both} THEN 1 ELSE 0 END AS both_missing "
            f"  FROM curated.filings WHERE {html} AND form IN ('10-Q', '10-Q/A')) "
            "SELECT q.cik, max(c.name) name, max(c.ticker) ticker, count(*) n_10q, "
            "  sum(both_missing) n_both_missing, "
            "  min(CASE WHEN both_missing=1 THEN filed END) first_bad, "
            "  max(CASE WHEN both_missing=1 THEN filed END) last_bad "
            "FROM q LEFT JOIN curated.companies c ON c.cik = q.cik "
            "GROUP BY q.cik HAVING sum(both_missing) > 0 ORDER BY 5 DESC"
        ),
        "cik", "n_10q", "n_both_missing",
    )  # fmt: skip

    # 10-Ks whose text mostly sits under Item 15 (the ADR-026 cross-reference symptom).
    item15 = ints(
        a.rows(
            "WITH s AS (SELECT c.accession, c.section, sum(length(c.text)) ch "
            "  FROM curated.chunks c JOIN curated.filings f ON f.accession = c.accession "
            "  WHERE f.doc_source = 'edgar_html' AND f.form IN ('10-K', '10-K/A') GROUP BY 1, 2), "
            "t AS (SELECT accession, sum(ch) total, sum(CASE WHEN section = '15' THEN ch END) i15 "
            "  FROM s GROUP BY 1) "
            "SELECT count(*) n_10k, count_if(i15 * 2 > total) n_over_50pct_item15, "
            "  count_if(i15 * 10 > total * 8) n_over_80pct_item15 FROM t"
        ),
        "n_10k", "n_over_50pct_item15", "n_over_80pct_item15",
    )[0]  # fmt: skip

    # 10-Qs whose text sits mostly under P2-6 (Exhibits): the filing's body is mislabelled.
    p26 = ints(
        a.rows(
            "WITH s AS (SELECT c.accession, c.section, sum(length(c.text)) ch "
            "  FROM curated.chunks c JOIN curated.filings f ON f.accession = c.accession "
            "  WHERE f.doc_source = 'edgar_html' AND f.form IN ('10-Q', '10-Q/A') GROUP BY 1, 2), "
            "t AS (SELECT accession, sum(ch) total, sum(CASE WHEN section = 'P2-6' THEN ch END) x "
            "  FROM s GROUP BY 1) "
            "SELECT count(*) n_10q_with_chunks, count_if(x * 2 > total) n_over_50pct_p2_6, "
            "  count_if(x * 10 > total * 8) n_over_80pct_p2_6, "
            "  count(DISTINCT CASE WHEN x * 10 > total * 8 THEN accession END) n_acc FROM t"
        ),
        "n_10q_with_chunks", "n_over_50pct_p2_6", "n_over_80pct_p2_6", "n_acc",
    )[0]  # fmt: skip
    p26_companies = int(
        a.rows(
            "WITH s AS (SELECT c.accession, c.section, sum(length(c.text)) ch "
            "  FROM curated.chunks c JOIN curated.filings f ON f.accession = c.accession "
            "  WHERE f.doc_source = 'edgar_html' AND f.form IN ('10-Q', '10-Q/A') GROUP BY 1, 2), "
            "t AS (SELECT accession, sum(ch) total, sum(CASE WHEN section = 'P2-6' THEN ch END) x "
            "  FROM s GROUP BY 1) "
            "SELECT count(DISTINCT f.cik) c FROM t "
            "JOIN curated.filings f ON f.accession = t.accession "
            "WHERE t.x * 10 > t.total * 8"
        )[0]["c"]
        or 0
    )
    p26["companies"] = p26_companies

    chunk_summary = ints(
        a.rows(
            "SELECT doc_source, count(*) chunks, count(DISTINCT accession) docs, max(n_tokens) "
            "max_tokens, count_if(n_tokens > 400) over_400, count_if(is_table) table_chunks, "
            "count_if(anchor IS NOT NULL) with_anchor, count_if(page IS NOT NULL) with_page, "
            "count_if(source_url IS NULL) null_source_url "
            "FROM curated.chunks GROUP BY 1"
        ),
        "chunks", "docs", "max_tokens", "over_400", "table_chunks", "with_anchor", "with_page",
        "null_source_url",
    )  # fmt: skip
    hist = ints(
        a.rows(
            "SELECT doc_source, CAST(floor((n_tokens - 1) / 50) * 50 AS integer) bin_start, "
            "count(*) n FROM curated.chunks GROUP BY 1, 2 ORDER BY 1, 2"
        ),
        "bin_start", "n",
    )  # fmt: skip
    section_counts = ints(
        a.rows(
            "SELECT section, count(*) chunks FROM curated.chunks WHERE doc_source='edgar_html' "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 40"
        ),
        "chunks",
    )

    order = f"to_hex(md5(to_utf8(chunk_id || '{seed}')))"
    picked = a.rows(f"SELECT chunk_id FROM curated.chunks ORDER BY {order} LIMIT 20")
    ids = ", ".join("'" + (r["chunk_id"] or "").replace("'", "''") + "'" for r in picked)
    chunks20 = ints(
        a.rows(
            "SELECT c.chunk_id, c.accession, f.form, c.section, c.section_title, c.n_tokens, "
            "c.is_table, c.page, c.source_url, c.text FROM curated.chunks c "
            f"JOIN curated.filings f ON f.accession = c.accession WHERE c.chunk_id IN ({ids}) "
            "ORDER BY c.chunk_id"
        ),
        "n_tokens",
    )
    urls = [
        r["source_url"]
        for r in a.rows(
            "SELECT source_url FROM curated.chunks WHERE doc_source='edgar_html' "
            f"ORDER BY {order} LIMIT 50"
        )
    ]
    return {
        "seed": seed,
        "html": {
            "n_filings": total,
            "n_companies": n_companies_total,
            "ok": ok,
            "partial": partial,
            "no_sections": total - ok - partial,
            "strict_success": ok / total,
            "lenient_success_ok_plus_partial": (ok + partial) / total,
            "status_by_form": by_form,
            "non_ok_errors": errors,
            "url_sample": urls,
        },
        "cross_reference_10q_both_p1_missing": {
            "companies": len(per_company),
            "filings": sum(r["n_both_missing"] for r in per_company),
            "per_company": per_company,
        },
        "item15_dominant_10k": item15,
        "p2_6_dominant_10q": p26,
        "chunks": chunk_summary,
        "token_histogram": hist,
        "sections_top": section_counts,
        "chunks_20": chunks20,
        "athena_scanned_bytes": a.scanned,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Full-corpus parse metrics from the lake.")
    parser.add_argument("--seed", type=int, default=20261121)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    result = measure(Athena(), args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    h = result["html"]
    print(
        f"{h['n_filings']} filings, {h['n_companies']} companies: ok {h['ok']}, partial "
        f"{h['partial']}, no_sections {h['no_sections']}; strict {h['strict_success']:.4f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
