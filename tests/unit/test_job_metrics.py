"""Cost arithmetic for Glue job metrics (T2b). No AWS call: the functions take a `JobRun` dict."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from pipelines.cloud.job_metrics import (
    PRICING,
    billed_dpu_seconds,
    build_record,
    cost_usd,
    load_rate,
    usd_per_100k_chunks,
)

RATE = 0.44


def job_run(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "JobName": "edgarqa-embed-chunks",
        "Id": "jr_test",
        "JobRunState": "SUCCEEDED",
        "StartedOn": datetime(2026, 11, 22, 10, 0, 0, tzinfo=UTC),
        "CompletedOn": datetime(2026, 11, 22, 10, 10, 0, tzinfo=UTC),
        "ExecutionTime": 600,
        "DPUSeconds": 7200.0,
        "GlueVersion": "5.0",
        "WorkerType": "G.1X",
        "NumberOfWorkers": 12,
        "Timeout": 120,
        "Arguments": {"--max-chunks": "2000", "--JOB_NAME": "x", "--TempDir": "s3://t/"},
    }
    return base | kw


def test_the_pricing_file_holds_the_sourced_glue_5_rate():
    spec = yaml.safe_load(Path(PRICING).read_text(encoding="utf-8"))

    assert load_rate() == 0.44
    assert spec["usage_type"] == "USE2-ETL-DPU-Hour"
    assert spec["region"] == "us-east-2"
    assert spec["source_url"].startswith("https://aws.amazon.com/glue/pricing")
    assert spec["price_list_publication_date"]


def test_cost_is_dpu_hours_times_rate():
    assert cost_usd(7200.0, RATE) == pytest.approx(0.88)  # 2 DPU-hours x $0.44


def test_a_run_shorter_than_a_minute_is_billed_one_minute_per_dpu():
    assert billed_dpu_seconds(20.0, 4, "G.1X") == 240.0
    assert billed_dpu_seconds(20.0, 4, "G.2X") == 480.0
    assert billed_dpu_seconds(900.0, 4, "G.1X") == 900.0


def test_usd_per_100k_chunks_scales_the_run_cost_to_100k():
    assert usd_per_100k_chunks(0.88, 2_000) == pytest.approx(44.0)
    with pytest.raises(ValueError):
        usd_per_100k_chunks(1.0, 0)


def test_the_record_carries_cost_dpu_hours_and_the_t2b_fields():
    record = build_record(job_run(), RATE, note="smoke", chunks=60_000)

    assert record["job"] == "edgarqa-embed-chunks"
    assert record["state"] == "SUCCEEDED"
    assert record["dpu_hours"] == pytest.approx(2.0)
    assert record["cost_usd"] == pytest.approx(0.88)
    assert record["usd_per_100k_chunks"] == pytest.approx(0.88 / 60_000 * 100_000)
    assert record["chunks_per_minute_job"] == pytest.approx(6_000.0)
    assert record["started_on"].startswith("2026-11-22T10:00:00")
    assert record["rate_usd_per_dpu_hour"] == RATE


def test_glues_own_arguments_are_dropped_from_the_record():
    assert build_record(job_run(), RATE)["arguments"] == {"--max-chunks": "2000"}


def test_a_run_without_dpu_seconds_has_a_null_cost_not_a_guess():
    record = build_record(job_run(DPUSeconds=None, JobRunState="RUNNING"), RATE, chunks=100)

    assert record["cost_usd"] is None
    assert "usd_per_100k_chunks" not in record


def test_an_unknown_worker_type_is_an_error_not_one_dpu():
    with pytest.raises(ValueError):
        billed_dpu_seconds(100.0, 4, "G.025X")
    with pytest.raises(ValueError):
        billed_dpu_seconds(100.0, 4, None)


def test_the_t2b_fields_say_they_cover_the_whole_job_run():
    record = build_record(job_run(), RATE, chunks=60_000)

    assert "whole job run" in record["cost_scope"]
