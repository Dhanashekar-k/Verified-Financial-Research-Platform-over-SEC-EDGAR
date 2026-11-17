"""Data-quality report for the lakehouse (M01 section 3.6).

    uv run python -m pipelines.spark.dq_checks --env local

Writes `docs/milestones/M01-dq-report.md`. CLAUDE.md rule 1: every number in the report comes
from a query run here (or from `docs/milestones/M01-spotcheck.json`); anything that cannot be
measured yet (a table that does not exist, a Shekar-owned function that is not implemented) is
printed as `NOT MEASURED` with the reason, never estimated.

Sections: rows per source quarter and quarter gaps, null rates, unit-of-measure mix, custom-tag
share, segment/co-registrant share, fiscal-derivation flags, version counts, label-offset
distribution, companies whose fiscal-year end changed, the companyfacts spot check, and a raw
(pre-fiscal-logic) look at Apple FY2023 revenue to compare with the 10-K by hand.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyspark.sql import Column, DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from pipelines.common.fiscal_ref import SubmissionPeriod, label_offsets
from pipelines.common.spark_session import Env, spark_session
from pipelines.spark.iceberg_util import latest_snapshot_id

log = logging.getLogger(__name__)

RAW_NS = "lake.raw"
CURATED_NS = "lake.curated"
DEFAULT_REPORT = Path("docs/milestones/M01-dq-report.md")
SPOTCHECK_JSON = Path("docs/milestones/M01-spotcheck.json")
FIRST_FSDS_QUARTER = (2009, 2)
APPLE_CIK = 320193
REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)


@dataclass(frozen=True)
class NotMeasured:
    reason: str


Section = Any  # a JSON-friendly value, or NotMeasured


def measure(fn: Callable[[], Section]) -> Section:
    """Run one section; turn a missing table or unimplemented function into NOT MEASURED."""
    try:
        return fn()
    except NotImplementedError as exc:
        return NotMeasured(f"a Shekar-owned function is not implemented yet ({exc or 'stub'})")
    except Exception as exc:  # noqa: BLE001 - report the reason, keep the other sections
        msg = str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__
        return NotMeasured(f"{type(exc).__name__}: {msg}")


def quarter_range(first: str, last: str) -> list[str]:
    y, q = int(first[:4]), int(first[5])
    out = []
    while (y, q) <= (int(last[:4]), int(last[5])):
        out.append(f"{y}q{q}")
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


def rows_per_quarter(spark: SparkSession, raw_ns: str) -> dict[str, Any]:
    tables: dict[str, dict[str, int]] = {}
    for name in ("sub", "num", "tag", "pre"):
        df = spark.table(f"{raw_ns}.fsds_{name}").groupBy("src_quarter").count()
        tables[name] = {r.src_quarter: r["count"] for r in df.collect()}
    present = sorted(tables["num"])
    expected = quarter_range(present[0], present[-1]) if present else []
    return {
        "tables": tables,
        "first_quarter": present[0] if present else None,
        "last_quarter": present[-1] if present else None,
        "missing_quarters": [q for q in expected if q not in tables["num"]],
        "starts_at_2009q2": bool(present) and present[0] == "2009q2",
    }


def null_rates(df: DataFrame) -> dict[str, Any]:
    total = df.count()
    exprs = [F.sum(F.col(c).isNull().cast("int")).alias(c) for c in df.columns]
    nulls = df.agg(*exprs).first()
    assert nulls is not None
    return {
        "rows": total,
        "null_rate": {c: (nulls[c] or 0) / total if total else None for c in df.columns},
    }


def uom_distribution(num: DataFrame, top: int = 15) -> dict[str, Any]:
    total = num.count()
    rows = num.groupBy("uom").count().orderBy(F.col("count").desc()).limit(top).collect()
    return {
        "rows": total,
        "distinct_uom": num.select("uom").distinct().count(),
        "top": [{"uom": r.uom, "rows": r["count"], "share": r["count"] / total} for r in rows],
    }


def blank(col: str) -> Column:
    """NULL or empty: the same notion of 'absent' that normalize_facts uses."""
    return F.col(col).isNull() | (F.col(col) == "")


def raw_shares(num: DataFrame) -> dict[str, Any]:
    total = num.count()
    row = num.agg(
        F.sum((F.col("version") == F.col("adsh")).cast("int")).alias("custom"),
        F.sum((~blank("segments")).cast("int")).alias("segmented"),
        F.sum((~blank("coreg")).cast("int")).alias("coreg"),
        F.sum(F.col("value").isNull().cast("int")).alias("null_value"),
    ).first()
    assert row is not None
    return {
        "rows": total,
        **{k: {"rows": row[k], "share": row[k] / total} for k in ("custom", "segmented", "coreg")},
        "null_value": {"rows": row["null_value"], "share": row["null_value"] / total},
    }


def curated_summary(spark: SparkSession, curated_ns: str) -> dict[str, Any]:
    """Rows are `curated.facts` rows; versions and restatements are counted per FACT (fact_key),
    because a restated fact has two rows that would otherwise be counted twice."""
    facts = spark.table(f"{curated_ns}.facts")
    total = facts.count()
    flagged = facts.where(F.size("dq_flags") > 0).count()
    flags = facts.select(F.explode("dq_flags").alias("flag")).groupBy("flag").count().collect()
    per_fact = facts.groupBy("fact_key").agg(
        F.max("n_versions").alias("n_versions"),
        F.max(F.col("is_restated").cast("int")).alias("restated"),
    )
    n_facts = per_fact.count()
    versions = per_fact.groupBy("n_versions").count().orderBy("n_versions").collect()
    restated_facts = per_fact.where(F.col("restated") == 1).count()
    other = facts.agg(
        F.sum(F.col("is_custom").cast("int")).alias("custom"),
        F.sum(F.col("derived_from").isNotNull().cast("int")).alias("derived_q4"),
    ).first()
    assert other is not None
    return {
        "rows": total,
        "facts": n_facts,
        "rows_by_fiscal_year": {
            r.fiscal_year: r["count"] for r in facts.groupBy("fiscal_year").count().collect()
        },
        "null_rate": null_rates(facts)["null_rate"],
        "custom": {"rows": other["custom"], "share": other["custom"] / total if total else None},
        "restated_facts": restated_facts,
        "derived_q4": {"rows": other["derived_q4"]},
        "with_dq_flags": {"rows": flagged, "share": flagged / total if total else None},
        "flag_counts": {r.flag: r["count"] for r in flags},
        "n_versions_per_fact": {r.n_versions: r["count"] for r in versions},
    }


def fye_changes(spark: SparkSession, raw_ns: str, limit: int = 20) -> dict[str, Any]:
    sub = spark.table(f"{raw_ns}.fsds_sub").where(F.col("fye").isNotNull())
    grouped = sub.groupBy("cik").agg(
        F.collect_set("fye").alias("fyes"), F.max("name").alias("name")
    )
    changed = grouped.where(F.size("fyes") > 1)
    return {
        "companies": grouped.count(),
        "companies_with_changed_fye": changed.count(),
        "examples": [
            {"cik": r.cik, "name": r["name"], "fyes": sorted(r.fyes)}
            for r in changed.orderBy("cik").limit(limit).collect()
        ],
    }


def label_offset_distribution(spark: SparkSession, raw_ns: str) -> dict[str, Any]:
    latest = Window.partitionBy("adsh").orderBy(
        F.col("src_quarter").desc(), F.col("ingested_at").desc()
    )
    subs = (
        spark.table(f"{raw_ns}.fsds_sub")
        .withColumn("_rn", F.row_number().over(latest))  # one row per accession, as in normalize
        .where((F.col("_rn") == 1) & F.col("form").isin("10-K", "10-KT") & F.col("fye").isNotNull())
        .select("cik", "adsh", "form", "fy", "period", "fye")
        .collect()
    )
    periods = [
        SubmissionPeriod(
            cik=r.cik,
            adsh=r.adsh,
            form=r.form,
            sub_fy=r.fy,
            sub_period=datetime.strptime(str(r.period), "%Y%m%d").date(),
            fye_mmdd=r.fye,
        )
        for r in subs
    ]
    result = label_offsets(periods)  # Shekar-owned reference: NotImplementedError until written
    return {
        "companies": len(result),
        "offsets": dict(sorted(Counter(v.offset for v in result.values()).items())),
        "non_constant": sum(1 for v in result.values() if not v.is_constant),
    }


def apple_revenue_raw(spark: SparkSession, raw_ns: str) -> dict[str, Any]:
    """Apple FY2023 revenue straight from raw (no fiscal logic), to compare with the 10-K."""
    sub = spark.table(f"{raw_ns}.fsds_sub").where(
        (F.col("cik") == APPLE_CIK) & (F.col("form") == "10-K") & (F.col("period") == 20230930)
    )
    num = spark.table(f"{raw_ns}.fsds_num").where(
        F.col("tag").isin(*REVENUE_TAGS)
        & (F.col("ddate") == 20230930)
        & (F.col("qtrs") == 4)
        & blank("segments")
        & blank("coreg")
    )
    rows = (
        num.join(sub.select("adsh", "form", "filed"), "adsh")
        .select("adsh", "tag", "uom", "value", "form", "filed")
        .collect()
    )
    return {
        "rows": [
            {"adsh": r.adsh, "tag": r.tag, "uom": r.uom, "value": str(r.value), "form": r.form}
            for r in rows
        ]
    }


def apple_revenue_curated(spark: SparkSession, curated_ns: str) -> dict[str, Any]:
    df = spark.table(f"{curated_ns}.facts").where(
        (F.col("cik") == APPLE_CIK)
        & F.col("tag").isin(*REVENUE_TAGS)
        & (F.col("fiscal_year") == 2023)
        & (F.col("fiscal_period") == "FY")
    )
    return {
        "rows": [
            {
                "tag": r.tag,
                "uom": r.uom,
                "value": str(r.value),
                "accession": r.accession,
                "is_original": r.is_original,
                "is_latest": r.is_latest,
            }
            for r in df.collect()
        ]
    }


def drop_counts(spark: SparkSession, raw_ns: str) -> dict[str, Any]:
    """Raw `num` rows that `normalize_facts.prepare_facts` drops, by cause (not exclusive).

    `dropped_any` counts each dropped row once, whatever its causes. `no_fye` follows
    `prepare_facts`: the fye of the LATEST `sub` row of the accession (src_quarter, ingested_at),
    NULL or empty. A row with no `sub` row at all is `no_matching_sub`, not `no_fye`.
    """
    num = spark.table(f"{raw_ns}.fsds_num")
    latest = Window.partitionBy("adsh").orderBy(
        F.col("src_quarter").desc(), F.col("ingested_at").desc()
    )
    subs = (
        spark.table(f"{raw_ns}.fsds_sub")
        .withColumn("_rn", F.row_number().over(latest))
        .where(F.col("_rn") == 1)
        .select("adsh", blank("fye").alias("_sub_has_no_fye"))
    )
    # left join: a row without a `sub` row gets NULL here; with one, a non-null true/false
    flagged = (
        num.join(subs, "adsh", "left")
        .select(
            F.col("value").isNull().alias("null_value"),
            F.to_date(F.col("ddate").cast("string"), "yyyyMMdd").isNull().alias("bad_date"),
            F.col("_sub_has_no_fye").isNull().alias("no_sub"),
            F.coalesce(F.col("_sub_has_no_fye"), F.lit(False)).alias("no_fye"),
        )
        .withColumn(
            "any", F.col("null_value") | F.col("bad_date") | F.col("no_sub") | F.col("no_fye")
        )
    )
    row = flagged.agg(
        F.count(F.lit(1)).alias("rows"),
        *[F.sum(F.col(c).cast("int")).alias(c) for c in ("null_value", "bad_date", "no_sub")],
        F.sum(F.col("no_fye").cast("int")).alias("no_fye"),
        F.sum(F.col("any").cast("int")).alias("dropped_any"),
    ).first()
    assert row is not None
    return {
        "rows": row["rows"],
        "null_value": row["null_value"] or 0,
        "unparseable_ddate": row["bad_date"] or 0,
        "no_matching_sub": row["no_sub"] or 0,
        "no_fye": row["no_fye"] or 0,
        "dropped_any": row["dropped_any"] or 0,
    }


def snapshot_ids(spark: SparkSession, tables: list[str]) -> dict[str, Any]:
    return {t: latest_snapshot_id(spark, t) for t in tables}


def collect_metrics(
    spark: SparkSession,
    raw_ns: str = RAW_NS,
    curated_ns: str = CURATED_NS,
    spotcheck_path: Path = SPOTCHECK_JSON,
) -> dict[str, Any]:
    def spotcheck() -> Section:
        if not spotcheck_path.is_file():
            raise FileNotFoundError(
                f"{spotcheck_path} (run pipelines.spark.spotcheck_companyfacts)"
            )
        data = json.loads(spotcheck_path.read_text())
        current = latest_snapshot_id(spark, f"{curated_ns}.facts")
        return {
            "summary": data["summary"],
            "selection": data.get("selection"),
            "generated_at": data["generated_at"],
            "facts_snapshot_at_check": data.get("facts_snapshot_id"),
            "facts_snapshot_now": current,
            "stale": data.get("facts_snapshot_id") != current,
        }

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "snapshots": snapshot_ids(
            spark,
            [f"{raw_ns}.fsds_num", f"{curated_ns}.facts", f"{curated_ns}.facts_history"],
        ),
        "rows_per_quarter": measure(lambda: rows_per_quarter(spark, raw_ns)),
        "raw_num_null_rates": measure(lambda: null_rates(spark.table(f"{raw_ns}.fsds_num"))),
        "uom": measure(lambda: uom_distribution(spark.table(f"{raw_ns}.fsds_num"))),
        "raw_shares": measure(lambda: raw_shares(spark.table(f"{raw_ns}.fsds_num"))),
        "drop_counts": measure(lambda: drop_counts(spark, raw_ns)),
        "fye_changes": measure(lambda: fye_changes(spark, raw_ns)),
        "label_offsets": measure(lambda: label_offset_distribution(spark, raw_ns)),
        "curated_facts": measure(lambda: curated_summary(spark, curated_ns)),
        "spotcheck": measure(spotcheck),
        "apple_revenue_raw": measure(lambda: apple_revenue_raw(spark, raw_ns)),
        "apple_revenue_curated": measure(lambda: apple_revenue_curated(spark, curated_ns)),
    }


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


# --------------------------------------------------------------------------------------
# Rendering (pure: no Spark, so it can be unit-tested)
# --------------------------------------------------------------------------------------


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.2f}%"


def _nm(section: Section) -> str | None:
    return f"**NOT MEASURED**: {section.reason}" if isinstance(section, NotMeasured) else None


def _selection_text(selection: dict[str, Any] | None) -> str:
    """Which filings the spot check compared (recorded in M01-spotcheck.json)."""
    if not selection:
        return "the selection is not recorded in the result file"
    if selection.get("mode") == "fiscal_years":
        years = ", ".join(str(y) for y in selection["fiscal_years"])
        return f"the 10-Ks of fiscal years {years} (each company's own fy label)"
    return f"each company's {selection.get('n', '?')} most recent 10-Ks"


def render_report(m: dict[str, Any]) -> str:
    L: list[str] = []
    add = L.append
    add("# M1 data-quality report")
    add("")
    add(
        f"Generated {m['generated_at']} at git `{m['git_sha']}`. Every number below comes from a "
        "query run by `pipelines/spark/dq_checks.py` (or from `M01-spotcheck.json`); "
        "`NOT MEASURED` means it could not be computed yet, and the reason is given."
    )
    snaps = ", ".join(f"`{t}` = {s}" for t, s in m["snapshots"].items())
    add(f"\nIceberg snapshot ids (latest): {snaps}")

    add("\n## Rows per source quarter (`raw.fsds_*`)")
    rq = m["rows_per_quarter"]
    if (msg := _nm(rq)) is not None:
        add(msg)
    else:
        add(f"\nQuarters present: {rq['first_quarter']} to {rq['last_quarter']}.")
        add(
            f"Starts at 2009q2: **{rq['starts_at_2009q2']}**. Missing quarters in range: "
            f"**{rq['missing_quarters'] or 'none'}**.\n"
        )
        add("| quarter | sub | num | tag | pre |")
        add("|---|---:|---:|---:|---:|")
        for q in sorted(rq["tables"]["num"]):
            cells = " | ".join(
                f"{rq['tables'][t].get(q, 0):,}" for t in ("sub", "num", "tag", "pre")
            )
            add(f"| {q} | {cells} |")  # fmt: skip

    add("\n## Null rates, `raw.fsds_num`")
    nr = m["raw_num_null_rates"]
    if (msg := _nm(nr)) is not None:
        add(msg)
    else:
        add(f"\n{nr['rows']:,} rows.\n")
        add("| column | null rate |")
        add("|---|---:|")
        for col, rate in nr["null_rate"].items():
            add(f"| `{col}` | {pct(rate)} |")

    add("\n## Unit-of-measure mix, `raw.fsds_num`")
    u = m["uom"]
    if (msg := _nm(u)) is not None:
        add(msg)
    else:
        add(f"\n{u['distinct_uom']:,} distinct units; top {len(u['top'])}:\n")
        add("| uom | rows | share |")
        add("|---|---:|---:|")
        for r in u["top"]:
            add(f"| `{r['uom']}` | {r['rows']:,} | {pct(r['share'])} |")

    add("\n## Custom tags, segments, co-registrants, NULL values (`raw.fsds_num`)")
    s = m["raw_shares"]
    if (msg := _nm(s)) is not None:
        add(msg)
    else:
        add("\n| what | rows | share |")
        add("|---|---:|---:|")
        for key, label in (
            ("custom", "custom tag (`version == adsh`)"),
            ("segmented", "segmented (`segments` set)"),
            ("coreg", "co-registrant (`coreg` set)"),
            ("null_value", "`value` is NULL (dropped from curated)"),
        ):
            add(f"| {label} | {s[key]['rows']:,} | {pct(s[key]['share'])} |")

    add("\n## Raw `num` rows that `normalize_facts` drops (a row can have more than one cause)")
    d = m["drop_counts"]
    if (msg := _nm(d)) is not None:
        add(msg)
    else:
        add("\n| cause | rows | share |")
        add("|---|---:|---:|")
        for key, label in (
            ("null_value", "`value` is NULL"),
            ("unparseable_ddate", "`ddate` is not a valid yyyymmdd date"),
            ("no_matching_sub", "no `sub` row for the accession"),
            ("no_fye", "no fiscal year end (`sub.fye` NULL or empty)"),
            ("dropped_any", "**dropped for at least one cause** (each row once)"),
        ):
            add(f"| {label} | {d[key]:,} | {pct(d[key] / d['rows'] if d['rows'] else None)} |")
        add(f"\nOf {d['rows']:,} raw rows.")

    add("\n## Companies whose fiscal-year end changed (`sub.fye`)")
    f = m["fye_changes"]
    if (msg := _nm(f)) is not None:
        add(msg)
    else:
        add(
            f"\n{f['companies_with_changed_fye']:,} of {f['companies']:,} companies have more "
            "than one `fye` in the loaded quarters."
        )
        for ex in f["examples"]:
            add(f"- CIK {ex['cik']} {ex['name']}: {', '.join(ex['fyes'])}")

    add("\n## Label-offset distribution (rule 3)")
    lo = m["label_offsets"]
    if (msg := _nm(lo)) is not None:
        add(msg)
    else:
        add(
            f"\n{lo['companies']:,} companies; offsets: {lo['offsets']}; "
            f"non-constant: {lo['non_constant']:,}."
        )

    add("\n## Curated facts (`curated.facts`)")
    c = m["curated_facts"]
    if (msg := _nm(c)) is not None:
        add(msg)
    else:
        add(
            f"\n{c['rows']:,} rows covering {c['facts']:,} distinct facts (fact_key). "
            f"Custom: {pct(c['custom']['share'])} of rows. "
            f"With dq flags: {pct(c['with_dq_flags']['share'])} of rows "
            f"({c['with_dq_flags']['rows']:,}). "
            f"Derived Q4 rows: {c['derived_q4']['rows']:,}."
        )
        add(f"\nFlag counts (rows): {c['flag_counts'] or 'none'}.")
        add(
            f"Restated facts: {c['restated_facts']:,}. "
            f"Versions per fact (`n_versions`, counted once per fact): {c['n_versions_per_fact']}."
        )
        by_year = sorted(
            c["rows_by_fiscal_year"].items(), key=lambda kv: (kv[0] is None, kv[0] or 0)
        )
        add(
            "\nRows by fiscal year: "
            + ", ".join(f"{'NULL' if y is None else y}: {n:,}" for y, n in by_year)
            + "."
        )
        add("\nNull rates:\n")
        add("| column | null rate |")
        add("|---|---:|")
        for col, rate in c["null_rate"].items():
            add(f"| `{col}` | {pct(rate)} |")

    add("\n## Second-source spot check (companyfacts)")
    sc = m["spotcheck"]
    if (msg := _nm(sc)) is not None:
        add(msg)
    else:
        sm = sc["summary"]
        if sc["stale"]:
            add(
                "\n> **STALE**: `curated.facts` has changed since this spot check ran "
                f"(snapshot {sc['facts_snapshot_at_check']} then, {sc['facts_snapshot_now']} now). "
                "Re-run `pipelines.spark.spotcheck_companyfacts` before relying on these numbers."
            )
        if sm["n_comparisons"] != sm["expected_comparisons"]:
            add(
                f"\n> **INCOMPLETE**: {sm['n_comparisons']} comparisons were made, "
                f"{sm['expected_comparisons']} were expected "
                "(a company had fewer than 2 annual filings)."
            )
        add(f"\nCompared: {_selection_text(sc.get('selection'))}.")
        add(
            f"\n{sm['matched']}/{sm['n_comparisons']} match (outcomes {sm['outcomes']}). "
            f"Frames agree: {sm['frames_agree']}/{sm['frames_compared']}. "
            f"Non-matches without a classification: {sm['unclassified_non_matches']}. "
            f"Run at {sc['generated_at']}; details in `M01-spotcheck.json`."
        )

    add("\n## Apple FY2023 revenue (manual check against the 10-K income statement)")
    add(
        "\nPeriod timing note: the 7-day tolerance in rule 2 matters for companyfacts dates, not "
        "for FSDS: FSDS rounds `ddate`/`period` to the nearest month end."
    )
    apple_sections = (
        ("apple_revenue_raw", "From `raw.fsds_num` (no fiscal logic)"),
        ("apple_revenue_curated", "From `curated.facts` (fiscal_year 2023, FY)"),
    )
    for key, label in apple_sections:
        sec = m[key]
        add(f"\n**{label}:**")
        if (msg := _nm(sec)) is not None:
            add(msg)
        elif not sec["rows"]:
            add("no rows found.")
        else:
            for r in sec["rows"]:
                add(
                    f"- `{r['tag']}` = {r['value']} {r['uom']}, "
                    f"accession {r.get('adsh', r.get('accession'))}"
                )
    add(
        "\nCompare these with the 10-K income statement by hand; this report does not assert "
        "a match.\n"
    )
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the M1 data-quality report.")
    parser.add_argument("--env", choices=["local", "emr"], default="local")
    parser.add_argument("--raw-namespace", default=RAW_NS)
    parser.add_argument("--curated-namespace", default=CURATED_NS)
    parser.add_argument("--out", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    env: Env = args.env
    spark = spark_session(env, app_name="dq_checks")
    try:
        metrics = collect_metrics(spark, args.raw_namespace, args.curated_namespace)
    finally:
        spark.stop()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(metrics), encoding="utf-8")
    not_measured = [k for k, v in metrics.items() if isinstance(v, NotMeasured)]
    print(f"wrote {out}; NOT MEASURED sections: {not_measured or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
