"""Summarise the box's resource sampler (`/opt/edgarqa/sample.csv`) over a time window.

The sampler (see serving/box notes in the M3 report) appends one line every 10 s:
`ts,cpu:<fields of the first /proc/stat line>,mem_available_kb,swap_free_kb,disk_used_kb`.
This reports CPU busy and steal over the window, the lowest available memory, swap and the peak
disk use. Everything comes from those samples; a window with fewer than two samples is an error.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def parse_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rec in csv.DictReader(text.splitlines()):
        try:
            cpu = [int(x) for x in rec["cpu_stat"].split(":")[2:]]
            if len(cpu) < 8:
                continue
            rows.append(
                {
                    "ts": int(rec["ts"]),
                    "cpu": cpu,
                    "mem_available_kb": int(rec["mem_available_kb"]),
                    "swap_free_kb": int(rec["swap_free_kb"]),
                    "disk_used_kb": int(rec["disk_used_kb"]),
                }
            )
        except (KeyError, ValueError, TypeError):
            continue  # a torn last line
    return rows


def summarize(rows: list[dict[str, Any]], start: int, end: int) -> dict[str, Any]:
    window = [r for r in rows if start <= r["ts"] <= end]
    if len(window) < 2:
        raise ValueError(f"fewer than two samples between {start} and {end}")
    first, last = window[0]["cpu"], window[-1]["cpu"]
    total = sum(last) - sum(first)
    idle = (last[3] - first[3]) + (last[4] - first[4])
    steal = last[7] - first[7]
    busy = total - idle
    return {
        "samples": len(window),
        "seconds": window[-1]["ts"] - window[0]["ts"],
        "cpu_busy_fraction": busy / total if total else None,
        "steal_fraction": steal / total if total else None,
        "busy_steal_fraction": steal / busy if busy > 0 else None,
        "min_mem_available_mib": min(r["mem_available_kb"] for r in window) / 1024,
        "swap_free_min_mib": min(r["swap_free_kb"] for r in window) / 1024,
        "peak_disk_used_gb": max(r["disk_used_kb"] for r in window) / 1024 / 1024,
        "disk_used_gb_at_start": window[0]["disk_used_kb"] / 1024 / 1024,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--start", type=int, required=True, help="unix seconds")
    parser.add_argument("--end", type=int, required=True, help="unix seconds")
    args = parser.parse_args(argv)
    rows = parse_rows(args.csv.read_text(encoding="utf-8"))
    print(json.dumps(summarize(rows, args.start, args.end), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
