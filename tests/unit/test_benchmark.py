"""Unit tests for the pure parts of the M1 benchmark harness (`pipelines/duckdb/benchmark.py`).

The harness times each engine in its own subprocess and compares their Parquet outputs; the
engines themselves are tested elsewhere. No Spark here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

from pipelines.duckdb.benchmark import compare_outputs, measure, tree_rss_kb

PS = """\
  100     1   1000
  101   100   2000
  102   101   4000
  200     1  99999
  103   100    500
"""


def test_tree_rss_sums_the_root_and_all_its_descendants_only():
    assert tree_rss_kb(100, PS) == 1000 + 2000 + 4000 + 500


def test_tree_rss_of_a_leaf_is_its_own_rss_and_of_an_unknown_pid_is_zero():
    assert tree_rss_kb(102, PS) == 4000
    assert tree_rss_kb(555, PS) == 0


def test_measure_sees_the_memory_a_child_actually_holds():
    """A child that holds ~150 MB for a second: a sampler that works reports at least most of it."""
    code = "import time; x = b'x' * 150_000_000; time.sleep(1.2)"

    result = measure([sys.executable, "-c", code])

    assert result.exit_code == 0
    assert result.peak_rss_mb >= 120


def test_measure_wall_time_is_not_inflated_by_the_polling_interval():
    result = measure([sys.executable, "-c", "import time; time.sleep(1.0)"], poll_seconds=0.9)

    assert 1.0 <= result.wall_seconds < 1.4  # the interpreter start-up is the only overhead


def test_a_quick_child_does_not_inherit_the_peak_of_an_earlier_big_one():
    measure([sys.executable, "-c", "import time; x = b'x' * 200_000_000; time.sleep(1.0)"])

    quick = measure([sys.executable, "-c", "pass"], poll_seconds=0.5)

    assert quick.peak_rss_mb < 100


def test_measure_reports_a_command_that_cannot_start_instead_of_raising():
    result = measure(["/nonexistent/command"])

    assert result.exit_code == 127


def test_measure_reports_a_failing_command_instead_of_raising():
    result = measure([sys.executable, "-c", "raise SystemExit(3)"])

    assert result.exit_code == 3


# --------------------------------------------------------------------------------------
# compare_outputs: same rows, whatever the hash values
# --------------------------------------------------------------------------------------

TABLES = ("facts_history", "facts", "facts_dimensional")


def write_tables(directory: Path, rows: dict[str, list[tuple]]) -> Path:
    """Each row: (fact_id, fact_key, cik, period_end, qtrs, accession, value, derived_from)."""
    directory.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for table in TABLES:
        con.execute(
            "CREATE TABLE t (fact_id BIGINT, fact_key BIGINT, cik INTEGER, period_end DATE, "
            "qtrs INTEGER, accession VARCHAR, value DECIMAL(28,4), derived_from BIGINT[])"
        )
        for r in rows.get(table, []):
            con.execute("INSERT INTO t VALUES (?, ?, ?, CAST(? AS DATE), ?, ?, ?, ?)", r)
        con.execute(f"COPY t TO '{directory / (table + '.parquet')}' (FORMAT parquet)")
        con.execute("DROP TABLE t")
    return directory


FY = (11, 1, 5, "2023-09-30", 4, "A-23", "10.0000", None)
NM = (12, 2, 5, "2023-07-01", 3, "Q3-23", "7.0000", None)
Q4 = (13, 3, 5, "2023-09-30", 1, "A-23", "3.0000", [11, 12])


def test_identical_content_with_different_hashes_has_no_differences(tmp_path: Path):
    a = write_tables(tmp_path / "a", {"facts_history": [FY, NM, Q4], "facts": [FY, NM, Q4]})
    # the other engine hashes differently: every id changes, the natural keys do not
    b = write_tables(
        tmp_path / "b",
        {
            "facts_history": [
                (91, 81, 5, "2023-09-30", 4, "A-23", "10.0000", None),
                (92, 82, 5, "2023-07-01", 3, "Q3-23", "7.0000", None),
                (93, 83, 5, "2023-09-30", 1, "A-23", "3.0000", [91, 92]),
            ],
            "facts": [
                (91, 81, 5, "2023-09-30", 4, "A-23", "10.0000", None),
                (92, 82, 5, "2023-07-01", 3, "Q3-23", "7.0000", None),
                (93, 83, 5, "2023-09-30", 1, "A-23", "3.0000", [91, 92]),
            ],
        },
    )

    result = compare_outputs(a, b)

    assert all(
        r == {"rows_a": r["rows_a"], "rows_b": r["rows_b"], "only_a": 0, "only_b": 0}
        for r in result.values()
    )
    assert result["facts_history"]["rows_a"] == result["facts_history"]["rows_b"] == 3


def test_a_different_value_shows_up_on_both_sides(tmp_path: Path):
    a = write_tables(tmp_path / "a", {"facts_history": [FY, NM]})
    changed = (11, 1, 5, "2023-09-30", 4, "A-23", "10.0001", None)
    b = write_tables(tmp_path / "b", {"facts_history": [changed, NM]})

    result = compare_outputs(a, b)["facts_history"]

    assert (result["only_a"], result["only_b"]) == (1, 1)


def test_a_derived_row_with_different_parents_is_a_difference(tmp_path: Path):
    """Same ids on both sides would hide this if derived_from were compared as raw numbers."""
    a = write_tables(tmp_path / "a", {"facts_history": [FY, NM, Q4]})
    wrong_parents = (13, 3, 5, "2023-09-30", 1, "A-23", "3.0000", [12, 11])  # 9M first
    b = write_tables(tmp_path / "b", {"facts_history": [FY, NM, wrong_parents]})

    result = compare_outputs(a, b)["facts_history"]

    assert (result["only_a"], result["only_b"]) == (1, 1)


def test_a_missing_table_is_an_error(tmp_path: Path):
    a = write_tables(tmp_path / "a", {})
    (a / "facts.parquet").unlink()
    b = write_tables(tmp_path / "b", {})

    with pytest.raises(FileNotFoundError):
        compare_outputs(a, b)


def test_a_table_without_derived_from_is_compared_on_its_other_columns(tmp_path: Path):
    """`facts_dimensional` has no `derived_from` column in the contract."""

    def write(directory: Path, value: str) -> Path:
        write_tables(directory, {"facts_history": [FY]})
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE d (fact_id BIGINT, fact_key BIGINT, cik INTEGER, period_end DATE, "
            "qtrs INTEGER, accession VARCHAR, value DECIMAL(28,4))"
        )
        con.execute("INSERT INTO d VALUES (1, 1, 5, DATE '2023-09-30', 4, 'A-23', ?)", [value])
        con.execute(f"COPY d TO '{directory / 'facts_dimensional.parquet'}' (FORMAT parquet)")
        return directory

    same = compare_outputs(write(tmp_path / "a", "10.0000"), write(tmp_path / "b", "10.0000"))
    different = compare_outputs(write(tmp_path / "c", "10.0000"), write(tmp_path / "d", "11.0000"))

    assert (same["facts_dimensional"]["only_a"], same["facts_dimensional"]["only_b"]) == (0, 0)
    assert (different["facts_dimensional"]["only_a"], different["facts_dimensional"]["only_b"]) == (
        1,
        1,
    )


# --------------------------------------------------------------------------------------
# run_benchmark: a failed engine step must never be compared against stale output
# --------------------------------------------------------------------------------------


def test_a_failed_duckdb_step_is_not_compared_against_a_stale_earlier_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from pipelines.duckdb import benchmark as bm

    runs_dir = tmp_path / "runs"
    stale = runs_dir / "2q" / "duckdb"
    stale.mkdir(parents=True)
    (stale / "facts.parquet").write_bytes(b"left over from an earlier run")

    def fake_measure(cmd: list[str], **_: object) -> bm.Measurement:
        failing = "normalize_facts_duckdb" in " ".join(cmd)
        return bm.Measurement(1.0, 1.0, 1 if failing else 0)

    def must_not_compare(*_: object, **__: object) -> None:
        raise AssertionError("compared the outputs although a step failed")

    monkeypatch.setattr(bm, "measure", fake_measure)
    monkeypatch.setattr(bm, "compare_outputs", must_not_compare)
    monkeypatch.setattr(bm.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(bm, "_machine", lambda: {})
    monkeypatch.setattr(bm, "_git_sha", lambda: "test")
    results = tmp_path / "bench.json"

    result = bm.run_benchmark(["2023q3"], "2q", tmp_path / "fsds", results, runs_dir)

    assert not stale.exists() or not any(stale.iterdir())  # the stale output was cleared first
    assert "2q" not in result["comparisons"]
    assert [r["exit_code"] for r in result["runs"] if r["engine"] == "duckdb"] == [1]


def test_git_sha_marks_uncommitted_changes_to_the_code(monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace

    from pipelines.duckdb import benchmark as bm

    def fake_run(cmd: list[str], **_: object) -> SimpleNamespace:
        if cmd[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(stdout="abc1234\n")
        return SimpleNamespace(stdout=" M pipelines/duckdb/benchmark.py\n")

    monkeypatch.setattr(bm.subprocess, "run", fake_run)

    assert bm._git_sha() == "abc1234+dirty"


def test_git_sha_of_a_clean_tree_is_the_plain_commit(monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace

    from pipelines.duckdb import benchmark as bm

    def fake_run(cmd: list[str], **_: object) -> SimpleNamespace:
        return SimpleNamespace(stdout="abc1234\n" if cmd[:2] == ["git", "rev-parse"] else "")

    monkeypatch.setattr(bm.subprocess, "run", fake_run)

    assert bm._git_sha() == "abc1234"
