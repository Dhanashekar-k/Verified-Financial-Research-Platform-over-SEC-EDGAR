"""M1 benchmark: DuckDB vs local Spark on the same FSDS quarters (M01 section 3.7, ADR-007).

    uv run python -m pipelines.duckdb.benchmark run --quarters 2023q3,2023q4 --label 2q

For one set of quarters it runs, each in its own subprocess so wall time and memory are measured
per run:

* DuckDB: `normalize_facts_duckdb` straight from the gzipped TSVs (`sub` and `num` only);
* local Spark: `fsds_to_iceberg` into a scratch namespace (all four FSDS files, as the pipeline
  loads them), then `normalize_facts` from those Iceberg tables.

Then it exports the Spark tables to Parquet (not timed) and compares the two engines' outputs row
for row (`compare_outputs`), and merges everything into `docs/milestones/M01-benchmark.json`.
That file is the only source of the benchmark numbers in ADR-007 and the /dod report.

Peak memory is the largest SUM of resident set sizes over the whole process tree (sampled every
`poll_seconds`), because a Spark job is a Python driver plus a JVM. It is indicative, not exact:
a peak between two samples is missed (understates) and pages shared between processes are
counted once per process (overstates). Wall time is one run per cell on a laptop doing
nothing else that the harness controls; it is not a statistical benchmark.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import resource
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

log = logging.getLogger(__name__)

TABLES = ("facts_history", "facts", "facts_dimensional")
HASH_COLUMNS = ("fact_key", "fact_id", "derived_from")
DEFAULT_RESULTS = Path("docs/milestones/M01-benchmark.json")
SCRATCH = Path("warehouse/bench")  # under the gitignored warehouse directory


# --------------------------------------------------------------------------------------
# measuring a command
# --------------------------------------------------------------------------------------


def tree_rss_kb(root_pid: int, ps_output: str) -> int:
    """Total resident set size (KB) of `root_pid` and all its descendants.

    `ps_output` is the output of `ps -axo pid=,ppid=,rss=`.
    """
    rss: dict[int, int] = {}
    children: dict[int, list[int]] = {}
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        pid, ppid, kb = (int(p) for p in parts)
        rss[pid] = kb
        children.setdefault(ppid, []).append(pid)
    if root_pid not in rss:
        return 0
    total, stack = 0, [root_pid]
    while stack:
        pid = stack.pop()
        total += rss.get(pid, 0)
        stack.extend(children.get(pid, []))
    return total


@dataclass(frozen=True)
class Measurement:
    wall_seconds: float
    peak_rss_mb: float
    exit_code: int


def _children_maxrss_mb() -> float:
    """High-water RSS of every child this process has ever reaped (bytes on macOS, KB on Linux)."""
    raw = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def measure(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    log_path: Path | None = None,
    poll_seconds: float = 0.25,
) -> Measurement:
    """Run `cmd`; return wall time, peak process-tree RSS (MB) and the exit code.

    Never raises: a command that cannot start has exit code 127. Wall time ends the moment the
    child exits (the sampler waits on the process, it does not sleep between polls). For a child
    that exits before the first sample, the kernel's high-water mark is used only if this child
    raised it, so an earlier big child is never credited to a later small one.
    """
    out = open(log_path, "wb") if log_path else subprocess.DEVNULL  # noqa: SIM115
    peak_kb = 0
    before = _children_maxrss_mb()
    start = time.perf_counter()
    try:
        try:
            proc = subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT)
        except OSError:
            return Measurement(0.0, 0.0, 127)
        while True:
            ps = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True, check=False
            ).stdout
            peak_kb = max(peak_kb, tree_rss_kb(proc.pid, ps))
            try:
                proc.wait(timeout=poll_seconds)
                break
            except subprocess.TimeoutExpired:
                continue
        wall = time.perf_counter() - start
    finally:
        if log_path:
            out.close()  # type: ignore[union-attr]
    peak_mb = peak_kb / 1024
    if peak_mb == 0:  # finished before the first sample
        after = _children_maxrss_mb()
        peak_mb = after if after > before else 0.0
    return Measurement(round(wall, 2), round(peak_mb, 1), proc.returncode)


# --------------------------------------------------------------------------------------
# comparing the two engines' outputs
# --------------------------------------------------------------------------------------


def _parquet(directory: Path, table: str) -> str:
    """A DuckDB `read_parquet` path: `table.parquet`, or a directory of Spark part files."""
    single = directory / f"{table}.parquet"
    if single.is_file():
        return str(single)
    parts = directory / table
    if parts.is_dir() and any(parts.glob("*.parquet")):
        return str(parts / "*.parquet")
    raise FileNotFoundError(f"no {table} Parquet in {directory}")


def _normalized_sql(directory: Path, table: str, columns: list[str], has_parents: bool) -> str:
    """The table without its hash columns, with `derived_from` replaced by the parents' natural
    keys (period_end|qtrs|accession), resolved inside the same engine's output."""
    lookup_table = "facts_dimensional" if table == "facts_dimensional" else "facts_history"
    select = ", ".join(f't."{c}"' for c in columns)
    if not has_parents:  # facts_dimensional has no derived_from column in the contract
        return f"SELECT {select} FROM read_parquet('{_parquet(directory, table)}') AS t"
    return f"""
WITH t AS (SELECT * FROM read_parquet('{_parquet(directory, table)}')),
lk AS (
  SELECT fact_id,
         concat_ws('|', CAST(period_end AS VARCHAR), CAST(qtrs AS VARCHAR), accession) AS nk
  FROM read_parquet('{_parquet(directory, lookup_table)}')
),
ex AS (
  SELECT fact_id AS rid, i, derived_from[i + 1] AS pid FROM (
    SELECT fact_id, derived_from, unnest(range(len(derived_from))) AS i
    FROM t WHERE derived_from IS NOT NULL)
),
par AS (
  SELECT ex.rid, list(lk.nk ORDER BY ex.i) AS parents
  FROM ex LEFT JOIN lk ON lk.fact_id = ex.pid GROUP BY ex.rid
)
SELECT {select}, par.parents FROM t LEFT JOIN par ON t.fact_id = par.rid
"""


