"""Second-source spot check of `curated.facts` against the SEC companyfacts API (M01 section 3.6).

    uv run python -m pipelines.spark.spotcheck_companyfacts --env local

For 10 companies x 3 metrics x the 2 most recent 10-K filings (60 comparisons), find the
companyfacts value of the annual (or year-end instant) fact and look for the same fact in
`curated.facts` by **accession + tag + period end + duration**. FSDS rounds dates to month end
while companyfacts has exact dates (Apple's FY2022 ends 2022-09-24 vs 20220930), so period ends
match within `DATE_TOLERANCE_DAYS`.

Outcomes per comparison: `match`, `value_mismatch`, `missing_in_curated`, `missing_in_companyfacts`.
Every non-match is left with `classification: null`: classifying a mismatch (restatement, tag
fallback, rounding, bug, ...) is Shekar's job (M01 section 5). The result is written to
`docs/milestones/M01-spotcheck.json`; it is the only source of the spot-check numbers in the DQ
report. The companyfacts `frame` is also compared with the curated `calendar_frame` (rule 6).

The 10 CIKs below are a convenience pick across fiscal-year ends (Sep, Jun, May, Jan x3, Dec x4)
and are checked at run time against the `entityName` the API returns; replace any you disagree
with.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from pipelines.common.spark_session import Env, spark_session
from pipelines.fetch.sec_client import SecClient, SecRequestError
from pipelines.spark.iceberg_util import latest_snapshot_id

log = logging.getLogger(__name__)

DATE_TOLERANCE_DAYS = 15
DEFAULT_OUT = Path("docs/milestones/M01-spotcheck.json")

# (cik, expected substring of entityName)
SPOT_COMPANIES: tuple[tuple[int, str], ...] = (
    (320193, "apple"),  # FYE Sep, 52/53 weeks
    (789019, "microsoft"),  # Jun
    (320187, "nike"),  # May
    (104169, "walmart"),  # Jan, names the year by the year it ends
    (27419, "target"),  # Jan, names the year by the year it starts
    (354950, "home depot"),  # Jan, names the year by the year it starts
    (34088, "exxon"),  # Dec
    (19617, "jpmorgan"),  # Dec
    (78003, "pfizer"),  # Dec
    (1018724, "amazon"),  # Dec
)

# Ordered fallback per metric, as in the metric catalog (first tag with a value wins).
METRICS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ),
    "net_income": ("NetIncomeLoss",),
    "total_assets": ("Assets",),
}
FILINGS_PER_COMPANY = 2
ANNUAL_DAYS = (340, 380)  # a duration of about one year, in days
ANNUAL_FORMS = ("10-K", "10-KT")
EXPECTED_COMPARISONS = len(SPOT_COMPANIES) * len(METRICS) * FILINGS_PER_COMPANY


@dataclass
class Comparison:
    cik: int
    metric: str
    accession: str
    form: str
    tag: str | None
    period_end: str | None  # companyfacts (exact) end date
    qtrs: int | None
    companyfacts_value: str | None
    curated_value: str | None
    curated_period_end: str | None
    outcome: str  # match | value_mismatch | missing_in_curated | missing_in_companyfacts
    frame_companyfacts: str | None
    frame_curated: str | None
    frame_agrees: bool | None
    classification: str | None = None  # Shekar fills this in for every non-match


def companyfacts_url(cik: int) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


def latest_annual_filings(cf: dict[str, Any], n: int = 2) -> list[tuple[str, str]]:
    """The `n` most recent distinct (accession, form) of annual filings, newest first."""
    seen: dict[str, tuple[str, str]] = {}
    for concept in cf.get("facts", {}).get("us-gaap", {}).values():
        for entries in concept.get("units", {}).values():
            for e in entries:
                if e.get("form") in ANNUAL_FORMS and e.get("accn"):
                    seen[e["accn"]] = (e["filed"], e["form"])
    ranked = sorted(seen.items(), key=lambda kv: kv[1][0], reverse=True)
    return [(accn, form) for accn, (_, form) in ranked[:n]]


def filing_period_end(cf: dict[str, Any], accession: str) -> str | None:
    """The reporting period of an annual filing: its latest `end` over all concepts (the rest of
    the entries are comparatives for earlier periods)."""
    ends = [
        e["end"]
        for concept in cf.get("facts", {}).get("us-gaap", {}).values()
        for entries in concept.get("units", {}).values()
        for e in entries
        if e.get("accn") == accession and e.get("form") in ANNUAL_FORMS
    ]
    return max(ends) if ends else None


def expected_fact(
    cf: dict[str, Any], accession: str, tags: tuple[str, ...]
) -> tuple[str, dict[str, Any]] | None:
    """The filing's CURRENT-period annual fact (or year-end instant) for the first tag that has
    one. A tag that only has comparative-year entries in this filing is skipped, so the check
    never validates a prior year by mistake."""
    current = filing_period_end(cf, accession)
    gaap = cf.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        candidates: list[dict[str, Any]] = []
        for entries in gaap.get(tag, {}).get("units", {}).values():
            for e in entries:
                if e.get("accn") != accession or e.get("form") not in ANNUAL_FORMS:
                    continue
                if e["end"] != current:
                    continue
                if e.get("start"):  # a duration: must be about a year
                    days = (date.fromisoformat(e["end"]) - date.fromisoformat(e["start"])).days
                    if not ANNUAL_DAYS[0] <= days <= ANNUAL_DAYS[1]:
                        continue
                candidates.append(e)
        if candidates:
            return tag, candidates[0]
    return None


def compare(
    ciks: tuple[int, ...],
    companyfacts: dict[int, dict[str, Any]],
    curated: list[dict[str, Any]],
    filings_per_company: int = FILINGS_PER_COMPANY,
) -> list[Comparison]:
    """All comparisons. `curated` rows need: cik, accession, tag, period_end (date), qtrs, value
    (Decimal), calendar_frame."""
    by_acc_tag: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in curated:
        if row.get("taxonomy", "us-gaap") != "us-gaap":
            continue  # companyfacts' us-gaap tags are compared with us-gaap facts only
        by_acc_tag.setdefault((row["accession"], row["tag"]), []).append(row)

    out: list[Comparison] = []
    for cik in ciks:
        cf = companyfacts[cik]
        for accession, form in latest_annual_filings(cf, filings_per_company):
            for metric, tags in METRICS.items():
                found = expected_fact(cf, accession, tags)
                if found is None:
                    out.append(
                        Comparison(cik, metric, accession, form, None, None, None, None, None,
                                   None, "missing_in_companyfacts", None, None, None)
                    )  # fmt: skip
                    continue
                tag, e = found
                end = date.fromisoformat(e["end"])
                qtrs = 4 if e.get("start") else 0
                value = Decimal(str(e["val"]))
                near = [
                    r
                    for r in by_acc_tag.get((accession, tag), [])
                    if r["qtrs"] == qtrs
                    and abs((r["period_end"] - end).days) <= DATE_TOLERANCE_DAYS
                ]
                same = [r for r in near if Decimal(r["value"]) == value]
                pick = same[0] if same else (near[0] if near else None)
                if len(near) > 1:
                    # (fact_key, accession) is unique in facts, so two rows for one accession,
                    # tag, period and duration is a curation bug, whatever their values are.
                    outcome = "duplicate_in_curated"
                elif same:
                    outcome = "match"
                else:
                    outcome = "value_mismatch" if near else "missing_in_curated"
                frame_cf = e.get("frame")
                frame_cur = pick["calendar_frame"] if pick else None
                out.append(
                    Comparison(
                        cik=cik, metric=metric, accession=accession, form=form, tag=tag,
                        period_end=end.isoformat(), qtrs=qtrs, companyfacts_value=str(value),
                        curated_value=",".join(sorted(str(r["value"]) for r in near)) or None,
                        curated_period_end=pick["period_end"].isoformat() if pick else None,
                        outcome=outcome, frame_companyfacts=frame_cf, frame_curated=frame_cur,
                        frame_agrees=(frame_cf == frame_cur) if (frame_cf and pick) else None,
                    )
                )  # fmt: skip
    return out


def summarize(comparisons: list[Comparison]) -> dict[str, Any]:
    outcomes: dict[str, int] = {}
    for c in comparisons:
        outcomes[c.outcome] = outcomes.get(c.outcome, 0) + 1
    framed = [c for c in comparisons if c.frame_agrees is not None]
    return {
        "n_comparisons": len(comparisons),
        "expected_comparisons": EXPECTED_COMPARISONS,
        "outcomes": dict(sorted(outcomes.items())),
        "matched": outcomes.get("match", 0),
        "frames_compared": len(framed),
        "frames_agree": sum(1 for c in framed if c.frame_agrees),
        "unclassified_non_matches": sum(
            1 for c in comparisons if c.outcome != "match" and c.classification is None
        ),
    }


def curated_rows(spark: SparkSession, ciks: tuple[int, ...], table: str) -> list[dict[str, Any]]:
    df = spark.table(table).where(F.col("cik").isin(*ciks))
    cols = ["cik", "accession", "taxonomy", "tag", "period_end", "qtrs", "value", "calendar_frame"]
    return [r.asDict() for r in df.select(*cols).collect()]


def fetch_all(
    client: SecClient, companies: tuple[tuple[int, str], ...]
) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for cik, name_hint in companies:
        cf = client.get_json(companyfacts_url(cik))
        name = str(cf.get("entityName", ""))
        if name_hint not in name.lower():
            raise ValueError(f"CIK {cik} is {name!r}, expected a name containing {name_hint!r}")
        out[cik] = cf
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spot-check curated.facts against companyfacts.")
    parser.add_argument("--env", choices=["local", "emr"], default="local")
    parser.add_argument("--table", default="lake.curated.facts")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from dotenv import load_dotenv

    load_dotenv()
    ciks = tuple(c for c, _ in SPOT_COMPANIES)
    env: Env = args.env
    spark = spark_session(env, app_name="spotcheck_companyfacts")
    try:
        rows = curated_rows(spark, ciks, args.table)
        facts_snapshot = latest_snapshot_id(spark, args.table)
        with SecClient() as client:
            cfs = fetch_all(client, SPOT_COMPANIES)
    except (SecRequestError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        spark.stop()

    comparisons = compare(ciks, cfs, rows)
    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "table": args.table,
        "facts_snapshot_id": facts_snapshot,
        "date_tolerance_days": DATE_TOLERANCE_DAYS,
        "summary": summarize(comparisons),
        "comparisons": [asdict(c) for c in comparisons],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    s = result["summary"]
    print(f"{s['matched']}/{s['n_comparisons']} match; outcomes {s['outcomes']}; wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
