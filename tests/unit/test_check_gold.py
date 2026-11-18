"""Unit tests for ``eval/check_gold.py`` (CLAUDE.md rule 2: gold files are checksummed).

Gold files: ``eval/sets/*.jsonl`` (not ``eval/sets/drafts/``) and ``eval/judges/calibration.jsonl``.
``eval/sets/CHECKSUMS`` holds ``<sha256>  <relative path>`` lines. With no gold files at all the
check skips cleanly (exit 0); once any exists, a missing or stale CHECKSUMS is a failure.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from eval.check_gold import find_gold_files, main


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "eval" / "sets" / "drafts").mkdir(parents=True)
    (tmp_path / "eval" / "judges").mkdir(parents=True)
    return tmp_path


def run(repo: Path, *args: str) -> int:
    return main(["--root", str(repo), *args])


def test_no_gold_files_skips_cleanly_even_without_checksums(
    repo: Path, capsys: pytest.CaptureFixture[str]
):
    (repo / "eval" / "sets" / "drafts" / "wip.jsonl").write_text("{}\n")

    assert run(repo) == 0
    assert "skip" in capsys.readouterr().out.lower()


def test_drafts_are_not_gold(repo: Path):
    (repo / "eval" / "sets" / "drafts" / "wip.jsonl").write_text("{}\n")
    (repo / "eval" / "sets" / "numeric_auto.jsonl").write_text("{}\n")

    assert find_gold_files(repo) == ["eval/sets/numeric_auto.jsonl"]


def test_calibration_file_is_gold(repo: Path):
    (repo / "eval" / "judges" / "calibration.jsonl").write_text("{}\n")

    assert find_gold_files(repo) == ["eval/judges/calibration.jsonl"]


def test_gold_without_a_checksums_file_fails_and_says_how_to_fix(
    repo: Path, capsys: pytest.CaptureFixture[str]
):
    (repo / "eval" / "sets" / "sections_gold.jsonl").write_text("{}\n")

    assert run(repo) == 1
    assert "make gold-checksums" in capsys.readouterr().err


def test_write_then_check_passes(repo: Path):
    (repo / "eval" / "sets" / "a.jsonl").write_text("one\n")
    (repo / "eval" / "judges" / "calibration.jsonl").write_text("two\n")

    assert run(repo, "--write") == 0
    assert run(repo) == 0
    lines = (repo / "eval" / "sets" / "CHECKSUMS").read_text().splitlines()
    assert lines == sorted(lines, key=lambda s: s.split("  ", 1)[1])
    assert f"{sha('one' + chr(10))}  eval/sets/a.jsonl" in lines


def test_an_edited_gold_file_fails(repo: Path, capsys: pytest.CaptureFixture[str]):
    gold = repo / "eval" / "sets" / "a.jsonl"
    gold.write_text("one\n")
    run(repo, "--write")
    gold.write_text("edited\n")

    assert run(repo) == 1
    assert "eval/sets/a.jsonl" in capsys.readouterr().err


def test_a_new_unlisted_gold_file_fails(repo: Path, capsys: pytest.CaptureFixture[str]):
    (repo / "eval" / "sets" / "a.jsonl").write_text("one\n")
    run(repo, "--write")
    (repo / "eval" / "sets" / "b.jsonl").write_text("new\n")

    assert run(repo) == 1
    assert "eval/sets/b.jsonl" in capsys.readouterr().err


def test_a_deleted_gold_file_fails(repo: Path, capsys: pytest.CaptureFixture[str]):
    a = repo / "eval" / "sets" / "a.jsonl"
    b = repo / "eval" / "sets" / "b.jsonl"
    a.write_text("one\n")
    b.write_text("two\n")
    run(repo, "--write")
    b.unlink()

    assert run(repo) == 1
    assert "eval/sets/b.jsonl" in capsys.readouterr().err


def test_write_with_no_gold_files_does_not_create_an_empty_checksums_file(repo: Path):
    assert run(repo, "--write") == 0
    assert not (repo / "eval" / "sets" / "CHECKSUMS").exists()


def test_deleting_every_gold_file_while_checksums_remain_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
):
    gold = repo / "eval" / "sets" / "a.jsonl"
    gold.write_text("one\n")
    run(repo, "--write")
    gold.unlink()

    assert run(repo) == 1
    assert "eval/sets/a.jsonl" in capsys.readouterr().err


def test_write_with_no_gold_files_refuses_to_blank_existing_checksums(repo: Path):
    gold = repo / "eval" / "sets" / "a.jsonl"
    gold.write_text("one\n")
    run(repo, "--write")
    gold.unlink()

    assert run(repo, "--write") == 1
    assert (repo / "eval" / "sets" / "CHECKSUMS").read_text().strip()
