"""Submit a job to EMR Serverless and record what it cost (M01 section 3.8, T1/T2).

    uv run python -m pipelines.cloud.emr_run --job parse_filings [--arg ...] [--yes]

Without `--yes` it only prints the request it WOULD send and exits (the Makefile target "asks
first"). Claude never passes `--yes`: starting a run spends money (CLAUDE.md rule 8). With it, the
run is started, polled to a terminal state, and a metrics file is written to
`docs/milestones/job-metrics/<job>-<run id>.json`:

* state, wall time (`totalExecutionDurationSeconds`) and AWS's billed vCPU-hours, memory GB-hours
  and storage GB-hours;
* `cost_usd` ONLY if all three unit prices are supplied (`EDGARQA_EMR_PRICE_VCPU_HOUR`,
  `..._MEMORY_GB_HOUR`, `..._STORAGE_GB_HOUR`, or the flags). No price is built in: prices change
  and a number nobody measured must not appear in a report, so without them the cost is `null`.
* `usd_per_million_facts` when `--facts-rows` (the measured `curated.facts` row count) is given.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

PYTHON = "/usr/bin/python3.12"
TERMINAL = {"SUCCESS", "FAILED", "CANCELLED"}
DEFAULT_TIMEOUT_MINUTES = 240
METRICS_DIR = Path("docs/milestones/job-metrics")


@dataclass(frozen=True)
class RunConfig:
    application_id: str
    role_arn: str
    bucket: str
    git_sha: str
    job: str  # a module under pipelines/spark, e.g. "parse_filings"
    args: tuple[str, ...] = ("--env", "emr")
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES


def module_of(job: str) -> str:
    if not job.replace("_", "").isalnum():
        raise ValueError(f"bad job name {job!r}")
    return f"pipelines.spark.{job}"


def spark_submit_parameters(cfg: RunConfig) -> str:
    base = f"s3://{cfg.bucket}/artifacts/{cfg.git_sha}"
    conf = {
        # driver and executors must share a Python minor version (ADR-024)
        "spark.emr-serverless.driverEnv.PYSPARK_PYTHON": PYTHON,
        "spark.emr-serverless.driverEnv.PYSPARK_DRIVER_PYTHON": PYTHON,
        "spark.executorEnv.PYSPARK_PYTHON": PYTHON,
        # spark_session("emr") builds the lake path from this
        "spark.emr-serverless.driverEnv.EDGARQA_LAKE_BUCKET": cfg.bucket,
        "spark.executorEnv.EDGARQA_LAKE_BUCKET": cfg.bucket,
    }
    parts = [f"--py-files {base}/pipelines.zip", *(f"--conf {k}={v}" for k, v in conf.items())]
    return " ".join(parts)


def build_request(cfg: RunConfig) -> dict[str, Any]:
    base = f"s3://{cfg.bucket}/artifacts/{cfg.git_sha}"
    return {
        "applicationId": cfg.application_id,
        "executionRoleArn": cfg.role_arn,
        "name": f"{cfg.job}-{cfg.git_sha}",
        "executionTimeoutMinutes": cfg.timeout_minutes,
        "jobDriver": {
            "sparkSubmit": {
                "entryPoint": f"{base}/entrypoint.py",
                "entryPointArguments": [module_of(cfg.job), *cfg.args],
                "sparkSubmitParameters": spark_submit_parameters(cfg),
            }
        },
        "configurationOverrides": {
            "monitoringConfiguration": {
                "s3MonitoringConfiguration": {"logUri": f"s3://{cfg.bucket}/emr-logs/"}
            }
        },
        "tags": {"project": "edgarqa", "job": cfg.job},
    }


def cost_usd(billed: Mapping[str, Any], prices: Mapping[str, str | None]) -> Decimal | None:
    """vCPU-hours, memory GB-hours and storage GB-hours times their unit prices; None if any
    price is missing (never a guess)."""
    if any(prices.get(k) in (None, "") for k in ("vcpu_hour", "memory_gb_hour", "storage_gb_hour")):
        return None
    return (
        Decimal(str(billed.get("vCPUHour", 0))) * Decimal(str(prices["vcpu_hour"]))
        + Decimal(str(billed.get("memoryGBHour", 0))) * Decimal(str(prices["memory_gb_hour"]))
        + Decimal(str(billed.get("storageGBHour", 0))) * Decimal(str(prices["storage_gb_hour"]))
    )


def usd_per_million(cost: Decimal | None, rows: int | None) -> Decimal | None:
    if cost is None or not rows:
        return None
    return cost / (Decimal(rows) / Decimal(1_000_000))


def wait(
    client: Any,
    application_id: str,
    run_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 30.0,
    max_polls: int = 2000,
) -> dict[str, Any]:
    for _ in range(max_polls):
        run: dict[str, Any] = client.get_job_run(applicationId=application_id, jobRunId=run_id)[
            "jobRun"
        ]
        if run["state"] in TERMINAL:
            return run
        sleep(poll_seconds)
    raise TimeoutError(f"job run {run_id} did not finish after {max_polls} polls")


def metrics_record(
    cfg: RunConfig,
    run: Mapping[str, Any],
    prices: Mapping[str, str | None],
    facts_rows: int | None,
) -> dict[str, Any]:
    billed = dict(run.get("billedResourceUtilization") or {})
    cost = cost_usd(billed, prices)
    per_million = usd_per_million(cost, facts_rows)
    return {
        "job": cfg.job,
        "git_sha": cfg.git_sha,
        "application_id": cfg.application_id,
        "job_run_id": run["jobRunId"],
        "state": run["state"],
        "state_details": run.get("stateDetails"),
        "total_execution_duration_seconds": run.get("totalExecutionDurationSeconds"),
        "billed_resource_utilization": billed or None,
        "prices_usd": dict(prices) if cost is not None else None,
        "cost_usd": None if cost is None else str(cost),
        "facts_rows": facts_rows,
        "usd_per_million_facts": None if per_million is None else str(per_million),
        "note": None if cost is not None else "cost NOT MEASURED: unit prices were not supplied",
    }


def write_metrics(record: Mapping[str, Any], directory: Path = METRICS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['job']}-{record['job_run_id']}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _env(name: str, flag: str | None) -> str | None:
    return flag or os.environ.get(name)


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Submit an EMR Serverless job run.")
    parser.add_argument("--job", required=True)
    parser.add_argument("--arg", action="append", default=None, help="job argument, repeatable")
    parser.add_argument("--application-id")
    parser.add_argument("--role-arn")
    parser.add_argument("--bucket")
    parser.add_argument("--git-sha")
    parser.add_argument("--timeout-minutes", type=int, default=DEFAULT_TIMEOUT_MINUTES)
    parser.add_argument("--facts-rows", type=int, default=None)
    parser.add_argument("--price-vcpu-hour")
    parser.add_argument("--price-memory-gb-hour")
    parser.add_argument("--price-storage-gb-hour")
    parser.add_argument("--yes", action="store_true", help="actually start the run (spends money)")
    args = parser.parse_args(argv)

    values = {
        "application_id": _env("EDGARQA_EMR_APP_ID", args.application_id),
        "role_arn": _env("EDGARQA_EMR_ROLE_ARN", args.role_arn),
        "bucket": _env("EDGARQA_LAKE_BUCKET", args.bucket),
        "git_sha": args.git_sha or os.environ.get("EDGARQA_GIT_SHA"),
    }
    missing = [k for k, v in values.items() if not v]
    if missing:
        print(
            f"error: missing {', '.join(missing)} (flags or EDGARQA_* environment)", file=sys.stderr
        )
        return 2
    cfg = RunConfig(
        job=args.job,
        args=("--env", "emr", *(args.arg or ())),  # extra --arg values never replace --env emr
        timeout_minutes=args.timeout_minutes,
        **{k: str(v) for k, v in values.items()},
    )
    request = build_request(cfg)
    if not args.yes:
        print(json.dumps(request, indent=2))
        print("\ndry run: nothing was started (pass --yes to start this run and spend money)")
        return 0

    if client is None:
        import boto3

        client = boto3.client("emr-serverless")
    started = client.start_job_run(**request)
    run = wait(client, cfg.application_id, started["jobRunId"])
    prices = {
        "vcpu_hour": _env("EDGARQA_EMR_PRICE_VCPU_HOUR", args.price_vcpu_hour),
        "memory_gb_hour": _env("EDGARQA_EMR_PRICE_MEMORY_GB_HOUR", args.price_memory_gb_hour),
        "storage_gb_hour": _env("EDGARQA_EMR_PRICE_STORAGE_GB_HOUR", args.price_storage_gb_hour),
    }
    path = write_metrics(metrics_record(cfg, run, prices, args.facts_rows))
    print(f"{cfg.job}: {run['state']}; metrics in {path}")
    return 0 if run["state"] == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
