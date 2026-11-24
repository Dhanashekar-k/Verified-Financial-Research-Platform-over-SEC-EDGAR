"""Long-context FinanceBench baseline: the whole document in the prompt, no retrieval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from app.engine.answer import Answer, ChunkProvenance, Claim
from app.engine.longctx import DocumentTooLarge, LongContextSystem
from app.llm.client import LLMClient
from app.llm.stub import register_stub
from app.llm.types import LLMMode
from eval import run
from eval.schemas import FinanceBenchRow
from eval.systems import SYSTEMS, AccountingLLM, EvalCfg

Q = "What was 3M's FY2018 capex?"


def _pdf(path: Path, pages: list[str]) -> None:
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def _setup(tmp_path: Path, pages: list[str]) -> LongContextSystem:
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    _pdf(pdfs / "3M_2018_10K.pdf", pages)
    qs = tmp_path / "fb.jsonl"
    qs.write_text(json.dumps({"question": Q, "doc_name": "3M_2018_10K"}) + "\n")
    return LongContextSystem(questions_path=qs, pdf_dir=pdfs)


def _answer() -> Answer:
    return Answer(
        answer_markdown="$1,577 million [2].",
        claims=[
            Claim(
                id="c1",
                kind="numeric",
                text="capex",
                value=1577,
                unit="USD millions",
                primary=True,
                provenance=ChunkProvenance(
                    type="chunk", chunk_id="fb:3M_2018_10K#p2", quote="capex 1,577"
                ),
            )
        ],
    )


class _LLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def structured(
        self, role: str, system: str, messages: list[dict[str, Any]], schema: Any, **kw: Any
    ) -> tuple[Answer, None]:
        self.calls.append((system, messages))
        return _answer(), None


def test_every_page_goes_into_one_prompt_with_its_own_chunk_id(tmp_path: Path) -> None:
    system = _setup(tmp_path, ["intro page", "capex 1,577", "notes page"])
    llm = _LLM()
    ans = system.answer(Q, EvalCfg(llm=llm))  # type: ignore[arg-type]
    assert ans == _answer()
    ((_, messages),) = llm.calls
    body = messages[0]["content"]
    for n, text in enumerate(["intro page", "capex 1,577", "notes page"], start=1):
        assert f"chunk_id=fb:3M_2018_10K#p{n}" in body and text in body
    assert body.endswith(f"Question: {Q}")


def test_it_does_not_retrieve_and_is_marked_so() -> None:
    assert LongContextSystem.retrieves is False
    assert "longctx" in SYSTEMS


def test_an_oversized_document_is_not_sent(tmp_path: Path) -> None:
    system = _setup(tmp_path, ["word " * 12] * 40)  # ~2,400 chars over 40 pages
    system._max_est_tokens = 100  # ~400 chars
    llm = _LLM()
    with pytest.raises(DocumentTooLarge):
        system.answer(Q, EvalCfg(llm=llm))  # type: ignore[arg-type]
    assert llm.calls == []  # nothing billed


def test_unknown_question_and_missing_pdf_fail_loudly(tmp_path: Path) -> None:
    system = _setup(tmp_path, ["p"])
    with pytest.raises(LookupError):
        system.answer("some other question", EvalCfg(llm=_LLM()))  # type: ignore[arg-type]
    (tmp_path / "pdfs" / "3M_2018_10K.pdf").unlink()
    with pytest.raises(FileNotFoundError):
        system.answer(Q, EvalCfg(llm=_LLM()))  # type: ignore[arg-type]


def test_harness_leaves_recall_unscored_for_a_system_that_does_not_retrieve(
    tmp_path: Path,
) -> None:
    """recall@k = 0 would read as 'retrieved nothing relevant'; here nothing was retrieved by
    design, so it is unscored (None), not 0."""
    system = _setup(tmp_path, ["intro", "capex 1,577"])
    register_stub(Answer, lambda **_: _answer())
    client = LLMClient(mode=LLMMode.stub)
    cfg = EvalCfg(llm=AccountingLLM(client))
    row = FinanceBenchRow(
        id="fb-1",
        question=Q,
        gold_answer="$1577",
        company="3M",
        doc_name="3M_2018_10K",
        evidence=[{"page": 2, "text": "capex 1,577"}],  # type: ignore[list-item]
        question_type="metrics-generated",
        reasoning="r",
    )
    outcomes, _ = run.run_questions(
        system,
        cfg,
        None,
        [("financebench", row, None)],
        {"fb-1": ["fb:3M_2018_10K#p2#0000"]},
        client,
    )
    (o,) = outcomes
    assert o.error is None and o.retrieved is None
    assert "recall_at_5" not in o.scores


def test_sample_is_seeded_per_set_and_keeps_order() -> None:
    items: list[Any] = [("a", object(), None) for _ in range(20)]
    items += [("b", object(), None) for _ in range(3)]
    s1 = run.sample_items(items, 5, seed=13)
    s2 = run.sample_items(items, 5, seed=13)
    assert s1 == s2 and sum(i[0] == "a" for i in s1) == 5 and sum(i[0] == "b" for i in s1) == 3
    assert [i for i in items if i in s1] == s1  # original order
    assert run.sample_items(items, 5, seed=14) != s1


def test_sampling_picks_by_position_even_when_rows_are_the_same_object() -> None:
    same = ("b", object(), None)
    assert len(run.sample_items([same, same, same], 1, seed=1)) == 1


def test_sample_must_be_positive() -> None:
    with pytest.raises(SystemExit):
        run.main(["--system", "stub", "--sample", "0"])


def test_main_writes_the_results_file_then_exits_2_when_a_metric_rejects_most_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eval import metrics

    def bad(answer: Answer) -> bool:
        raise ValueError("unknown label")

    monkeypatch.setattr(metrics, "refused", bad)
    sets = tmp_path / "sets"
    sets.mkdir()
    rows = [
        {
            "id": f"un-{i:03d}",
            "question": f"Q{i}?",
            "reason": "future_guidance",
            "expected": "refuse",
        }
        for i in range(8)
    ]
    (sets / "unanswerable.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = tmp_path / "out"
    code = run.main(
        [
            "--system", "stub", "--mode", "stub", "--allow-stub-results", "--no-judge",
            "--sets", "unanswerable", "--split", "all", "--sets-dir", str(sets),
            "--splits", str(tmp_path / "none.json"), "--out", str(out),
            "--cassettes", str(tmp_path / "cass"),
        ]
    )  # fmt: skip
    assert code == 2
    (written,) = out.glob("*.json")  # the run's cost record was not lost
    assert json.loads(written.read_text())["sets"]["unanswerable"]["scoring_errors"] == 8


def test_a_sampled_run_records_its_sample_and_seed(tmp_path: Path) -> None:
    sets = tmp_path / "sets"
    sets.mkdir()
    rows = [
        {
            "id": f"un-{i:03d}",
            "question": f"Q{i}?",
            "reason": "future_guidance",
            "expected": "refuse",
        }
        for i in range(8)
    ]
    (sets / "unanswerable.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = tmp_path / "out"
    code = run.main(
        [
            "--system", "stub", "--mode", "stub", "--allow-stub-results", "--no-judge",
            "--sets", "unanswerable", "--split", "all", "--sets-dir", str(sets),
            "--splits", str(tmp_path / "none.json"), "--out", str(out),
            "--cassettes", str(tmp_path / "cass"), "--sample", "3", "--sample-seed", "7",
        ]
    )  # fmt: skip
    (written,) = out.glob("*.json")
    res = json.loads(written.read_text())
    assert code == 0 and res["config"]["sample"] == 3 and res["config"]["sample_seed"] == 7
    assert res["sets"]["unanswerable"]["n"] == 3


def _stub_run(tmp_path: Path, *extra: str) -> dict[str, Any]:
    sets = tmp_path / "sets"
    sets.mkdir(exist_ok=True)
    rows = [
        {
            "id": f"un-{i:03d}",
            "question": f"Q{i}?",
            "reason": "future_guidance",
            "expected": "refuse",
        }
        for i in range(4)
    ]
    (sets / "unanswerable.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = tmp_path / "out"
    code = run.main(
        [
            "--system", "stub", "--mode", "stub", "--allow-stub-results", "--no-judge",
            "--sets", "unanswerable", "--split", "all", "--sets-dir", str(sets),
            "--splits", str(tmp_path / "none.json"), "--out", str(out),
            "--cassettes", str(tmp_path / "cass"), *extra,
        ]
    )  # fmt: skip
    assert code == 0
    (written,) = out.glob("*.json")
    result: dict[str, Any] = json.loads(written.read_text())
    return result


def test_a_full_run_records_no_sample_and_no_seed(tmp_path: Path) -> None:
    """A seed with no sample would make a full run look sampled."""
    cfg = _stub_run(tmp_path, "--sample-seed", "99")["config"]
    assert cfg["sample"] is None and cfg["sample_seed"] is None
    assert cfg["request_timeout_s"] is None


def test_request_timeout_reaches_the_results_config(tmp_path: Path) -> None:
    assert _stub_run(tmp_path, "--request-timeout", "600")["config"]["request_timeout_s"] == 600.0


def test_request_timeout_must_be_positive() -> None:
    for bad in ("0", "-5"):
        with pytest.raises(SystemExit):
            run.main(["--system", "stub", "--request-timeout", bad])


def test_an_errored_row_from_a_non_retrieving_system_is_null_not_empty(tmp_path: Path) -> None:
    """[] would read as 'retrieved nothing'; a system that doesn't retrieve has no such list."""
    system = LongContextSystem(questions_path=_write_q(tmp_path), pdf_dir=tmp_path / "nopdfs")
    client = LLMClient(mode=LLMMode.stub)
    row = FinanceBenchRow(
        id="fb-1",
        question=Q,
        gold_answer="x",
        company="3M",
        doc_name="3M_2018_10K",
        evidence=[],
        question_type="t",
        reasoning="r",
    )
    outcomes, _ = run.run_questions(
        system, EvalCfg(llm=AccountingLLM(client)), None, [("financebench", row, None)], {}, client
    )
    (o,) = outcomes
    assert o.error is not None and "FileNotFoundError" in o.error
    assert o.retrieved is None


def _write_q(tmp_path: Path) -> Path:
    qs = tmp_path / "fb.jsonl"
    qs.write_text(json.dumps({"question": Q, "doc_name": "3M_2018_10K"}) + "\n")
    return qs