def compare_outputs(dir_a: Path, dir_b: Path) -> dict[str, dict[str, int]]:
    """Row-for-row comparison of two engines' outputs, per table.

    Every column is compared except `fact_key`, `fact_id` (hash values differ between engines)
    and `derived_from`, which is compared as the parents' natural keys. Returns for each table
    `rows_a`, `rows_b` and `only_a` / `only_b` (rows of one side with no equal row on the other,
    counted as a multiset: `EXCEPT ALL`).
    """
    for directory in (dir_a, dir_b):
        for table in TABLES:
            _parquet(directory, table)  # raises FileNotFoundError early
    con = duckdb.connect()
    result: dict[str, dict[str, int]] = {}
    try:
        for table in TABLES:
            head = con.execute(f"SELECT * FROM read_parquet('{_parquet(dir_a, table)}') LIMIT 0")
            all_columns = [d[0] for d in head.description]
            columns = [c for c in all_columns if c not in HASH_COLUMNS]
            has_parents = "derived_from" in all_columns
            a = _normalized_sql(dir_a, table, columns, has_parents)
            b = _normalized_sql(dir_b, table, columns, has_parents)

            def count(sql: str) -> int:
                return int(con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0])  # type: ignore[index]

            result[table] = {
                "rows_a": count(a),
                "rows_b": count(b),
                "only_a": count(f"SELECT * FROM ({a}) EXCEPT ALL SELECT * FROM ({b})"),
                "only_b": count(f"SELECT * FROM ({b}) EXCEPT ALL SELECT * FROM ({a})"),
            }
    finally:
        con.close()
    return result


# --------------------------------------------------------------------------------------
# the benchmark run
# --------------------------------------------------------------------------------------


def _python(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", module, *args]


def _git_sha() -> str:
    """The short commit, with `+dirty` when code under pipelines/, tests/ or the Makefile has
    uncommitted changes (so a result file says whether its commit is the code that ran)."""
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    )
    sha = out.stdout.strip() or "unknown"
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "pipelines", "tests", "Makefile"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    return f"{sha}+dirty" if status.stdout.strip() else sha


def _machine() -> dict[str, Any]:
    def sysctl(key: str) -> str | None:
        out = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, check=False)
        return out.stdout.strip() or None

    import pyspark

    return {
        "platform": platform.platform(),
        "cpu": sysctl("machdep.cpu.brand_string"),
        "cpu_count": os.cpu_count(),
        "memory_bytes": int(sysctl("hw.memsize") or 0) or None,
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "pyspark": pyspark.__version__,
    }


def export_spark(raw_ns: str, curated_ns: str, out_dir: Path, drop: bool) -> None:
    """Write the three Spark curated tables as Parquet; optionally drop the scratch namespaces."""
    from pipelines.common.spark_session import spark_session

    spark = spark_session("local", app_name="benchmark_export")
    try:
        for table in TABLES:
            spark.table(f"{curated_ns}.{table}").write.mode("overwrite").parquet(
                str(out_dir / table)
            )
        if drop:
            for ns in (raw_ns, curated_ns):
                for row in spark.sql(f"SHOW TABLES IN {ns}").collect():
                    spark.sql(f"DROP TABLE IF EXISTS {ns}.{row.tableName} PURGE")
                spark.sql(f"DROP NAMESPACE IF EXISTS {ns}")
    finally:
        spark.stop()


