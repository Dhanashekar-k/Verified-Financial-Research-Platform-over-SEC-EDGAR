"""Integration tests for ``pipelines/spark/export_companies.py``.

The exporter turns ``curated.companies`` into the CIK list the filing fetcher reads
(``data/targets/tracked_ciks.csv``): S&P 500 members only, one row per CIK, sorted, atomic.
"""

from __future__ import annotations

import csv
from pathlib import Path

from pyspark.sql import SparkSession

from pipelines.spark.curated_schemas import COMPANIES
from pipelines.spark.export_companies import export_tracked_ciks

ROWS = [
    (320193, "Apple Inc.", "AAPL", 3571, "0930", True, None, []),
    (789019, "MICROSOFT CORP", "MSFT", 7372, "0630", True, None, []),
    (1067983, "BERKSHIRE", "BRK-B", 6331, "1231", True, None, ["BRK-A"]),
    (999001, "Not In Index", "NOPE", None, None, False, None, []),
    (888002, "Unflagged", "UNF", None, None, None, None, []),
]


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_exports_only_sp500_members_sorted_by_cik(
    spark: SparkSession, lake_namespace: str, tmp_path: Path
):
    spark.createDataFrame(ROWS, COMPANIES).writeTo(f"{lake_namespace}.companies").using(
        "iceberg"
    ).create()
    dest = tmp_path / "targets" / "tracked_ciks.csv"

    n = export_tracked_ciks(spark, lake_namespace, dest)

    assert n == 3
    assert [r["cik"] for r in read(dest)] == ["320193", "789019", "1067983"]
    assert list(read(dest)[0]) == ["cik", "ticker", "name"]


def test_rerun_is_byte_identical_and_leaves_no_temp_files(
    spark: SparkSession, lake_namespace: str, tmp_path: Path
):
    spark.createDataFrame(ROWS, COMPANIES).writeTo(f"{lake_namespace}.companies").using(
        "iceberg"
    ).create()
    dest = tmp_path / "tracked_ciks.csv"

    export_tracked_ciks(spark, lake_namespace, dest)
    first = dest.read_bytes()
    export_tracked_ciks(spark, lake_namespace, dest)

    assert dest.read_bytes() == first
    assert [p.name for p in tmp_path.iterdir()] == ["tracked_ciks.csv"]


def test_one_row_per_cik_even_if_the_table_has_duplicates(
    spark: SparkSession, lake_namespace: str, tmp_path: Path
):
    dup = [*ROWS, (320193, "Apple Inc.", "AAPL", 3571, "0930", True, None, [])]
    spark.createDataFrame(dup, COMPANIES).writeTo(f"{lake_namespace}.companies").using(
        "iceberg"
    ).create()
    dest = tmp_path / "tracked_ciks.csv"

    assert export_tracked_ciks(spark, lake_namespace, dest) == 3
