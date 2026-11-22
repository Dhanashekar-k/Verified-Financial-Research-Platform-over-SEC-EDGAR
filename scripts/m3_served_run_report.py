"""M3 stage A DoD numbers for the served embedding run, from AWS (read-only except one rerun job).

    eval "$(aws configure export-credentials --profile edgarqa --format env)"; unset AWS_PROFILE
    python -m scripts.m3_served_run_report --runs jr_1,jr_2,jr_3,jr_4 --cache /path/cache.json

Steps, each cached in `--cache` so an expired AWS session only loses the step in flight:
  1. Athena DoD queries over lake.curated.chunk_embeddings (completeness over the embedded scope,
     dim, unit norm, duplicates, orphans, the deliberate 10-Q exclusion).
  2. The per-wave stats the job wrote to S3 and the `get_job_run` cost records.
  3. Per-core inference rates from the executors' `embed_partition` log lines.
  4. A rerun of the job on 2 workers (a real, small run): it must select and embed 0 chunks.
The result is merged into `docs/milestones/M03-embed-metrics.json` under `served_run`. Every number
there comes from Athena, S3 or CloudWatch; nothing is estimated.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from pipelines.cloud.job_metrics import build_record, load_rate
from scripts.check_cross_arch import athena_rows

BUCKET = "edgarqa-lake-019593222039"
JOB = "edgarqa-embed-chunks"
METRICS = Path("docs/milestones/M03-embed-metrics.json")
SCOPE = "(c.form IN ('10-K','10-K/A') OR c.doc_source = 'financebench_pdf')"
EXPECTED_IN_SCOPE = 1_097_376  # Athena, 2026-11-21 (ADR-037)
EXPECTED_TINY_IN_SCOPE = 15_915  # ADR-035 pre-flight count

QUERIES: dict[str, str] = {
    "shape": (
        "SELECT count(*), count(DISTINCT chunk_id), min(dim), max(dim), "
        "min(cardinality(embedding)), max(cardinality(embedding)), "
        "count(DISTINCT model_name), min(model_name), "
        "count_if(embedded_at IS NULL) FROM chunk_embeddings"
    ),
    "scope": (
        "SELECT count(*), count_if(e.chunk_id IS NULL), "
        "count_if(e.chunk_id IS NOT NULL AND c.n_tokens <= 5) "
        f"FROM chunks c LEFT JOIN chunk_embeddings e ON c.chunk_id = e.chunk_id WHERE {SCOPE}"
    ),
    "orphans": (
        "SELECT count_if(c.chunk_id IS NULL), count_if(c.chunk_id IS NOT NULL AND NOT "
        f"{SCOPE}) FROM chunk_embeddings e LEFT JOIN chunks c ON c.chunk_id = e.chunk_id"
    ),
    "not_embedded_10q": (
        "SELECT count(*), count_if(e.chunk_id IS NOT NULL) FROM chunks c "
        "LEFT JOIN chunk_embeddings e ON c.chunk_id = e.chunk_id "
        "WHERE c.doc_source = 'edgar_html' AND c.form IN ('10-Q','10-Q/A')"
    ),
    "norms": (
        "WITH n AS (SELECT sqrt(reduce("
        "transform(embedding, x -> CAST(x AS double) * CAST(x AS double)), "
        "CAST(0 AS double), (s, v) -> s + v, s -> s)) AS nrm FROM chunk_embeddings) "
        "SELECT count(*), min(nrm), max(nrm), count_if(abs(nrm - 1) > 0.001), "
        "count_if(is_nan(nrm) OR is_infinite(nrm)) FROM n"
    ),
}


def load_cache(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_cache(path: Path, cache: dict[str, Any]) -> None:
    path.write_text(json.dumps(cache, indent=1, sort_keys=True, default=str), encoding="utf-8")


def step(cache: dict[str, Any], path: Path, name: str, fn: Any) -> Any:
    if name not in cache:
        print(f"  running {name} ...", flush=True)
        cache[name] = fn()
        save_cache(path, cache)
    return cache[name]


def run_athena(name: str) -> list[str]:
    return athena_rows(QUERIES[name])[0]


def s3_json(key: str) -> dict[str, Any]:
    import boto3

    body = boto3.client("s3").get_object(Bucket=BUCKET, Key=key)["Body"].read()
    return json.loads(body)  # type: ignore[no-any-return]


def partition_lines(run: str) -> list[str]:
    """Every `embed_partition` line the executors of one run wrote to stderr."""
    import boto3

    logs = boto3.client("logs")
    group = "/aws-glue/jobs/error"
    streams = logs.describe_log_streams(logGroupName=group, logStreamNamePrefix=f"{run}_")
    lines: list[str] = []
    for st in streams["logStreams"]:
        token: str | None = None
        while True:
            kw: dict[str, Any] = {"logGroupName": group, "logStreamName": st["logStreamName"]}
            kw |= {"startFromHead": True, "limit": 10000}
            if token:
                kw["nextToken"] = token
            page = logs.get_log_events(**kw)
            lines += [
                e["message"].strip() for e in page["events"] if "embed_partition" in e["message"]
            ]
            if not page["events"] or page.get("nextForwardToken") == token:
                break
            token = page["nextForwardToken"]
    return lines


def per_core(lines: list[str]) -> dict[str, float]:
    rows = [int(re.search(r"rows=(\d+)", x).group(1)) for x in lines]  # type: ignore[union-attr]
    infer = [float(re.search(r"infer_s=([\d.]+)", x).group(1)) for x in lines]  # type: ignore[union-attr]
    rates = [r / i for r, i in zip(rows, infer, strict=True) if i > 0]
    return {
        "partition_lines": len(lines),
        "rows": sum(rows),
        "chunks_per_second_per_core": sum(rows) / sum(infer),
        "min_partition": min(rates),
        "max_partition": max(rates),
    }


def rerun_job() -> dict[str, Any]:
    import boto3

    glue = boto3.client("glue")
    args = {
        "--scope": "served",
        "--batch-size": "32",
        "--metrics-out": f"s3://{BUCKET}/metrics/embed_chunks/served-rerun.json",
    }
    run = glue.start_job_run(
        JobName=JOB, NumberOfWorkers=2, WorkerType="G.1X", Timeout=15, Arguments=args
    )
    run_id = run["JobRunId"]
    while True:
        state = glue.get_job_run(JobName=JOB, RunId=run_id)["JobRun"]["JobRunState"]
        if state not in {"STARTING", "RUNNING", "WAITING", "STOPPING"}:
            break
        time.sleep(20)
    out = {"run_id": run_id, "state": state}
    if state == "SUCCEEDED":
        out["stats"] = s3_json("metrics/embed_chunks/served-rerun.json")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--runs", required=True, help="the four wave job-run ids, comma separated")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--skip-rerun", action="store_true")
    args = parser.parse_args(argv)
    runs = args.runs.split(",")
    cache, path = load_cache(args.cache), args.cache

    import boto3

    glue = boto3.client("glue")
    rate = load_rate()
    out: dict[str, Any] = {"waves": []}
    for n, run in enumerate(runs, start=1):
        stats = step(
            cache,
            path,
            f"wave{n}_stats",
            lambda n=n: s3_json(f"metrics/embed_chunks/served-wave{n}.json"),
        )
        job_run = step(
            cache,
            path,
            f"wave{n}_jobrun",
            lambda run=run: glue.get_job_run(JobName=JOB, RunId=run)["JobRun"],
        )
        for key in ("StartedOn", "CompletedOn"):  # json round trip of datetimes
            if isinstance(job_run.get(key), str):
                from datetime import datetime

                job_run[key] = datetime.fromisoformat(job_run[key])
        record = build_record(job_run, rate, f"M3 served run wave {n}", stats["chunks_embedded"])
        rates = step(cache, path, f"wave{n}_rates", lambda run=run: per_core(partition_lines(run)))
        out["waves"].append({"run": run, "stats": stats, "record": record, "per_core": rates})
        Path(f"docs/milestones/job-metrics/{JOB}-{run}.json").write_text(
            json.dumps(record, indent=1) + "\n", encoding="utf-8"
        )

    dod = {
        name: step(cache, path, f"athena_{name}", lambda name=name: run_athena(name))
        for name in QUERIES
    }
    out["dod"] = dod
    if not args.skip_rerun:
        out["rerun"] = step(cache, path, "rerun", rerun_job)

    chunks = sum(w["stats"]["chunks_embedded"] for w in out["waves"])
    cost = sum(w["record"]["cost_usd"] for w in out["waves"])
    dpu_h = sum(w["record"]["dpu_hours"] for w in out["waves"])
    wall = sum(w["record"]["execution_seconds"] for w in out["waves"])
    stage = sum(w["stats"]["seconds_embedding"] for w in out["waves"])
    rows = sum(w["per_core"]["rows"] for w in out["waves"])
    infer_rate = sum(w["per_core"]["rows"] for w in out["waves"]) / sum(
        w["per_core"]["rows"] / w["per_core"]["chunks_per_second_per_core"] for w in out["waves"]
    )
    out["measured"] = {
        "chunks_embedded": chunks,
        "dpu_hours": dpu_h,
        "cost_usd": cost,
        "usd_per_100k_chunks": cost / chunks * 100_000,
        "chunks_per_minute_whole_runs": chunks / wall * 60,
        "chunks_per_minute_embedding_stage": chunks / stage * 60,
        "chunks_per_second_per_core_inference": infer_rate,
        "partition_rows_checked": rows,
        "rate_usd_per_dpu_hour": rate,
        "cost_explorer_reconcile": {
            "usage_type": "USE2-ETL-DPU-Hour",
            "expect_dpu_hours_from_these_runs": dpu_h,
            "note": "reconcile UsageQuantity a day later; credits make the dollar field 0",
        },
    }
    metrics = json.loads(METRICS.read_text(encoding="utf-8"))
    metrics["served_run"] = out
    METRICS.write_text(
        json.dumps(metrics, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: out[k] for k in ("dod", "rerun", "measured") if k in out}, indent=1, default=str
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
