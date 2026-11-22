"""Swap / rollback correctness on the REAL tables (M03 section 3.3, DoD "rollback tested").

Runs on the serving box after version 1 is loaded and live. It then:
  1. loads version 2 from the same export WITHOUT indexes (so no second HNSW build; labelled in
     the output) and swaps it in;
  2. rolls back to version 1;
  3. reloads version 2 (now the previous version) and checks it replaced, not appended;
  4. swaps back to 2 and rolls back again, to show the pair really exchanges;
while a reader thread selects from the `chunks` view in a loop the whole time. After every step it
records what the views point at, `corpus_state`, which versioned tables exist, and row counts.
The output is JSON; nothing is asserted by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import psycopg

from serving.load.load_snapshot import load_version, rollback, state, swap


def view_target(conn: psycopg.Connection[Any], view: str) -> str:
    row = conn.execute(f"SELECT tableoid::regclass::text FROM {view} LIMIT 1").fetchone()
    return str(row[0]) if row else "empty"


def snapshot_state(dsn: str, step: str) -> dict[str, Any]:
    pattern = "^(facts|chunks|companies)_v[0-9]+$"
    with psycopg.connect(dsn, autocommit=True) as conn:
        tables = sorted(
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE tablename ~ %s", (pattern,)
            )
        )

        def count(relation: str) -> int:
            return int(conn.execute(f"SELECT count(*) FROM {relation}").fetchone()[0])  # type: ignore[index]

        return {
            "step": step,
            "corpus_state": state(dsn),
            "chunks_view_reads": view_target(conn, "chunks"),
            "facts_view_reads": view_target(conn, "facts"),
            "chunks_rows": count("chunks"),
            "facts_rows": count("facts"),
            "companies_rows": count("companies"),
            "versioned_tables": tables,
        }


class Reader(threading.Thread):
    """Selects one row from each view in a loop and records the tables it saw and any error."""

    def __init__(self, dsn: str) -> None:
        super().__init__(daemon=True)
        self.dsn = dsn
        self.stop = threading.Event()
        self.seen: set[tuple[str, str]] = set()
        self.errors: list[str] = []
        self.reads = 0

    def run(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            while not self.stop.is_set():
                try:
                    self.seen.add((view_target(conn, "chunks"), view_target(conn, "facts")))
                    self.reads += 1
                except psycopg.Error as exc:
                    self.errors.append(str(exc)[:200])
                time.sleep(0.02)


def timed(fn: Any) -> tuple[Any, float]:
    started = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - started


def run(dsn: str, source: str) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    reader = Reader(dsn)
    reader.start()
    try:
        steps.append(snapshot_state(dsn, "start: version 1 live"))
        report, load_s = timed(
            lambda: load_version(dsn, version=2, source=source, build_indexes=False)
        )
        steps.append(
            snapshot_state(dsn, "loaded version 2 (no indexes), not swapped")
            | {"seconds": load_s, "rows": report.rows}
        )
        _, s = timed(lambda: swap(dsn, 2))
        steps.append(snapshot_state(dsn, "swap(2)") | {"seconds": s})
        new_live, s = timed(lambda: rollback(dsn))
        steps.append(snapshot_state(dsn, "rollback()") | {"seconds": s, "returned": new_live})
        report, load_s = timed(
            lambda: load_version(dsn, version=2, source=source, build_indexes=False)
        )
        steps.append(
            snapshot_state(dsn, "reloaded version 2 (previous)")
            | {"seconds": load_s, "rows": report.rows}
        )
        _, s = timed(lambda: swap(dsn, 2))
        steps.append(snapshot_state(dsn, "swap(2) again") | {"seconds": s})
        new_live, s = timed(lambda: rollback(dsn))
        steps.append(snapshot_state(dsn, "rollback() again") | {"seconds": s, "returned": new_live})
    finally:
        reader.stop.set()
        reader.join(timeout=5)
    return {
        "note": "version 2 is the same export loaded without indexes; version 1 keeps all indexes",
        "steps": steps,
        "reader": {
            "reads": reader.reads,
            "errors": reader.errors,
            "distinct_view_targets_seen": sorted([list(x) for x in reader.seen]),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(os.environ["EDGARQA_PG_DSN"], args.source)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for step in result["steps"]:
        print(
            step["step"],
            "| state",
            step["corpus_state"],
            "| chunks reads",
            step["chunks_view_reads"],
            "| tables",
            step["versioned_tables"],
        )
    print("reader:", result["reader"]["reads"], "reads,", len(result["reader"]["errors"]), "errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
