"""Builders for small raw FSDS tables used by the Spark integration tests."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.types import StringType, StructField, StructType, TimestampType

from pipelines.spark.raw_schemas import NUM_SOURCE, RAW_SUB_COLUMNS, SUB_SOURCE

A_K = "0000320193-23-000106"  # the 10-K
A_Q = "0000320193-23-000077"  # a 10-Q
INGESTED = datetime(2026, 11, 17, 12, 0, 0)

NUM_RAW = StructType(
    [
        *NUM_SOURCE.fields,
        StructField("src_quarter", StringType()),
        StructField("ingested_at", TimestampType()),
    ]
)
SUB_RAW = StructType(
    [
        *[f for f in SUB_SOURCE.fields if f.name in RAW_SUB_COLUMNS],
        StructField("src_quarter", StringType()),
        StructField("ingested_at", TimestampType()),
    ]
)


def num_df(spark: SparkSession, rows: list[dict[str, Any]]) -> DataFrame:
    base: dict[str, Any] = {
        "adsh": A_K,
        "tag": "Revenues",
        "version": "us-gaap/2023",
        "ddate": 20230930,
        "qtrs": 4,
        "uom": "USD",
        "segments": None,
        "coreg": None,
        "value": Decimal("100.0000"),
        "footnote": None,
        "src_quarter": "2023q4",
        "ingested_at": INGESTED,
    }
    data = [tuple((base | r)[f.name] for f in NUM_RAW.fields) for r in rows]
    return spark.createDataFrame(data, NUM_RAW)


def sub_df(spark: SparkSession, rows: list[dict[str, Any]] | None = None) -> DataFrame:
    base: dict[str, Any] = {
        "adsh": A_K,
        "cik": 320193,
        "name": "Apple Inc.",
        "sic": 3571,
        "countryba": "US",
        "fye": "0930",
        "form": "10-K",
        "period": 20230930,
        "fy": 2023,
        "fp": "FY",
        "filed": 20231103,
        "accepted": INGESTED,
        "prevrpt": 0,
        "afs": "1-LAF",
        "wksi": 1,
        "instance": "aapl-20230930_htm.xml",
        "src_quarter": "2023q4",
        "ingested_at": INGESTED,
    }
    rows = rows if rows is not None else [{}]
    data = [tuple((base | r)[f.name] for f in SUB_RAW.fields) for r in rows]
    return spark.createDataFrame(data, SUB_RAW)
