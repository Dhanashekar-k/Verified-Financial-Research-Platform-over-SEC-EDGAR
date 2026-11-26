"""A stand-in for the four Shekar-owned decisions, for testing what is **around** them: the
orchestration, the engine wiring and the offline runners. It implements none of the verifier's
rules: it fails exactly the claim ids it is told to, and its "policy" is the shortest thing that
honours the pipeline's contract."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from app.engine.answer import Answer, Claim, Refusal
from app.engine.verify.models import (
    CheckOutcome,
    Checks,
    Evidence,
    PolicyDecision,
    VerifierResult,
    VerifyConfig,
    VerifyContext,
)
from app.engine.verify.pipeline import Verifier


class FakeVerifier:
    def __init__(self, fail_texts: Collection[str] = (), fail_ids: Collection[str] = ()) -> None:
        self.fail_texts, self.fail_ids = set(fail_texts), set(fail_ids)
        self.numeric_calls: list[str] = []
        self.text_calls: list[str] = []
        self.contexts: list[VerifyContext] = []
        self.evidence: list[Evidence] = []

    def _fails(self, claim: Claim) -> bool:
        return claim.id in self.fail_ids or claim.text in self.fail_texts

    def check_numeric(
        self, claim: Claim, evidence: Evidence, ctx: VerifyContext, cfg: VerifyConfig
    ) -> CheckOutcome:
        self.numeric_calls.append(claim.id)
        self.contexts.append(ctx)
        self.evidence.append(evidence)
        first = "fail" if self._fails(claim) else "pass"
        return CheckOutcome(
            checks=Checks(provenance_exec=first, metadata_match="pass", number_in_text="pass")
        )

    def check_text(
        self, claim: Claim, evidence: Evidence, ctx: VerifyContext, cfg: VerifyConfig
    ) -> CheckOutcome:
        self.text_calls.append(claim.id)
        self.contexts.append(ctx)
        self.evidence.append(evidence)
        first = "fail" if self._fails(claim) else "pass"
        return CheckOutcome(
            checks=Checks(quote_substring=first, metadata_match="pass", number_in_text="pass")
        )

    def decide(self, claim: Claim, outcome: CheckOutcome, cfg: VerifyConfig) -> VerifierResult:
        return VerifierResult(
            claim_id=claim.id,
            status="supported" if outcome.passed else "unsupported",
            checks=outcome.checks,
            action="keep" if outcome.passed else "drop",
        )

    def apply(
        self,
        answer: Answer,
        results: Sequence[VerifierResult],
        ctx: VerifyContext,
        cfg: VerifyConfig,
    ) -> PolicyDecision:
        kept = [c for c, r in zip(answer.claims, results, strict=True) if r.action == "keep"]
        if answer.refusal is None and kept:
            status = "verified" if len(kept) == len(answer.claims) else "partially_verified"
            return PolicyDecision(kept=kept, status=status)
        reason = answer.refusal.reason if answer.refusal else "fake refusal"
        refusal = Refusal(reason=reason, found=[c.text for c in kept])
        return PolicyDecision(kept=kept, refusal=refusal, status="refused")

    def verifier(self) -> Verifier:
        return Verifier(self.check_numeric, self.check_text, self.decide, self.apply)
