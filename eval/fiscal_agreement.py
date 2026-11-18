"""Do a filing's fiscal labels equal the labels of its own facts? (ADR-023, measured on the lake)

    uv run python -m eval.fiscal_agreement --env local
        [--out docs/milestones/M02-fiscal-agreement.json]

For every `curated.filings` row that also has facts in `curated.facts_history` for the SAME
accession, take the reporting-period facts only (`period_end = period_of_report`; annual and
instant facts for a 10-K, quarter and instant facts for a 10-Q: a filing's comparatives belong to
other years by design) and compare their `(fiscal_year, fiscal_period)` with the filing's.
Filings with no facts in the lake (outside the loaded FSDS quarters) are counted, not compared.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from pipelines.common.spark_session import Env, spark_session

DEFAULT_OUT = Path("docs/milestones/M02-fiscal-agreement.json")
CURATED = "lake.curated"


def agreement(spark: SparkSession, curated_ns: str = CURATED) -> dict[str, Any]:
    filings = spark.table(f"{curated_ns}.filings").where(F.col("doc_source") == "edgar_html")
    facts = spark.table(f"{curated_ns}.facts_history")
    is_10k = F.col("f.form").startswith("10-K")
    wanted_qtrs = F.when(is_10k, F.col("h.qtrs").isin(4, 0)).otherwise(F.col("h.qtrs").isin(1, 0))
    joined = (
        filings.alias("f")
        .join(
            facts.alias("h"),
            (F.col("h.accession") == F.col("f.accession"))
            & (F.col("h.period_end") == F.col("f.period_of_report"))
            & wanted_qtrs,
            "left",
        )
        .groupBy("f.accession", "f.form", "f.fiscal_year", "f.fiscal_period")
        .agg(
            F.count("h.fact_key").alias("n_facts"),
            F.collect_set(F.struct("h.fiscal_year", "h.fiscal_period")).alias("fact_labels"),
        )
        .collect()
    )
    compared = [r for r in joined if r["n_facts"] > 0]
    mismatches = []
    for r in compared:
        labels = {(x["fiscal_year"], x["fiscal_period"]) for x in r["fact_labels"]}
        if labels != {(r["fiscal_year"], r["fiscal_period"])}:
            mismatches.append(
                {
                    "accession": r["accession"],
                    "form": r["form"],
                    "filing_label": [r["fiscal_year"], r["fiscal_period"]],
                    "fact_labels": sorted(map(list, labels), key=str),
                }
            )
    return {
        "filings": len(joined),
        "with_facts_compared": len(compared),
        "without_facts_not_compared": len(joined) - len(compared),
        "agree": len(compared) - len(mismatches),
        "mismatches": mismatches,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare filing labels with their facts' labels.")
    parser.add_argument("--env", choices=["local", "emr"], default="local")
    parser.add_argument("--namespace", default=CURATED)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    env: Env = args.env
    spark = spark_session(env, app_name="fiscal_agreement")
    try:
        result = agreement(spark, args.namespace)
    finally:
        spark.stop()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"{result['agree']}/{result['with_facts_compared']} filings with facts agree with their "
        f"facts' labels ({result['without_facts_not_compared']} have no facts in the lake)"
    )
    return 0 if not result["mismatches"] else 1


if __name__ == "__main__":
    sys.exit(main())
