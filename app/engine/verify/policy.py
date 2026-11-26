"""Keep, drop and refuse (M06 3.4, ADR-049 D5-D7). **Shekar-owned.**
Stub: set ``IMPLEMENTED = True`` once ``decide`` and ``apply`` are written.

decide
  One claim's ``CheckOutcome`` -> ``VerifierResult``. No failed check: ``supported`` / ``keep``.
  Any failed check: ``unsupported`` / ``drop``, with the outcome's checks and reason carried
  over. ``partial``, ``error`` and ``rewrite`` are never produced (no entailment stage).

apply: which claims survive
  ``kept`` = the answer's claims whose result is ``keep``, **unchanged and in composer order**.

apply: sufficiency (D5). Refuse when what survives does not answer the question.
  A *numeric slot* comes from ``ctx.results``; a slot is met when a kept claim's
  ``(expr, set(fact_ids))`` equals that result's. A kept claim over a restated note's latest
  value does not meet a slot.
  - ``ctx.not_found`` not empty, or ``ctx.unsupported`` set: a requested figure is unmet.
  - value, ratio, trend, rank: every result is a slot.
  - growth: the growth result only; no separate claim for the base year is needed.
  - compare: the ``difference`` result, **or** every ``value`` result.
  - The answer's primary claim (``answer.primary_claim()``) was dropped: refuse. Scoring must
    not fall back to another claim.
  - Text is wanted when the intent is ``narrative`` or ``hybrid``, or ``ctx.text_only``: at
    least **one** kept text claim. (``Claim`` has no topic field, so M06's "one per narrative
    topic" cannot be checked: a recorded departure.)
  A rank over the whole universe has ``plan.companies == []``: its slots still come from
  ``ctx.results``, so an empty company list never makes sufficiency pass by itself.

apply: refusals (D6), in the ADR-041 shape
  - A refusal the **composer** set stays a refusal even when every claim passes.
  - ``found`` is rebuilt: exactly the ``text`` of each kept claim. Composer ``found`` strings
    are never copied (they carry figures no claim supports).
  - The composer's ``reason`` and ``missing`` entries stay only if they add no value-bearing
    number that no kept claim states; otherwise use a template.
  - A verifier refusal has a templated one-sentence ``reason``, and ``missing`` has one entry per
    unmet slot (metric, company, period) or unmet need ("a supported passage ...").
  - **No text of a dropped claim appears anywhere in the refusal.**

apply: status
  ``refused`` when there is a refusal; else ``verified`` when nothing was dropped; else
  ``partially_verified``. Without a refusal at least one claim is kept: an answer whose every
  claim was dropped, or that had none, is refused.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.engine.answer import Answer, Claim
from app.engine.verify.models import (
    CheckOutcome,
    PolicyDecision,
    VerifierResult,
    VerifyConfig,
    VerifyContext,
)

IMPLEMENTED = False


def decide(claim: Claim, outcome: CheckOutcome, cfg: VerifyConfig) -> VerifierResult:
    """Keep or drop one checked claim."""
    raise NotImplementedError("Shekar-owned: verify/policy.decide")


def apply(
    answer: Answer,
    results: Sequence[VerifierResult],
    ctx: VerifyContext,
    cfg: VerifyConfig,
) -> PolicyDecision:
    """``results`` has one entry per ``answer.claims``, in the same order."""
    raise NotImplementedError("Shekar-owned: verify/policy.apply")
