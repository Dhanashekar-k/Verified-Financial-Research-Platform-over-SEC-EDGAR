"""Judge prompts: content separation, schema wiring, stub mode (no network).

The test directory is ``tests/unit/judges`` rather than ``tests/unit/eval``: with rootdir
imports a test package named ``eval`` would shadow the real ``eval`` package.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.engine.answer import Answer, ChunkProvenance, Claim, Refusal
from app.llm.client import LLMClient
from app.llm.stub import register_stub
from app.llm.types import LLMMode
from eval.judges import correctness, narrative, support
from eval.judges.correctness import CorrectnessVerdict, judge_correctness
from eval.judges.narrative import (
    InvalidJudgeOutput,
    NarrativeVerdict,
    PointCoverage,
    judge_narrative,
)
from eval.judges.support import SupportVerdict, judge_support

EVIDENCE = "Purchases of property, plant and equipment (PP&E) (1,577)"
GOLD = "$1577.00"
QUESTION = "What is the FY2018 capital expenditure amount (in USD millions) for 3M?"


class CapturingSDK:
    """Fakes only the SDK boundary: records the request, returns a canned JSON reply."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._reply = reply
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self._reply))],
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=10,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            stop_reason="end_turn",
        )


def _answer(text: str = "3M's FY2018 capex was $1,577 million.") -> Answer:
    return Answer(
        answer_markdown=text,
        claims=[
            Claim(
                id="c1",
                kind="text",
                text=text,
                provenance=ChunkProvenance(
                    type="chunk", chunk_id="fb:3M_2018_10K#p60#0001", quote=EVIDENCE
                ),
            )
        ],
    )


def _request_text(call: dict[str, Any]) -> str:
    system = " ".join(block["text"] for block in call["system"])
    user = " ".join(m["content"] for m in call["messages"])
    return system + "\n" + user


def _live(reply: dict[str, Any]) -> tuple[LLMClient, CapturingSDK]:
    sdk = CapturingSDK(reply)
    return LLMClient(mode=LLMMode.live, sdk_client=sdk, sleep=lambda s: None), sdk


# --- content separation (M04 pitfall: gold and evidence go to separate prompts) -------------


def test_correctness_prompt_has_gold_but_no_evidence_or_citations() -> None:
    client, sdk = _live({"label": "correct", "reason": "matches"})
    verdict, _ = judge_correctness(client, QUESTION, GOLD, _answer())
    assert verdict.label == "correct"
    text = _request_text(sdk.calls[0])
    assert GOLD in text and QUESTION in text
    assert EVIDENCE not in text
    assert "fb:3M_2018_10K" not in text  # no chunk ids / citations either


def test_support_prompt_has_evidence_but_no_gold() -> None:
    client, sdk = _live({"label": "supported", "reason": "stated"})
    verdict, _ = judge_support(client, "3M's FY2018 capex was $1,577 million.", EVIDENCE)
    assert verdict.label == "supported"
    text = _request_text(sdk.calls[0])
    assert EVIDENCE in text
    assert GOLD not in text and "gold" not in sdk.calls[0]["messages"][0]["content"].lower()


def test_judges_use_the_judge_role_model() -> None:
    client, sdk = _live({"label": "incorrect", "reason": "x"})
    judge_correctness(client, QUESTION, GOLD, _answer())
    assert sdk.calls[0]["model"] == "claude-sonnet-5-5"


def test_three_judges_have_distinct_system_prompts_with_rubric_versions() -> None:
    systems = {correctness.SYSTEM, support.SYSTEM, narrative.SYSTEM}
    assert len(systems) == 3
    for module in (correctness, support, narrative):
        assert module.RUBRIC_VERSION in module.SYSTEM


