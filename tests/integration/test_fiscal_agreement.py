"""Integration test for ``eval/fiscal_agreement.py``: filing labels vs their facts' labels."""

from __future__ import annotations

from datetime import date

from pyspark.sql import SparkSession

from eval.fiscal_agreement import agreement
from pipelines.spark.curated_schemas import FACTS_HISTORY, FILINGS


def filing(acc: str, form: str, fy: int | None, fp: str | None, period: date):
    return (acc, 1, form, date(2023, 11, 3), None, period, fy, fp, "u", "s", "ok", None, 4, 9,
            "edgar_html", [])  # fmt: skip


def fact(acc: str, qtrs: int, period: date, fy: int, fp: str):
    row = {f.name: None for f in FACTS_HISTORY.fields}
    row |= {"accession": acc, "qtrs": qtrs, "period_end": period, "fiscal_year": fy,
            "fiscal_period": fp, "fact_key": hash((acc, qtrs, period, fp)) & 0xFFFFFF}  # fmt: skip
    return tuple(row[f.name] for f in FACTS_HISTORY.fields)


def test_only_reporting_period_facts_are_compared_and_comparatives_do_not_count(
    spark: SparkSession, lake_namespace: str
):
    ns = lake_namespace
    p23, p22 = date(2023, 9, 30), date(2022, 9, 24)
    spark.createDataFrame(
        [
            filing(
                "A", "10-K", 2023, "FY", p23
            ),  # agrees; its FY2022 comparative differs by design
            filing("B", "10-K", 2023, "FY", p23),  # its facts say 2024: a real disagreement
            filing("C", "10-Q", 2023, "Q3", date(2023, 7, 1)),  # no facts in the lake
        ],
        FILINGS,
    ).writeTo(f"{ns}.filings").using("iceberg").create()
    spark.createDataFrame(
        [
            fact("A", 4, p23, 2023, "FY"),
            fact("A", 0, p23, 2023, "FY"),
            fact("A", 4, p22, 2022, "FY"),
            fact("B", 4, p23, 2024, "FY"),
        ],
        FACTS_HISTORY,
    ).writeTo(f"{ns}.facts_history").using("iceberg").create()

    result = agreement(spark, ns)

    assert (result["filings"], result["with_facts_compared"], result["agree"]) == (3, 2, 1)
    assert result["without_facts_not_compared"] == 1
    [bad] = result["mismatches"]
    assert bad["accession"] == "B" and bad["filing_label"] == [2023, "FY"]
