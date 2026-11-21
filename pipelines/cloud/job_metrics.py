"""Turn one AWS Glue job run into a `docs/milestones/job-metrics/*.json` record with its cost.

    python -m pipelines.cloud.job_metrics --job edgarqa-embed-chunks --run jr_... --note "smoke"
    python -m pipelines.cloud.job_metrics --job ... --run ... --chunks 2000     # adds T2b numbers

The record is read from `glue:GetJobRun` (a read-only call). The dollar cost is
`dpu_seconds / 3600 x rate`, where the rate comes from `infra/pricing/glue.yaml` (AWS Price List,
us-east-2, Glue 5.0 ETL). Nothing here estimates: a run that has no `DPUSeconds` yet gets
`cost_usd: null`. Reconcile against Cost Explorer a day later on `UsageQuantity` (DPU-hours).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

PRICING = Path("infra/pricing/glue.yaml")
OUT_DIR = Path("docs/milestones/job-metrics")
DPU_PER_WORKER = {"G.1X": 1, "G.2X": 2, "G.4X": 4, "G.8X": 8}
MIN_BILLED_SECONDS = 60  # Glue bills per second with a one-minute minimum per run


def load_rate(path: Path = PRICING) -> float:
    """USD per DPU-hour from the pricing file."""
    return float(yaml.safe_load(path.read_text(encoding="utf-8"))["rate_usd_per_dpu_hour"])


def billed_dpu_seconds(dpu_seconds: float, workers: int | None, worker_type: str | None) -> float:
    """`DPUSeconds` of the run, raised to the one-minute minimum when the run was shorter."""
    if worker_type not in DPU_PER_WORKER:
        raise ValueError(
            f"unknown Glue worker type {worker_type!r}; add its DPU count to the table"
        )
    dpus = (workers or 0) * DPU_PER_WORKER[worker_type]
    return max(float(dpu_seconds), float(MIN_BILLED_SECONDS * dpus))


def cost_usd(billed_seconds: float, rate_usd_per_dpu_hour: float) -> float:
    return billed_seconds / 3600.0 * rate_usd_per_dpu_hour


def usd_per_100k_chunks(cost: float, chunks: int) -> float:
    """T2b: dollars to embed 100,000 chunks at this run's cost per chunk."""
    if chunks <= 0:
        raise ValueError("chunks must be > 0")
    return cost / chunks * 100_000


def build_record(
    job_run: dict[str, Any], rate: float, note: str = "", chunks: int | None = None
) -> dict[str, Any]:
    """The job-metrics record for a `GetJobRun` result (the `JobRun` object)."""
    dpu_seconds = job_run.get("DPUSeconds")
    workers = job_run.get("NumberOfWorkers")
    worker_type = job_run.get("WorkerType")
    started, completed = job_run.get("StartedOn"), job_run.get("CompletedOn")
    record: dict[str, Any] = {
        "job": job_run["JobName"],
        "job_run_id": job_run["Id"],
        "note": note,
        "state": job_run.get("JobRunState"),
        "started_on": started.isoformat() if started else None,
        "completed_on": completed.isoformat() if completed else None,
        "execution_seconds": job_run.get("ExecutionTime"),
        "dpu_seconds": dpu_seconds,
        "glue_version": job_run.get("GlueVersion"),
        "worker_type": worker_type,
        "number_of_workers": workers,
        "timeout_minutes": job_run.get("Timeout"),
        "arguments": {k: v for k, v in (job_run.get("Arguments") or {}).items() if _public(k)},
        "rate_usd_per_dpu_hour": rate,
        "cost_usd": None,
    }
    if dpu_seconds is not None:
        billed = billed_dpu_seconds(dpu_seconds, workers, worker_type)
        record["billed_dpu_seconds"] = billed
        record["dpu_hours"] = billed / 3600.0
        record["cost_usd"] = cost_usd(billed, rate)
        if chunks is not None:
            record["chunks"] = chunks
            # Whole job run: startup, pip install, the table scan and model load all count, so a
            # small smoke run overstates the cost per chunk at scale (an upper bound, not T2b).
            record["cost_scope"] = "whole job run including startup and fixed overhead"
            record["usd_per_100k_chunks"] = usd_per_100k_chunks(record["cost_usd"], chunks)
            if record["execution_seconds"]:
                record["chunks_per_minute_job"] = chunks / record["execution_seconds"] * 60
    return record


def _public(argument: str) -> bool:
    """Job arguments worth keeping: ours, not Glue's own plumbing (`--TempDir`, ...)."""
    return argument not in {"--JOB_NAME", "--TempDir", "--enable-metrics", "--job-bookmark-option"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--job", required=True)
    parser.add_argument("--run", required=True, help="job run id, jr_...")
    parser.add_argument("--note", default="")
    parser.add_argument("--chunks", type=int, help="chunks embedded by the run (adds T2b fields)")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    import boto3

    run = boto3.client("glue").get_job_run(JobName=args.job, RunId=args.run)["JobRun"]
    record = build_record(run, load_rate(), args.note, args.chunks)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{args.job}-{args.run}.json"
    out.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
