"""The resource-sampler summary used for the serving box measurements."""

from __future__ import annotations

import pytest

from serving.bench.box_resources import parse_rows, summarize

HEADER = "ts,cpu_stat,mem_available_kb,swap_free_kb,disk_used_kb\n"


def line(
    ts: int, user: int, idle: int, steal: int, mem: int = 4_000_000, disk: int = 3_000_000
) -> str:
    # cpu  user nice system idle iowait irq softirq steal guest guest_nice
    return f"{ts},cpu::{user}:0:0:{idle}:0:0:0:{steal}:0:0,{mem},0,{disk}\n"


def test_busy_and_steal_over_a_window():
    text = (
        HEADER
        + line(100, 0, 0, 0)
        + line(110, 60, 30, 10, mem=3_500_000, disk=5_000_000)
        + line(120, 100, 50, 10)
    )

    out = summarize(parse_rows(text), 100, 120)

    assert out["seconds"] == 20
    assert out["cpu_busy_fraction"] == pytest.approx((160 - 50) / 160)  # (total-idle)/total
    assert out["steal_fraction"] == pytest.approx(10 / 160)
    assert out["busy_steal_fraction"] == pytest.approx(10 / 110)
    assert out["min_mem_available_mib"] == pytest.approx(3_500_000 / 1024)
    assert out["peak_disk_used_gb"] == pytest.approx(5_000_000 / 1024 / 1024)


def test_the_window_excludes_samples_outside_it_and_torn_lines_are_skipped():
    text = HEADER + line(50, 0, 0, 0) + line(100, 10, 10, 0) + line(110, 20, 20, 0) + "120,cpu::1:2"

    out = summarize(parse_rows(text), 100, 119)

    assert out["samples"] == 2


def test_a_window_with_one_sample_is_an_error():
    with pytest.raises(ValueError):
        summarize(parse_rows(HEADER + line(100, 0, 0, 0)), 100, 200)