def test_rubric_version_changes_the_cassette_key() -> None:
    client = LLMClient(mode=LLMMode.replay)
    msgs = [{"role": "user", "content": correctness.build_user_message(QUESTION, GOLD, _answer())}]
    before = client.request_key("judge", correctness.SYSTEM, msgs, CorrectnessVerdict)
    changed = correctness.SYSTEM.replace(correctness.RUBRIC_VERSION, "correctness-v99")
    assert client.request_key("judge", changed, msgs, CorrectnessVerdict) != before


def test_refusal_is_decided_in_code_without_a_model_call() -> None:
    """refusal != null is deterministic: no LLM call, zero cost, label refused."""
    client, sdk = _live({"label": "correct", "reason": "would be wrong"})
    refused = Answer(
        answer_markdown="I can't answer from the filings.",
        refusal=Refusal(reason="no data", found=["revenue 2023 = 5"], missing=["2024"]),
    )
    verdict, usage = judge_correctness(client, QUESTION, GOLD, refused)
    assert verdict.label == "refused" and "no data" in verdict.reason
    assert sdk.calls == []
    assert usage.cost_usd == 0


def test_correctness_prompt_carries_no_refusal_found_or_missing() -> None:
    """found/missing are evidence-derived; the gold-side judge never sees them."""
    msg = correctness.build_user_message(QUESTION, GOLD, _answer())
    assert "refusal" not in msg and "found" not in msg


def test_narrative_lists_gold_points_by_index_and_coverage_is_computed_in_code() -> None:
    client, sdk = _live(
        {
            "points": [{"index": 0, "covered": True}, {"index": 1, "covered": False}],
            "contradiction": False,
            "reason": "one of two",
        }
    )
    verdict, _ = judge_narrative(
        client, "Supply-chain risks?", ["contract manufacturers", "tariffs"], _answer()
    )
    text = _request_text(sdk.calls[0])
    assert "0. contract manufacturers" in text and "1. tariffs" in text
    assert verdict.coverage(2) == 0.5
    assert verdict.contradiction is False


@pytest.mark.parametrize(
    "indices",
    [
        [1, 2, 3],  # 1-based: would silently shift every point
        [0, 0, 1],  # duplicate
        [0, 1],  # a point skipped
        [0, 1, 7],  # out of range
    ],
)
def test_narrative_verdict_with_wrong_indices_is_invalid(indices: list[int]) -> None:
    verdict = NarrativeVerdict(
        points=[PointCoverage(index=i, covered=True) for i in indices],
        contradiction=False,
        reason="",
    )
    with pytest.raises(InvalidJudgeOutput):
        verdict.coverage(3)


def test_judge_narrative_raises_on_invalid_indices_after_the_call() -> None:
    client, sdk = _live(
        {
            "points": [{"index": 1, "covered": True}, {"index": 2, "covered": True}],
            "contradiction": False,
            "reason": "1-based",
        }
    )
    with pytest.raises(InvalidJudgeOutput):
        judge_narrative(client, "q", ["a", "b"], _answer())
    assert len(sdk.calls) == 1


def test_narrative_requires_gold_points() -> None:
    with pytest.raises(ValueError):
        judge_narrative(LLMClient(mode=LLMMode.stub), "q", [], _answer())


# --- stub mode (the test default) -----------------------------------------------------------


def test_stub_mode_returns_registered_verdicts_without_network() -> None:
    register_stub(
        CorrectnessVerdict, lambda **_: CorrectnessVerdict(label="correct", reason="stub")
    )
    register_stub(SupportVerdict, lambda **_: SupportVerdict(label="partial", reason="stub"))
    register_stub(
        NarrativeVerdict,
        lambda **_: NarrativeVerdict(
            points=[PointCoverage(index=0, covered=True)], contradiction=False, reason="stub"
        ),
    )
    client = LLMClient()  # autouse fixture: stub
    assert judge_correctness(client, QUESTION, GOLD, _answer())[0].label == "correct"
    assert judge_support(client, "c", "e")[0].label == "partial"
    v, usage = judge_narrative(client, "q", ["p"], _answer())
    assert v.coverage(1) == 1.0 and usage.cost_usd == 0
