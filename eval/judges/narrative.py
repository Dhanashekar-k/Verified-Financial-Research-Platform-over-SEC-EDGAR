"""Narrative judge: gold-point coverage (0-1) and contradiction (yes/no), M04 3.7.

The judge sees the question, the curated gold points and the system's answer text. Coverage
is the mean of the per-point ``covered`` booleans, computed in code (the judge never does the
arithmetic). Reported as uncalibrated (no human labels).
"""

from __future__ import annotations

from pydantic import BaseModel

from app.engine.answer import Answer
from app.llm.client import LLMClient
from app.llm.types import Usage

RUBRIC_VERSION = "narrative-v1"

SYSTEM = f"""You grade answers to narrative questions about SEC filings (risk factors, MD&A,
market risk) against a list of gold points written by a human reviewer.
Rubric version: {RUBRIC_VERSION}

For each gold point, in the given order, say whether the answer covers it: the answer states
the point or a clear paraphrase of it. A vague gesture at the topic does not count.
Then say whether the answer contradicts any gold point (states the opposite, or a materially
different fact about the same thing). Omissions are not contradictions.
If the system refused, mark every point as not covered and contradiction as false.
Return one entry per gold point, with the point's index, and a one-sentence reason overall."""


class InvalidJudgeOutput(ValueError):
    """The judge's verdict doesn't fit the row (for example 1-based or missing point indices).

    Raised instead of silently scoring mismatched indices as "not covered"."""


class PointCoverage(BaseModel):
    index: int
    covered: bool


class NarrativeVerdict(BaseModel):
    points: list[PointCoverage]
    contradiction: bool
    reason: str

    def check(self, n_points: int) -> None:
        """Raise InvalidJudgeOutput unless the indices are exactly 0..n_points-1, once each."""
        if n_points <= 0:
            raise ValueError("a narrative row needs at least one gold point")
        indices = sorted(p.index for p in self.points)
        if indices != list(range(n_points)):
            raise InvalidJudgeOutput(
                f"judge returned point indices {indices}, expected 0..{n_points - 1} once each"
            )

    def coverage(self, n_points: int) -> float:
        """Share of the ``n_points`` gold points covered (after ``check``)."""
        self.check(n_points)
        return sum(p.covered for p in self.points) / n_points


def build_user_message(question: str, gold_points: list[str], answer: Answer) -> str:
    points = "\n".join(f"{i}. {p}" for i, p in enumerate(gold_points))
    refused = "yes" if answer.refusal is not None else "no"
    return (
        f"<question>\n{question}\n</question>\n"
        f"<gold_points>\n{points}\n</gold_points>\n"
        f'<system_answer refused="{refused}">\n{answer.answer_markdown}\n</system_answer>'
    )


def judge_narrative(
    client: LLMClient, question: str, gold_points: list[str], answer: Answer
) -> tuple[NarrativeVerdict, Usage]:
    if not gold_points:
        raise ValueError("a narrative row needs at least one gold point")
    messages = [{"role": "user", "content": build_user_message(question, gold_points, answer)}]
    verdict, usage = client.structured("judge", SYSTEM, messages, NarrativeVerdict, max_tokens=2048)
    verdict.check(len(gold_points))  # the call was billed either way; the ledger has it
    return verdict, usage
