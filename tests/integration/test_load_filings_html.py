"""Integration tests for ``pipelines/spark/load_filings_html.py`` (contract 2.1 raw.filings_html).

The fetcher's JSONL manifest is merged into ``raw.filings_html`` on ``accession``: reruns do not
duplicate rows, and a later fetch result (a retried failure) replaces the earlier row.
"""

from __future__ import annotations

import json
from pathlib import Path

from pyspark.sql import SparkSession

from pipelines.spark.load_filings_html import load_filings_html
from pipelines.spark.raw_schemas import FILINGS_HTML


def row(accession: str, form: str = "10-K", status: int = 200, **kw: object) -> dict[str, object]:
    ok = status == 200
    base: dict[str, object] = {
        "accession": accession,
        "cik": 320193,
        "form": form,
        "filed": "2023-11-03",
        "url": f"https://www.sec.gov/Archives/edgar/data/320193/{accession}/x.htm",
        "s3_uri": f"s3://lake/raw/filings/320193/{accession}.htm.gz" if ok else None,
        "bytes": 1234 if ok else None,
        "sha256": "ab" * 32 if ok else None,
        "http_status": status,
        "fetched_at": "2026-11-18T12:00:00Z",
    }
    return base | kw


def write_manifest(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_loads_every_manifest_row_with_the_contract_schema(
    spark: SparkSession, lake_namespace: str, tmp_path: Path
):
    manifest = write_manifest(
        tmp_path / "m.jsonl",
        [row("0000320193-23-000001"), row("0000320193-23-000002", "10-Q", 404)],
    )

    n = load_filings_html(spark, manifest, f"{lake_namespace}.filings_html")

    table = spark.table(f"{lake_namespace}.filings_html")
    assert n == 2
    assert [(f.name, f.dataType.simpleString()) for f in table.schema.fields] == [
        (f.name, f.dataType.simpleString()) for f in FILINGS_HTML.fields
    ]
    failed = table.where("http_status = 404").collect()[0]
    assert (failed.s3_uri, failed.bytes, failed.sha256) == (None, None, None)


def test_rerun_does_not_duplicate_and_a_retry_replaces_the_failed_row(
    spark: SparkSession, lake_namespace: str, tmp_path: Path
):
    table_name = f"{lake_namespace}.filings_html"
    manifest = tmp_path / "m.jsonl"
    write_manifest(manifest, [row("0000320193-23-000001", status=404)])
    load_filings_html(spark, manifest, table_name)
    load_filings_html(spark, manifest, table_name)
    assert spark.table(table_name).count() == 1

    write_manifest(manifest, [row("0000320193-23-000001", status=200)])
    load_filings_html(spark, manifest, table_name)

    rows = spark.table(table_name).collect()
    assert [(r.accession, r.http_status, r.bytes) for r in rows] == [
        ("0000320193-23-000001", 200, 1234)
    ]


def test_a_malformed_manifest_row_fails_the_load_instead_of_loading_nulls(
    spark: SparkSession, lake_namespace: str, tmp_path: Path
):
    manifest = tmp_path / "m.jsonl"
    manifest.write_text('{"accession": "a", "cik": "not a number"}\n', encoding="utf-8")

    try:
        load_filings_html(spark, manifest, f"{lake_namespace}.filings_html")
    except Exception:  # noqa: BLE001 - any Spark/Py4J error type is acceptable here
        return
    raise AssertionError("a malformed row was loaded")
