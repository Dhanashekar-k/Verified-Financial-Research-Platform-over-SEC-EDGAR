# ruff: noqa: E501 - generated sentences are long f-strings
"""Assemble docs/milestones/M03-serving-bench.json and M03-spend.json from the raw M3 results.

    python -m scripts.assemble_m03_serving --raw <dir with the files fetched from the box> \
        --stopped-at 2026-11-23T01:00:00Z

Raw inputs (all produced by measurements on the box or by AWS):
  bench_m16.json, bench_m32.json, bench_final.json, halfvec_check.json, rebuild.json,
  rebuild_index.txt, bench.json (the first, superseded run), bench_final_cache_*.json,
  and the job-metrics files under docs/milestones/job-metrics/.
Nothing is estimated except where a sentence says so; a missing input is an error.
"""

from __future__ import annotations

import argparse
import glob
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DIR = Path("docs/milestones")
GATE = 0.95
EC2_USD_PER_HOUR = 0.09576  # m7i-flex.large us-east-2, AWS Price List 2026-11-21
GP3_USD_PER_GB_MONTH = 0.08  # AWS Price List 2026-11-21
IPV4_USD_PER_HOUR = 0.005  # docs/plan/04_aws_and_cost_plan.md
VOLUME_GB = 60


def jload(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def choose_default_ef(curve: dict[str, dict[str, Any]]) -> tuple[int | None, str]:
    """Smallest ef_search whose conservative recall reaches the gate; the sentence says how sure."""
    for ef in sorted(curve, key=int):
        c = curve[ef]
        if c["recall_at_10"] >= GATE:
            lo, hi = c["recall_at_10_ci95"]
            sure = (
                "its 95% CI lies above 0.95"
                if lo >= GATE
                else f"its 95% CI [{lo:.3f}, {hi:.3f}] still includes values below 0.95"
            )
            return int(ef), (
                f"{ef} is the smallest tested ef_search whose recall@10 on the harder query set reaches {GATE} "
                f"({c['recall_at_10']:.4f}; {sure}); dense search there takes p50 {c['p50_ms']:.1f} ms, p95 {c['p95_ms']:.1f} ms "
                "on a Free-plan flex instance (preliminary). Lower settings are cheaper but miss the gate: "
                + ", ".join(
                    f"ef {k}: {v['recall_at_10']:.3f}" for k, v in curve.items() if int(k) < int(ef)
                )
                + "."
            )
    return None, "no tested ef_search reaches the gate"


def assemble_bench(raw: Path) -> dict[str, Any]:
    m16, m32, final = (
        jload(raw / "bench_m16.json"),
        jload(raw / "bench_m32.json"),
        jload(raw / "bench_final.json"),
    )
    first = jload(raw / "bench.json")
    rebuild = jload(raw / "rebuild.json")
    load = jload(DIR / "M03-serving-load.json")
    default_ef, why = choose_default_ef(m32["hnsw_curve"])
    ef_key = str(default_ef) if default_ef else None
    return {
        "instance_type": final["instance_type"],
        "server": final["server"],
        "queries": final["queries"],
        "seed": final["seed"],
        "latency_note": m32.get("latency_note"),
        "index": {
            "final": {
                "m": 32,
                "ef_construction": 128,
                "build_seconds": rebuild["seconds"],
                "size_bytes": int(
                    (raw / "rebuild_index.txt")
                    .read_text(encoding="utf-8")
                    .strip()
                    .splitlines()[-1]
                    .split("|")[-1]
                ),
                "maintenance_work_mem": rebuild["maintenance_work_mem"],
            },
            "original": {
                "m": 16,
                "ef_construction": 64,
                "build_seconds": load["load_report"]["seconds"]["index_chunks_hnsw"],
                "size_bytes": load["load_report"]["sizes_bytes"]["index:chunks_v1_hnsw"],
            },
        },
        "exact_scan_seconds_per_query": m32["exact_scan_seconds_per_query"],
        "hnsw_curve": m32["hnsw_curve"],
        "hnsw_curve_original_index": m16["hnsw_curve"],
        "chosen_default_ef_search": default_ef,
        "chosen_default_justification": why,
        "numeric_lookup": final.get("numeric_lookup"),
        "hybrid": final.get("hybrid"),
        "hybrid_lexical_mode": final.get("hybrid_lexical_mode"),
        "hybrid_note": "ef_search values in this table are 200 and 800; the lexical leg is Postgres full-text search with ts_rank_cd over every matching row, which is the known cost (not fixed in M3).",
        "query_instruction_ab": final.get("query_instruction_ab"),
        "filtered_dense": final.get("filtered_dense"),
        "cpu_steal_fraction_whole_run": final.get("cpu_steal_fraction_whole_run"),
        "cpu_steal_fraction_of_busy_time": final.get("cpu_steal_fraction_of_busy_time"),
        "halfvec_check": jload(raw / "halfvec_check.json"),
        "superseded_first_run": {
            "note": "first full bench on the m=16/64 index: non-deterministic query sample, cold first ef_search, hybrid with an OR lexical query (stress shape)",
            "hnsw_curve": first["hnsw_curve"],
            "hybrid_or_mode": first["hybrid"],
            "numeric_lookup": first["numeric_lookup"],
            "filtered_dense": first["filtered_dense"],
        },
        "default_ef_dense_latency_ms": None
        if ef_key is None
        else {k: m32["hnsw_curve"][ef_key][k] for k in ("p50_ms", "p95_ms")},
    }


def assemble_spend(stopped_at: str) -> dict[str, Any]:
    recs = [jload(Path(f)) for f in glob.glob(str(DIR / "job-metrics" / "*.json"))]
    m3 = [r for r in recs if r["job"] in {"edgarqa-embed-chunks", "edgarqa-export-serving"}]
    groups = {
        "Glue: embedding smoke runs (4, one failed)": [
            r
            for r in m3
            if r["job"] == "edgarqa-embed-chunks"
            and ("smoke" in r["note"].lower() or "M3 smoke" in r["note"])
        ],
        "Glue: embedding served run (4 waves)": [
            r for r in m3 if r["note"].startswith("M3 served run wave")
        ],
        "Glue: embedding rerun idempotency check": [
            r for r in m3 if r["note"].startswith("M3 served rerun")
        ],
        "Glue: serving export (2 runs)": [r for r in m3 if r["job"] == "edgarqa-export-serving"],
    }
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, rs in groups.items():
        usd = sum(r["cost_usd"] or 0 for r in rs)
        seen |= {r["job_run_id"] for r in rs}
        items.append(
            {
                "item": name,
                "usd": usd,
                "dpu_hours": sum(r["dpu_hours"] or 0 for r in rs),
                "runs": len(rs),
            }
        )
    other = [r for r in m3 if r["job_run_id"] not in seen]
    if other:
        items.append(
            {
                "item": "Glue: other M3 runs",
                "usd": sum(r["cost_usd"] or 0 for r in other),
                "runs": len(other),
            }
        )
    launch = datetime(2026, 11, 22, 18, 51, 28, tzinfo=UTC)
    stop = datetime.fromisoformat(stopped_at.replace("Z", "+00:00"))
    hours = (stop - launch).total_seconds() / 3600.0
    ec2 = hours * EC2_USD_PER_HOUR
    ebs = VOLUME_GB * GP3_USD_PER_GB_MONTH * hours / 730.0
    ip = hours * IPV4_USD_PER_HOUR
    items += [
        {
            "item": f"EC2 m7i-flex.large, {hours:.2f} instance-hours at ${EC2_USD_PER_HOUR}/h (Price List rate x launch-to-stop hours; not from the bill)",
            "usd": ec2,
        },
        {
            "item": f"EBS gp3 {VOLUME_GB} GB while running ({hours:.2f} h; billing continues at about $4.80/month while stopped)",
            "usd": ebs,
        },
        {"item": f"Public IPv4 {hours:.2f} h at ${IPV4_USD_PER_HOUR}/h", "usd": ip},
    ]
    total = sum(i["usd"] for i in items)
    return {
        "items": items,
        "total_m3_usd": total,
        "instance": {
            "launch_utc": launch.isoformat(),
            "stopped_utc": stop.isoformat(),
            "hours": hours,
        },
        "reconciliation": (
            "Glue lines are `dpu_seconds / 3600 x $0.44` from `get_job_run` (job-metrics files). Cost Explorer for 2026-11-22 UTC showed 42.59 DPU-hours = $18.74 before credits against the job-metrics sum of 41.44 + 1.13 (the export runs) = 42.57; the EC2, EBS and IPv4 lines are estimates from list prices and the instance's launch-to-stop time and are not in Cost Explorer yet (it lags about a day). "
            "Account-wide, Cost Explorer on 2026-11-23 (before credits): November usage $31.18 = Glue $30.79 (69.97 DPU-hours x $0.44, M1 and M2 included) + $0.40 other (Cost Explorer $0.08, Athena $0.12, RDS $0.01, S3 $0.19; not separable by milestone); credits remaining $168.82 (`aws freetier get-account-plan-state`), consistent with $200.00 minus $31.18. EC2 does not appear in Cost Explorer yet."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--stopped-at", required=True, help="when the box was stopped, UTC ISO")
    args = parser.parse_args(argv)
    (DIR / "M03-serving-bench.json").write_text(
        json.dumps(assemble_bench(args.raw), indent=1) + "\n", encoding="utf-8"
    )
    (DIR / "M03-spend.json").write_text(
        json.dumps(assemble_spend(args.stopped_at), indent=1) + "\n", encoding="utf-8"
    )
    print("written M03-serving-bench.json and M03-spend.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
