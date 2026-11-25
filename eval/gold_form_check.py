"""Does numeric gold cite a 10-Q's trailing-twelve-month figure as a full fiscal year? (ADR-047)

    uv run python -m eval.gold_form_check --dsn "$EDGARQA_SERVING_DSN"   # read-only

The lake labels a 10-Q's twelve-month amount `FY` with `qtrs = 4`, and the gold generator's
`screen()` does not filter forms. This looks up every `gold_fact_ids` entry of numeric_auto.jsonl
in the serving `facts` view and flags full-year duration facts that are not from a 10-K family
form. It reads gold and never writes it.

Exit code: 0 every id found and none flagged; 1 something is flagged or could not be checked (stop
and report; gold is Shekar's to change).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.engine.plan.compiler import ANNUAL_FORM_PREFIX

GOLD = Path("eval/sets/numeric_auto.jsonl")
OUT = Path("docs/m5/gold_form_check.json")
QUERY = (
    "SELECT fact_id, cik, tag, qtrs, fiscal_year, fiscal_period, form, period_end "
    "FROM facts WHERE fact_id = ANY(%s)"
)


def check(rows: list[dict[str, Any]], conn: Any) -> dict[str, Any]:
    wanted = sorted({int(i) for r in rows for i in r["gold_fact_ids"]})
    found = {int(r[0]): r for r in conn.execute(QUERY, (wanted,)).fetchall()}
    flagged = {
        fact_id: {
            "fact_id": fact_id,
            "cik": r[1],
            "tag": r[2],
            "fiscal_year": r[4],
            "form": r[6],
            "period_end": str(r[7]),
        }
        for fact_id, r in found.items()
        if r[3] == 4 and r[5] == "FY" and not str(r[6]).startswith(ANNUAL_FORM_PREFIX)
    }
    missing = sorted(set(wanted) - set(found))

    def citing(ids: set[int]) -> list[str]:
        return sorted(r["id"] for r in rows if ids & {int(i) for i in r["gold_fact_ids"]})

    return {
        "gold_rows": len(rows),
        "distinct_gold_fact_ids": len(wanted),
        "found": len(found),
        "missing": len(missing),
        "missing_fact_ids": missing,
        "gold_rows_with_a_missing_id": citing(set(missing)),
        "fy_facts_from_a_non_10k_form": [flagged[i] for i in sorted(flagged)],
        "affected_gold_rows": citing(set(flagged)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--dsn", default=os.environ.get("EDGARQA_SERVING_DSN"))
    ap.add_argument("--gold", type=Path, default=GOLD)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    if not args.dsn:
        raise SystemExit("needs the serving DSN (--dsn or EDGARQA_SERVING_DSN)")

    import psycopg

    rows = [json.loads(line) for line in args.gold.read_text().splitlines() if line.strip()]
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        state = conn.execute("SELECT live_version FROM corpus_state").fetchone()
        result = check(rows, conn)
    doc = {
        "measured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "serving `facts` view",
        "corpus_version": None if state is None else int(state[0]),
        "gold_file": str(args.gold),
        "query": QUERY,
        **result,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2) + "\n")
    print(
        f"wrote {args.out}: {result['found']}/{result['distinct_gold_fact_ids']} ids found, "
        f"{len(result['affected_gold_rows'])} gold rows affected"
    )
    return 1 if result["affected_gold_rows"] or result["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
