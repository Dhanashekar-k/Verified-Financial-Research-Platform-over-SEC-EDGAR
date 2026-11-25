"""Metric coverage (M05 section 3.1): for each catalog metric, the share of tracked companies for
which the compiler returns a value in a fiscal year, and why it does not for the rest.

    uv run python -m eval.coverage_report --dsn "$EDGARQA_SERVING_DSN"   # read-only, no LLM

The denominator is the tracked companies (data/targets/tracked_ciks.csv) that have at least one
full-year fact from a 10-K in that fiscal year; the count of all tracked companies is recorded too.
A fiscal year is reported when at least ``--min-companies`` of them filed one. "Found" means the
compiler's own rules returned a number (ADR-043 abstentions count as not found, with the reason).
Output: `eval/results/coverage/<run_id>.json` (a subdirectory: the results report reads only the
top level of `eval/results`).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.engine.engine import load_tracked_ciks
from app.engine.plan.catalog import MetricDef, load_catalog
from app.engine.plan.compiler import ANNUAL_FORM_PREFIX, compile_plan
from app.engine.plan.schema import CompanyRef, PeriodRef, QueryPlan

FILERS = (
    "SELECT fiscal_year, array_agg(DISTINCT cik) FROM facts "
    "WHERE cik = ANY(%s) AND qtrs = 4 AND fiscal_period = 'FY' AND form LIKE %s "
    "GROUP BY fiscal_year ORDER BY fiscal_year"
)


def filers_by_year(conn: Any, ciks: Collection[int], min_companies: int) -> dict[int, list[int]]:
    rows = conn.execute(FILERS, (sorted(ciks), ANNUAL_FORM_PREFIX + "%")).fetchall()
    return {int(y): sorted(c) for y, c in rows if len(c) >= min_companies}


def coverage(
    conn: Any,
    catalog: Mapping[str, MetricDef],
    filers: Mapping[int, Sequence[int]],
    log: Any = print,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for metric in catalog:
        for year, ciks in filers.items():
            plan = QueryPlan(
                intent="numeric",
                companies=[CompanyRef(mention=str(c), cik=c) for c in ciks],
                periods=[PeriodRef(fiscal_year=year)],
                metrics=[metric],
                operation="value",
            )
            outcome = compile_plan(plan, conn, catalog=catalog)
            if len(outcome.results) + len(outcome.not_found) != len(ciks):
                raise RuntimeError(f"{metric} {year}: not one outcome per company")
            out.append(
                {
                    "metric": metric,
                    "fiscal_year": year,
                    "companies": len(ciks),
                    "found": len(outcome.results),
                    "coverage": len(outcome.results) / len(ciks),
                    "not_found_reasons": dict(Counter(n.reason for n in outcome.not_found)),
                }
            )
        log(metric)
    return out


def by_metric(cells: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pooled over the reported years: found / (company, year) pairs."""
    out = []
    for metric in dict.fromkeys(c["metric"] for c in cells):
        mine = [c for c in cells if c["metric"] == metric]
        pairs, found = sum(c["companies"] for c in mine), sum(c["found"] for c in mine)
        reasons: Counter[str] = Counter()
        for c in mine:
            reasons.update(c["not_found_reasons"])
        out.append(
            {
                "metric": metric,
                "pairs": pairs,
                "found": found,
                "coverage": found / pairs if pairs else None,
                "not_found_reasons": dict(reasons),
            }
        )
    return out


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Metric coverage over the tracked companies.")
    ap.add_argument("--dsn", default=os.environ.get("EDGARQA_SERVING_DSN"))
    ap.add_argument("--out", type=Path, default=Path("eval/results/coverage"))
    ap.add_argument("--min-companies", type=int, default=100)
    args = ap.parse_args(argv)
    if not args.dsn:
        raise SystemExit("needs the serving DSN (--dsn or EDGARQA_SERVING_DSN)")

    import psycopg

    tracked = load_tracked_ciks()
    catalog = load_catalog()
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        state = conn.execute("SELECT live_version FROM corpus_state").fetchone()
        filers = filers_by_year(conn, tracked, args.min_companies)
        cells = coverage(conn, catalog, filers)
    run_id = f"{datetime.now(UTC).strftime('%Y-%m-%dT%H-%M-%SZ')}_coverage"
    doc = {
        "run_id": run_id,
        "git_sha": _git_sha(),
        "corpus_version": None if state is None else int(state[0]),
        "config": {
            "tracked_companies": len(tracked),
            "denominator": "tracked companies with a full-year 10-K fact in that fiscal year",
            "min_companies_per_year": args.min_companies,
        },
        "filers_per_year": {str(y): len(c) for y, c in filers.items()},
        "by_metric": by_metric(cells),
        "by_metric_year": cells,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{run_id}.json"
    path.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
