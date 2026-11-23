"""Correctness judge: the system's answer against the gold answer (FinanceBench).

The judge sees the question, the gold answer, and the system's ``answer_markdown``. It never
sees citations or evidence: correctness is judged against gold, support against evidence, in
separate prompts (M04 pitfall). A refusal's ``found`` / ``missing`` lists are evidence-derived,
so they are never sent either.

Refusals are decided in code, not by the LLM: ``refusal != null`` is deterministic, so when the
answer carries a refusal the judge returns ``refused`` without a model call (zero cost). The
LLM's "refused" label remains for answers that decline in prose without a refusal object.

This is the headline judge, the one the 30 calibration labels measure (Cohen's kappa,
``eval/judges/calibrate.py``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.engine.answer import Answer
from app.llm.client import LLMClient
from app.llm.types import Usage

# Part of the system prompt, so a rubric change changes the cassette key (no stale replays).
RUBRIC_VERSION = "correctness-v2"

SYSTEM = f"""You grade answers to questions about SEC filings against a gold answer.
Rubric version: {RUBRIC_VERSION}

Return exactly one label:
- "refused": the answer says it cannot answer from the filings. Use this even if it mentions
  partial findings.
- "correct": the answer states the gold answer's key fact(s). For numbers, accept differences of
  rounding and unit convention when the magnitudes agree: "$1577.00" (USD millions) matches
  "$1.58 billion" and "1,577 million"; a ratio of 0.298 matches "29.8%". The sign and the
  period (fiscal year or quarter) must match. Extra correct context does not make it wrong.
- "incorrect": anything else, including a wrong number beyond rounding (more than about 1%
  relative unless the gold itself is rounded more coarsely), the wrong period, the wrong
  company, a hedge between several values, or a reasoning question answered with the wrong
  conclusion.

Judge only against the gold answer. Do not use outside knowledge to overrule the gold answer.
Give a one-sentence reason."""


class CorrectnessVerdict(BaseModel):
    label: Literal["correct", "incorrect", "refused"]
    reason: str


SHORT_CIRCUIT_MODEL = "none (refusal decided in code)"


def build_user_message(question: str, gold_answer: str, answer: Answer) -> str:
    return (
        f"<question>\n{question}\n</question>\n"
        f"<gold_answer>\n{gold_answer}\n</gold_answer>\n"
        f"<system_answer>\n{answer.answer_markdown}\n</system_answer>"
    )


def judge_correctness(
    client: LLMClient, question: str, gold_answer: str, answer: Answer
) -> tuple[CorrectnessVerdict, Usage]:
    if answer.refusal is not None:
        verdict = CorrectnessVerdict(label="refused", reason=f"refusal: {answer.refusal.reason}")
        usage = Usage(
            model=SHORT_CIRCUIT_MODEL,
            input_tokens=0,
            cache_write_tokens=0,
            cache_read_tokens=0,
            output_tokens=0,
            cost_usd=Decimal(0),
            latency_ms=0,
        )
        return verdict, usage
    messages = [{"role": "user", "content": build_user_message(question, gold_answer, answer)}]
    return client.structured("judge", SYSTEM, messages, CorrectnessVerdict, max_tokens=1024)
