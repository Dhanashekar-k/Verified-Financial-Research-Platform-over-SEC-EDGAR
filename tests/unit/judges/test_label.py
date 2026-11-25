"""The blind labeling session: navigation, resume, blindness, and the protected labels file."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eval.judges import label
from eval.judges.calibrate import LABELS_PATH, paired_labels


def _rows(n: int = 4) -> list[dict[str, Any]]:
    return [
        {
            "id": f"fb-{i}",
            "question": f"question {i}",
            "gold_answer": f"gold {i}",
            "answer_markdown": f"answer {i}",
            "refusal": None,
            "human_label": "",
        }
        for i in range(n)
    ]


def _worksheet(tmp_path: Path, n: int = 4) -> Path:
    path = tmp_path / "worksheet.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in _rows(n)))
    return path


def _session(
    tmp_path: Path, keys: list[str], n: int = 4
) -> tuple[bool, dict[str, str], list[str], Path]:
    rows = label.read_rows(_worksheet(tmp_path, n))
    out = tmp_path / "labeled.jsonl"
    labels = label.load_labels(rows, out)
    feed: Iterator[str] = iter(keys)
    shown: list[str] = []
    done = label.run_session(rows, labels, out, ask=lambda _p: next(feed), show=shown.append)
    return done, labels, shown, out


def test_labels_every_row_and_writes_the_full_rows_with_human_label(tmp_path: Path) -> None:
    done, labels, _, out = _session(tmp_path, ["c", "i", "r", "c", "y"])
    assert done
    assert labels == {"fb-0": "correct", "fb-1": "incorrect", "fb-2": "refused", "fb-3": "correct"}
    written = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["human_label"] for r in written] == ["correct", "incorrect", "refused", "correct"]
    assert all(r["answer_markdown"] for r in written)  # kappa checks answers against the run
    ids, human, judge = paired_labels(
        written, [{"id": r["id"], "label": "correct"} for r in written]
    )
    assert len(ids) == 4 and human[1] == "incorrect"  # the file is what `make kappa` reads


def test_skipped_rows_come_round_again_after_the_last_row(tmp_path: Path) -> None:
    done, labels, shown, _ = _session(tmp_path, ["s", "c", "c", "c", "i", "y"])
    assert done and labels["fb-0"] == "incorrect"  # row 0 was skipped, then labeled last
    assert sum("id fb-0" in s for s in shown) == 2


def test_the_last_unlabeled_row_cannot_be_skipped_forever(tmp_path: Path) -> None:
    done, _, shown, _ = _session(tmp_path, ["c", "c", "c", "s", "c", "y"])
    assert done
    assert any("only unlabeled row left" in s for s in shown)


def test_back_steps_to_the_previous_row_and_allows_relabeling(tmp_path: Path) -> None:
    done, labels, shown, _ = _session(tmp_path, ["c", "b", "i", "c", "c", "c", "y"])
    assert done and labels["fb-0"] == "incorrect"
    assert any("your current label: correct" in s for s in shown)


def test_back_at_the_first_row_stays_put(tmp_path: Path) -> None:
    done, labels, _, _ = _session(tmp_path, ["b", "c", "c", "c", "c", "y"])
    assert done and len(labels) == 4


def test_an_unknown_key_reprompts_without_labeling(tmp_path: Path) -> None:
    done, labels, shown, _ = _session(tmp_path, ["x", "", "c", "c", "c", "c", "y"])
    assert done and len(labels) == 4
    assert sum("press c, i, r, s, b or q" in s for s in shown) == 2


def test_quit_saves_progress_and_the_next_session_resumes(tmp_path: Path) -> None:
    done, labels, _, out = _session(tmp_path, ["c", "i", "q"])
    assert not done and len(labels) == 2
    done2, labels2, shown, _ = _session(tmp_path, ["c", "c", "y"])  # same worksheet, same out file
    assert done2 and len(labels2) == 4
    assert labels2["fb-0"] == "correct" and labels2["fb-1"] == "incorrect"
    assert "id fb-2" in shown[0]  # picked up at the first unlabeled row, not at row 0
    assert out.exists()


def test_end_of_input_quits_cleanly_and_saves(tmp_path: Path) -> None:
    rows = label.read_rows(_worksheet(tmp_path))
    out, labels = tmp_path / "labeled.jsonl", {}

    def eof(_p: str) -> str:
        raise EOFError

    assert label.run_session(rows, labels, out, ask=eof, show=lambda _s: None) is False
    assert out.exists()


def test_only_question_gold_and_answer_are_shown_never_judge_fields(tmp_path: Path) -> None:
    rows = _rows(1)
    rows[0]["judge_label"] = "LEAK-JUDGE"
    rows[0]["correct"] = "LEAK-CORRECT"
    text = label.render(rows[0], 0, 1, 0, None)
    assert "question 0" in text and "gold 0" in text and "answer 0" in text
    assert "LEAK" not in text and "judge" not in text.lower()


def test_progress_from_a_different_worksheet_is_refused(tmp_path: Path) -> None:
    _, _, _, out = _session(tmp_path, ["c", "q"])
    other = _rows(4)
    other[0]["answer_markdown"] = "a rerun's answer"
    with pytest.raises(ValueError, match="different worksheet"):
        label.load_labels(other, out)


def test_a_bad_label_in_the_progress_file_fails_loudly(tmp_path: Path) -> None:
    rows = _rows(2)
    out = tmp_path / "labeled.jsonl"
    out.write_text(json.dumps({**rows[0], "human_label": "yes"}) + "\n")
    with pytest.raises(ValueError, match="expected one of"):
        label.load_labels(rows, out)


def test_the_protected_labels_file_is_never_written(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="refusing to write"):
        label.save(_rows(1), {}, LABELS_PATH)
    assert label.main(["--out", str(LABELS_PATH)]) == 2
    assert not LABELS_PATH.exists()


def test_main_prints_the_promote_command_only_when_every_row_is_labeled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(label, "DRAFTS_DIR", tmp_path)
    ws, out = _worksheet(tmp_path, 2), tmp_path / "labeled.jsonl"
    feed = iter(["c", "q"])
    monkeypatch.setattr("builtins.input", lambda _p: next(feed))
    assert label.main(["--worksheet", str(ws), "--out", str(out)]) == 0  # a quit is not an error
    assert "cp " not in capsys.readouterr().out  # unfinished: no promote command
    feed2 = iter(["i", "y"])
    monkeypatch.setattr("builtins.input", lambda _p: next(feed2))
    assert label.main(["--worksheet", str(ws), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert f"cp {out} {LABELS_PATH}" in printed and "make kappa RESULTS=" in printed


def test_a_missing_worksheet_says_how_to_make_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert label.main(["--worksheet", str(tmp_path / "nope.jsonl")]) == 2
    assert "make calibration-worksheet" in capsys.readouterr().out


def test_a_typo_on_the_last_row_can_be_undone_before_finishing(tmp_path: Path) -> None:
    """The last label used to end the session at once, with no way back."""
    done, labels, shown, _ = _session(tmp_path, ["c", "c", "c", "i", "b", "c", "y"])
    assert done and labels["fb-3"] == "correct"  # 'i' was a typo; back, relabel, then confirm
    assert any("All rows labeled" in s or "labeled 4/4" in s for s in shown)


def test_a_fully_labeled_file_asks_for_confirmation_instead_of_finishing(tmp_path: Path) -> None:
    _session(tmp_path, ["c", "c", "c", "c", "y"])
    done, labels, shown, _ = _session(tmp_path, ["v", "i", "c", "c", "c", "y"], n=4)
    assert done and labels["fb-0"] == "incorrect"  # 'v' reviews from row 1, relabeling allowed
    assert "id fb-0" in shown[1]


def test_end_of_input_at_the_confirm_prompt_does_not_confirm(tmp_path: Path) -> None:
    rows = label.read_rows(_worksheet(tmp_path, 1))
    out = tmp_path / "labeled.jsonl"
    keys = iter(["c"])

    def ask(_p: str) -> str:
        try:
            return next(keys)
        except StopIteration:
            raise EOFError from None

    assert label.run_session(rows, {}, out, ask=ask, show=lambda _s: None) is False
    assert json.loads(out.read_text())["human_label"] == "correct"  # saved, just not confirmed


def test_confirm_prompt_rejects_unknown_keys(tmp_path: Path) -> None:
    done, _, shown, _ = _session(tmp_path, ["c", "c", "c", "c", "x", "y"])
    assert done and any("press y, b or v" in s for s in shown)


def test_out_must_be_a_file_under_drafts_and_not_the_worksheet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts = tmp_path / "eval" / "judges" / "drafts"
    drafts.mkdir(parents=True)
    monkeypatch.setattr(label, "DRAFTS_DIR", drafts)
    ws = drafts / "calibration_worksheet.jsonl"
    ws.write_text("")
    label.check_out(drafts / "calibration_labeled.jsonl", ws)  # fine
    with pytest.raises(ValueError, match="under"):
        label.check_out(tmp_path / "elsewhere.jsonl", ws)
    with pytest.raises(ValueError, match="under"):  # a gold path
        label.check_out(tmp_path / "eval" / "sets" / "sections_gold.jsonl", ws)
    with pytest.raises(ValueError, match="under"):  # ./a/../ spelling
        label.check_out(drafts / ".." / "calibration.jsonl", ws)
    with pytest.raises(ValueError, match="worksheet itself"):
        label.check_out(ws, ws)


def test_a_symlink_in_drafts_to_the_protected_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts = tmp_path / "eval" / "judges" / "drafts"
    drafts.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(label, "DRAFTS_DIR", drafts)
    link = drafts / "sneaky.jsonl"
    link.symlink_to(tmp_path / LABELS_PATH)  # dangling, then resolves to the protected file
    with pytest.raises(ValueError):
        label.check_out(link, drafts / "calibration_worksheet.jsonl")
