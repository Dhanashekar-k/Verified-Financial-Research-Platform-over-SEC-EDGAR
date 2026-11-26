"""Verify one composed answer: check every claim, apply the policy, rebuild the text.

No LLM anywhere (ADR-049). A numeric claim goes to ``numeric.py``, a text claim to ``quotes.py``;
``policy.py`` decides what survives and whether that answers the question; ``render.py`` writes
the answer from the survivors.

A composer's own prose never reaches the user (H0): the policy is handed the answer without the
composer's ``answer_markdown`` or refusal wording, so it cannot copy them into a refusal. It does
receive the claims; that no dropped claim's text appears in a refusal is the policy's rule.

The pipeline also holds the policy to its contract and raises ``VerifierContractError`` rather
than pass a breach on: a claim is kept only with a "pass" on each check of its kind, kept claims
are the answer's own and unchanged, ``found`` is only kept claims, and a composer's refusal
stays one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import ModuleType

from app.engine.answer import Answer, Claim
from app.engine.verify import numbers, numeric, policy, quotes
from app.engine.verify.models import (
    POLICY_DROP,
    WITHHELD_REASON,
    CheckOutcome,
    Evidence,
    PolicyDecision,
    VerificationReport,
    VerifierResult,
    VerifyConfig,
    VerifyContext,
    without_composer_wording,
)
from app.engine.verify.render import render

CORE_MODULES: tuple[ModuleType, ...] = (numbers, numeric, quotes, policy)
# A claim is kept only when each check that applies to it says "pass" (05 4.4). "Nothing failed"
# is not enough: a check that returned early would leave everything "n/a" and publish the claim.
NUMERIC_CHECKS = ("provenance_exec", "metadata_match", "number_in_text")
TEXT_CHECKS = ("quote_substring", "metadata_match", "number_in_text")

CheckFn = Callable[[Claim, Evidence, VerifyContext, VerifyConfig], CheckOutcome]
DecideFn = Callable[[Claim, CheckOutcome, VerifyConfig], VerifierResult]
ApplyFn = Callable[[Answer, Sequence[VerifierResult], VerifyContext, VerifyConfig], PolicyDecision]


class VerifierNotImplemented(RuntimeError):
    """A run asked for the verifier while a core module is still a stub."""


class VerifierContractError(RuntimeError):
    """The policy returned something the contract forbids."""


@dataclass(frozen=True)
class Verifier:
    """The four decisions, injectable so the orchestration can be tested on its own."""

    check_numeric: CheckFn = numeric.check_numeric
    check_text: CheckFn = quotes.check_text
    decide: DecideFn = policy.decide
    apply: ApplyFn = policy.apply


DEFAULT_VERIFIER = Verifier()


def require_implemented(modules: Sequence[ModuleType] = CORE_MODULES) -> None:
    """Fail before the first question, not once per question after the model calls."""
    stubs = [m.__name__.rsplit(".", 1)[-1] for m in modules if not getattr(m, "IMPLEMENTED", False)]
    if stubs:
        raise VerifierNotImplemented(
            f"the verifier cannot run: still stubbed: {', '.join(stubs)} (app/engine/verify)"
        )


def verify_claim(
    claim: Claim,
    evidence: Evidence,
    ctx: VerifyContext,
    cfg: VerifyConfig | None = None,
    verifier: Verifier = DEFAULT_VERIFIER,
) -> VerifierResult:
    cfg = cfg or VerifyConfig()
    check = verifier.check_text if claim.kind == "text" else verifier.check_numeric
    outcome = check(claim, evidence, ctx, cfg)
    result = verifier.decide(claim, outcome, cfg)
    if result.claim_id != claim.id:
        raise VerifierContractError(f"result for {result.claim_id!r}, expected {claim.id!r}")
    if result.action != "drop":
        if not outcome.passed:
            raise VerifierContractError(f"claim {claim.id!r} failed a check and was not dropped")
        required = TEXT_CHECKS if claim.kind == "text" else NUMERIC_CHECKS
        unmade = [name for name in required if getattr(outcome.checks, name) != "pass"]
        if unmade:
            raise VerifierContractError(
                f"claim {claim.id!r} would be kept without a pass on: {', '.join(unmade)}"
            )
    return result


def _enforce(answer: Answer, results: Sequence[VerifierResult], decision: PolicyDecision) -> None:
    survivors = [c for c, r in zip(answer.claims, results, strict=True) if r.action != "drop"]
    if decision.kept != survivors:
        raise VerifierContractError("kept must be exactly the claims not dropped, unchanged")
    refusal = decision.refusal
    if refusal is None:
        if answer.refusal is not None:
            raise VerifierContractError("a composer refusal must stay a refusal")
        if not decision.kept:
            raise VerifierContractError("an answer with no kept claim must be refused")
    elif refusal.found != [c.text for c in decision.kept]:
        raise VerifierContractError("refusal.found must be the texts of the kept claims")
    elif any(WITHHELD_REASON in text for text in (refusal.reason, *refusal.missing)):
        raise VerifierContractError(
            "the refusal reason must be a template and missing built from slots (H0), not "
            "passed through from the composer's refusal"
        )
    if (refusal is not None) != (decision.status == "refused"):
        raise VerifierContractError(f"status {decision.status!r} does not match the refusal")
    dropped = len(answer.claims) - len(decision.kept)
    if refusal is None and (decision.status == "verified") != (dropped == 0):
        raise VerifierContractError(f"status {decision.status!r} with {dropped} claims dropped")


def verify_answer(
    answer: Answer,
    evidence: Evidence,
    ctx: VerifyContext,
    cfg: VerifyConfig | None = None,
    verifier: Verifier = DEFAULT_VERIFIER,
) -> tuple[Answer, VerificationReport]:
    """The verified ``Answer`` (same schema) and the report beside it."""
    cfg = cfg or VerifyConfig()
    results = [verify_claim(c, evidence, ctx, cfg, verifier) for c in answer.claims]
    decision = verifier.apply(without_composer_wording(answer), results, ctx, cfg)
    _enforce(answer, results, decision)
    first_failed = Counter(
        (r.checks.failed() or [POLICY_DROP])[0] for r in results if r.action == "drop"
    )
    report = VerificationReport(
        status=decision.status,
        claims_total=len(answer.claims),
        claims_verified=len(decision.kept),
        claims_dropped=len(answer.claims) - len(decision.kept),
        results=results,
        dropped_by_check=dict(first_failed),
    )
    verified = Answer(
        answer_markdown=render(decision, ctx), claims=decision.kept, refusal=decision.refusal
    )
    return verified, report
