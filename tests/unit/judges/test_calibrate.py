"""Cohen's kappa math, blind template, disagreement report, and the kappa CLI guard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eval.judges.calibrate import (
    LABELS_PATH,
    TEMPLATE_FIELDS,
    cohen_kappa,
    disagreement_report,
    main,
    make_label_template,
    paired_labels,
)
from eval.judges.correctness import RUBRIC_VERSION

# --- kappa ----------------------------------------------------------------------------------


def test_perfect_agreement_with_variation_is_one() -> None:
    a = ["correct", "incorrect", "refused", "correct"]
    assert cohen_kappa(a, list(a)) == pytest.approx(1.0)


def test_hand_computed_two_label_case() -> None:
    """10 items: both 'correct' 4, both 'incorrect' 3, A correct/B incorrect 2, A incorrect/B
    correct 1. p_o = 0.7; A: 6 correct/4 incorrect, B: 5/5; p_e = .6*.5 + .4*.5 = 0.5;
    kappa = (0.7 - 0.5) / 0.5 = 0.4."""
    a = ["correct"] * 4 + ["incorrect"] * 3 + ["correct"] * 2 + ["incorrect"]
    b = ["correct"] * 4 + ["incorrect"] * 3 + ["incorrect"] * 2 + ["correct"]
    assert cohen_kappa(a, b) == pytest.approx(0.4)


def test_chance_level_agreement_is_zero() -> None:
    """A: c,c,i,i; B: c,i,c,i. p_o = 0.5, p_e = .5*.5 + .5*.5 = 0.5 -> kappa 0."""
    assert cohen_kappa(
        ["correct", "correct", "incorrect", "incorrect"],
        ["correct", "incorrect", "correct", "incorrect"],
    ) == pytest.approx(0.0)


def test_systematic_disagreement_is_negative() -> None:
    assert cohen_kappa(["correct", "incorrect"], ["incorrect", "correct"]) == pytest.approx(-1.0)


def test_three_label_hand_computed() -> None:
    """6 items. A: c,c,i,i,r,r. B: c,i,i,i,r,c. p_o = 4/6. A marginals 2/2/2 (each 1/3), B:
    c 2, i 3, r 1 -> p_e = (1/3)(2/6)+(1/3)(3/6)+(1/3)(1/6) = 1/3. kappa = (2/3-1/3)/(2/3) = 0.5."""
    a = ["correct", "correct", "incorrect", "incorrect", "refused", "refused"]
    b = ["correct", "incorrect", "incorrect", "incorrect", "refused", "correct"]
    assert cohen_kappa(a, b) == pytest.approx(0.5)


def test_degenerate_single_shared_label_is_undefined() -> None:
    """p_e == 1 (both raters used only 'correct'): kappa is 0/0, reported as None, not 1 or 0."""
    assert cohen_kappa(["correct"] * 5, ["correct"] * 5) is None


def test_one_rater_constant_other_varied_is_defined() -> None:
    """p_e < 1 when only one rater is constant: A all correct, B 3 correct + 1 incorrect.
    p_o = .75, p_e = 1*.75 = .75 -> kappa 0."""
    assert cohen_kappa(["correct"] * 4, ["correct"] * 3 + ["incorrect"]) == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("a", "b"),
    [(["correct"], []), ([], []), (["correct"], ["Correct"])],
)
def test_invalid_inputs_raise(a: list[str], b: list[str]) -> None:
    with pytest.raises(ValueError):
        cohen_kappa(a, b)


# --- template -------------------------------------------------------------------------------


def _results(tmp_path: Path, n_dev: int = 40, n_test: int = 10) -> Path:
    rows: list[dict[str, Any]] = []
    for i in range(n_dev + n_test):
        rows.append(
            {
                "id": f"fb-{i:05d}",
                "set": "financebench",
                "split": "dev" if i < n_dev else "test",
                "question": f"q{i}",
                "gold_answer": f"g{i}",
                "answer_markdown": f"a{i}",
                "refusal": None,
                "correct": i % 2 == 0,
                "judge_label": "correct",
                "judge_reason": "LEAK",
                "cost_usd": "0.01",
                "latency_ms": 5,
                "route": "baseline",
            }
        )
    rows.append({**rows[0], "id": "num-0001", "set": "numeric"})
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"config": {"split": "all"}, "per_question": rows}))
    return path


def test_template_is_blind_and_dev_financebench_only(tmp_path: Path) -> None:
    out = tmp_path / "t.jsonl"
    rows = make_label_template(_results(tmp_path), out=out)
    assert len(rows) == 30
    written = [json.loads(line) for line in out.read_text().splitlines()]
    assert written == rows
    for row in written:
        assert set(row) == {*TEMPLATE_FIELDS, "human_label"}
        assert row["human_label"] == ""
        assert int(row["id"].split("-")[1]) < 40  # dev rows only, no numeric rows
    assert "LEAK" not in out.read_text() and "judge" not in out.read_text()


def test_template_sampling_is_deterministic_and_order_independent(tmp_path: Path) -> None:
    path = _results(tmp_path)
    first = make_label_template(path, out=tmp_path / "a.jsonl")
    data = json.loads(path.read_text())
    data["per_question"].reverse()
    path.write_text(json.dumps(data))
    second = make_label_template(path, out=tmp_path / "b.jsonl")
    assert [r["id"] for r in first] == [r["id"] for r in second]
    third = make_label_template(path, seed=14, out=tmp_path / "c.jsonl")
    assert [r["id"] for r in third] != [r["id"] for r in first]


def test_template_refuses_a_too_small_pool(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="need 30"):
        make_label_template(_results(tmp_path, n_dev=12), out=tmp_path / "t.jsonl")


# --- pairing, report, CLI ------------------------------------------------------------------


def _human() -> list[dict[str, Any]]:
    return [
        {
            "id": "fb-1",
            "question": "q1",
            "gold_answer": "g1",
            "answer_markdown": "a1",
            "refusal": None,
            "human_label": "correct",
        },
        {
            "id": "fb-2",
            "question": "q2",
            "gold_answer": "g2",
            "answer_markdown": "a2",
            "refusal": None,
            "human_label": "incorrect",
        },
    ]


def _judge() -> list[dict[str, Any]]:
    return [
        {"id": "fb-2", "label": "correct", "reason": "judge thought close enough"},
        {"id": "fb-1", "label": "correct", "reason": "ok"},
    ]


def test_pairing_aligns_by_id_and_rejects_gaps() -> None:
    ids, human, judge = paired_labels(_human(), _judge())
    assert (
        ids == ["fb-1", "fb-2"]
        and human == ["correct", "incorrect"]
        and judge == ["correct", "correct"]
    )
    with pytest.raises(ValueError, match="no judge output"):
        paired_labels(_human(), _judge()[:1])
    unlabeled = [{**_human()[0], "human_label": ""}]
    with pytest.raises(ValueError, match="empty"):
        paired_labels(unlabeled, _judge())


def test_disagreement_report_lists_only_disagreements_with_context() -> None:
    report = disagreement_report(_human(), _judge())
    assert "## fb-2: human `incorrect`, judge `correct`" in report
    assert "judge thought close enough" in report and "q2" in report and "g2" in report
    assert "## fb-1" not in report
    assert "agreement = 1/2" in report


def test_kappa_cli_refuses_without_labels_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    judge = tmp_path / "judge.jsonl"
    judge.write_text("".join(json.dumps(r) + "\n" for r in _judge()))
    code = main(["kappa", "--judge", str(judge), "--labels", str(tmp_path / "missing.jsonl")])
    assert code == 2
    assert "Shekar's labels" in capsys.readouterr().err


def test_kappa_cli_reports_writes_report_and_kappa_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels, judge, report = tmp_path / "l.jsonl", tmp_path / "j.jsonl", tmp_path / "r.md"
    kappa_out = tmp_path / "kappa.json"
    labels.write_text("".join(json.dumps(r) + "\n" for r in _human()))
    judge.write_text("".join(json.dumps(r) + "\n" for r in _judge()))
    args = ["kappa", "--judge", str(judge), "--labels", str(labels), "--report", str(report)]
    code = main([*args, "--kappa-out", str(kappa_out), "--allow-n"])
    assert code == 0
    assert "n=2 kappa=0.0000" in capsys.readouterr().out
    assert report.exists()
    record = json.loads(kappa_out.read_text())
    assert record["kappa"] == 0.0 and record["n"] == 2
    assert record["rubric_version"] == RUBRIC_VERSION
    assert len(record["labels_sha256"]) == 64


def test_kappa_cli_refuses_n_other_than_30(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels, judge = tmp_path / "l.jsonl", tmp_path / "j.jsonl"
    labels.write_text("".join(json.dumps(r) + "\n" for r in _human()))
    judge.write_text("".join(json.dumps(r) + "\n" for r in _judge()))
    kappa_out = tmp_path / "kappa.json"
    code = main(
        ["kappa", "--judge", str(judge), "--labels", str(labels), "--kappa-out", str(kappa_out)]
    )
    assert code == 3 and not kappa_out.exists()
    assert "target is 30" in capsys.readouterr().err


def test_kappa_cli_prints_undefined_when_no_label_variation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    human = [{**r, "human_label": "correct"} for r in _human()]
    judge_rows = [{**r, "label": "correct"} for r in _judge()]
    labels, judge, report = tmp_path / "l.jsonl", tmp_path / "j.jsonl", tmp_path / "r.md"
    labels.write_text("".join(json.dumps(r) + "\n" for r in human))
    judge.write_text("".join(json.dumps(r) + "\n" for r in judge_rows))
    kappa_out = tmp_path / "kappa.json"
    code = main(
        [
            "kappa",
            "--judge",
            str(judge),
            "--labels",
            str(labels),
            "--report",
            str(report),
            "--kappa-out",
            str(kappa_out),
            "--allow-n",
        ]
    )
    assert code == 0
    assert "kappa=undefined" in capsys.readouterr().out
    assert "undefined (no label variation)" in report.read_text()
    assert json.loads(kappa_out.read_text())["kappa"] is None


def test_kappa_cli_reads_judge_labels_from_a_results_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "run.json"
    results.write_text(
        json.dumps(
            {
                "run_id": "r1",
                "config": {"judge": "claude-sonnet-5-5", "judge_rubrics": {"correctness": "cv9"}},
                "per_question": [
                    {"id": "fb-1", "judge_label": "correct", "judge_reason": "ok"},
                    {"id": "fb-2", "judge_label": "incorrect", "judge_reason": "no"},
                    {"id": "fb-3", "judge_label": "correct", "judge_reason": "not labelled"},
                ],
            }
        )
    )
    labels, kappa_out = tmp_path / "l.jsonl", tmp_path / "kappa.json"
    labels.write_text("".join(json.dumps(r) + "\n" for r in _human()))
    code = main(
        [
            "kappa",
            "--results",
            str(results),
            "--labels",
            str(labels),
            "--kappa-out",
            str(kappa_out),
            "--allow-n",
        ]
    )
    assert code == 0
    record = json.loads(kappa_out.read_text())
    assert record["kappa"] == pytest.approx(1.0)
    assert record["judge_model"] == "claude-sonnet-5-5"
    assert record["rubric_version"] == "cv9" and record["results_run_id"] == "r1"


def test_duplicate_ids_fail() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        paired_labels([*_human(), _human()[0]], _judge())
    with pytest.raises(ValueError, match="duplicate"):
        paired_labels(_human(), [*_judge(), _judge()[0]])


def test_out_of_set_human_label_fails() -> None:
    human = [{**_human()[0], "human_label": "Correct"}, _human()[1]]
    _, h, j = paired_labels(human, _judge())
    with pytest.raises(ValueError, match="labels outside"):
        cohen_kappa(h, j)


def test_template_refuses_the_protected_labels_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="refusing"):
        make_label_template(_results(tmp_path), out=LABELS_PATH)


def test_template_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    out = tmp_path / "t.jsonl"
    make_label_template(_results(tmp_path), out=out)
    with pytest.raises(FileExistsError):
        make_label_template(_results(tmp_path), out=out)
    assert len(make_label_template(_results(tmp_path), out=out, force=True)) == 30


def test_template_skips_errored_and_refused_rows(tmp_path: Path) -> None:
    path = _results(tmp_path, n_dev=34)
    data = json.loads(path.read_text())
    rows = data["per_question"]
    rows[0]["error"] = "LLMResponseError: truncated"
    del rows[1]["answer_markdown"]
    rows[2]["refusal"] = {"reason": "no data"}
    rows[3]["answer_markdown"] = None
    path.write_text(json.dumps(data))
    out = make_label_template(path, out=tmp_path / "t.jsonl")
    ids = {r["id"] for r in out}
    assert not ids & {"fb-00000", "fb-00001", "fb-00002", "fb-00003"}
    assert len(out) == 30  # exactly the 30 clean dev rows remain
