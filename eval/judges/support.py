"""Support judge: does the cited evidence support one claim? (unsupported-claim rate, M04 3.7)

The judge sees the claim and the evidence it cites, never the gold answer. It must stay
independent of the M6 verifier's entailment check (``app/engine/verify``): a different prompt,
written separately, and never imported from or shared with that module, so the eval does not
grade the verifier with its own rubric. Reported as uncalibrated (no human labels).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.llm.client import LLMClient
from app.llm.types import Usage

RUBRIC_VERSION = "support-v1"
# Thinking counts toward the limit; 1024 risked cutting a verdict off (ADR-049). Not the rubric.
MAX_TOKENS = 8192

SYSTEM = f"""You audit claims made by a question-answering system about SEC filings.
Rubric version: {RUBRIC_VERSION}

You get one claim and the evidence the system cited for it. Decide from the evidence alone:
- "supported": every factual part of the claim is stated in, or follows directly from, the
  evidence. Numbers must match the evidence up to rounding and unit convention.
- "partial": some of the claim is backed by the evidence and some is not (an extra figure,
  an added cause, a wider scope, a different period).
- "unsupported": the evidence does not back the claim's main point, contradicts it, or is
  about something else.

Do not use outside knowledge, and do not reward a claim for being true in general: the
question is only whether this evidence backs it. Give a one-sentence reason."""


class SupportVerdict(BaseModel):
    label: Literal["supported", "unsupported", "partial"]
    reason: str


def build_user_message(claim_text: str, evidence_text: str) -> str:
    return f"<claim>\n{claim_text}\n</claim>\n<evidence>\n{evidence_text}\n</evidence>"


def judge_support(
    client: LLMClient, claim_text: str, evidence_text: str
) -> tuple[SupportVerdict, Usage]:
    messages = [{"role": "user", "content": build_user_message(claim_text, evidence_text)}]
    return client.structured("judge", SYSTEM, messages, SupportVerdict, max_tokens=MAX_TOKENS)
