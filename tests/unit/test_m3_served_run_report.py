"""The pure parts of the served-run report: per-core rates from the executors' log lines."""

from __future__ import annotations

import pytest

from scripts.m3_served_run_report import QUERIES, per_core

LINE = (
    "embed_partition rows={rows} load_s=4.0 infer_s={infer} rows_per_s={rate} batch=32 sorted=True "
    "pid=1 cpu='Intel(R) Xeon(R) Platinum 8259CL CPU @ 2.50GHz' usable_cpus=4 flags=avx2"
)


def line(rows: int, infer: float) -> str:
    return LINE.format(rows=rows, infer=infer, rate=f"{rows / infer:.2f}")


def test_the_core_rate_is_total_rows_over_total_inference_time():
    lines = [line(1000, 400.0), line(500, 250.0)]

    out = per_core(lines)

    assert out["partition_lines"] == 2
    assert out["rows"] == 1500
    assert out["chunks_per_second_per_core"] == pytest.approx(1500 / 650.0)
    assert out["min_partition"] == pytest.approx(2.0)
    assert out["max_partition"] == pytest.approx(2.5)


def test_every_dod_query_reads_the_embeddings_table_and_none_uses_not_in():
    for name, sql in QUERIES.items():
        assert "chunk_embeddings" in sql, name
        assert "NOT IN" not in sql.upper().replace("IS NOT NULL", ""), name


def test_the_scope_query_uses_the_shared_served_rule():
    assert "form IN ('10-K','10-K/A')" in QUERIES["scope"]
    assert "doc_source = 'financebench_pdf'" in QUERIES["scope"]