def run_benchmark(
    quarters: list[str], label: str, src: Path, results_path: Path, runs_dir: Path
) -> dict[str, Any]:
    q = ",".join(quarters)
    work = runs_dir / label
    work.mkdir(parents=True, exist_ok=True)
    duck_out, spark_out = work / "duckdb", work / "spark"
    raw_ns, cur_ns = f"lake.bench_raw_{label}", f"lake.bench_cur_{label}"
    runs: list[dict[str, Any]] = []
    for stale in (duck_out, spark_out):  # never compare against the output of an earlier run
        shutil.rmtree(stale, ignore_errors=True)

    def record(engine: str, step: str, cmd: list[str]) -> Measurement:
        log.info("%s %s: %s", engine, step, " ".join(cmd))
        m = measure(cmd, log_path=work / f"{engine}_{step}.log")
        runs.append({"label": label, "quarters": quarters, "engine": engine, "step": step}
                    | asdict(m))  # fmt: skip
        log.info("  -> %.1f s, peak %.0f MB, exit %d", m.wall_seconds, m.peak_rss_mb, m.exit_code)
        return m

    duck_ok = record(
        "duckdb",
        "normalize",
        _python(
            "pipelines.duckdb.normalize_facts_duckdb",
            "--src", str(src), "--quarters", q, "--out", str(duck_out),
        ),
    ).exit_code == 0  # fmt: skip
    ok = record(
        "spark_local",
        "raw_load",
        _python(
            "pipelines.spark.fsds_to_iceberg",
            "--env", "local", "--quarters", q, "--src", str(src), "--namespace", raw_ns,
        ),
    ).exit_code == 0  # fmt: skip
    if ok:
        ok = record(
            "spark_local",
            "normalize",
            _python(
                "pipelines.spark.normalize_facts",
                "--env", "local", "--raw-namespace", raw_ns, "--curated-namespace", cur_ns,
            ),
        ).exit_code == 0  # fmt: skip

    ok = ok and duck_ok  # compare only when BOTH engines produced fresh output
    comparison: dict[str, Any] | None = None
    if ok:
        subprocess.run(
            _python(
                "pipelines.duckdb.benchmark", "export-spark",
                "--raw-namespace", raw_ns, "--curated-namespace", cur_ns,
                "--out", str(spark_out), "--drop",
            ),
            check=True,
        )  # fmt: skip
        comparison = compare_outputs(spark_out, duck_out)  # a = Spark, b = DuckDB

    existing: dict[str, Any] = (
        json.loads(results_path.read_text()) if results_path.is_file() else {}
    )
    kept_runs = [r for r in existing.get("runs", []) if r["label"] != label]
    comparisons = {k: v for k, v in existing.get("comparisons", {}).items() if k != label}
    if comparison is not None:
        comparisons[label] = comparison
    result = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "machine": _machine(),
        "note": (
            "One run per cell. Memory is the peak sum of RSS over the process tree, sampled: "
            "indicative (a missed peak understates, shared pages overstate). "
            "Spark path = fsds_to_iceberg (sub, num, tag, pre) + normalize_facts; "
            "DuckDB reads sub and num only."
        ),
        "runs": kept_runs + runs,
        "comparisons": comparisons,
    }
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DuckDB vs local Spark on FSDS quarters.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="time both engines and compare their outputs")
    run.add_argument("--quarters", required=True, help="comma-separated, e.g. 2023q3,2023q4")
    run.add_argument("--label", required=True, help="short name of this cell, e.g. 2q")
    run.add_argument("--src", type=Path, default=Path("data/raw/fsds"))
    run.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    run.add_argument("--runs-dir", type=Path, default=SCRATCH)

    exp = sub.add_parser("export-spark", help="(internal) write Spark curated tables as Parquet")
    exp.add_argument("--raw-namespace", required=True)
    exp.add_argument("--curated-namespace", required=True)
    exp.add_argument("--out", type=Path, required=True)
    exp.add_argument("--drop", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.command == "export-spark":
        export_spark(args.raw_namespace, args.curated_namespace, args.out, args.drop)
        return 0

    result = run_benchmark(
        [x.strip() for x in args.quarters.split(",")],
        args.label,
        args.src,
        args.results,
        args.runs_dir,
    )
    failed = [r for r in result["runs"] if r["label"] == args.label and r["exit_code"] != 0]
    diffs = {
        t: c for t, c in result["comparisons"].get(args.label, {}).items()
        if c["only_a"] or c["only_b"]
    }  # fmt: skip
    if failed:
        log.error("failed runs: %s", [(r["engine"], r["step"], r["exit_code"]) for r in failed])
    if diffs:
        log.error("the engines disagree: %s", diffs)
    return 1 if failed or diffs else 0


if __name__ == "__main__":
    raise SystemExit(main())
