"""``answer_markdown`` rebuilt from the claims that survive (M06 3.4).

The composer's ``answer_markdown`` is never shown: a dropped claim cannot leave its sentence
behind. What is written here is a kept claim's text, the compiler's own note about a kept figure,
a period line built from the plan, or the ``reason`` of the refusal the policy decided. That
reason can be the composer's own sentence (D6 keeps it unless it adds an unsupported number), so
for a refused answer unverified composer wording can still appear: ``docs/m6/spec-questions.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.engine.answer import Claim, ExprProvenance
from app.engine.plan.schema import NumericResult, PeriodRef
from app.engine.verify.models import PolicyDecision, VerifyContext

SUPPORTED_HEADING = "What the filings do support:"


def _label(period: PeriodRef) -> str:
    if period.fiscal_period == "FY":
        return f"FY{period.fiscal_year}"
    return f"{period.fiscal_period} FY{period.fiscal_year}"


def _results_cited(claim: Claim, results: Sequence[NumericResult]) -> list[NumericResult]:
    """The compiled results a claim rests on: its own expression, or a restated note's latest."""
    p = claim.provenance
    if not isinstance(p, ExprProvenance):
        return []
    ids = set(p.fact_ids)
    return [
        r
        for r in results
        if (p.expr == r.expr and ids == set(r.fact_ids))
        or any(
            n.latest_fact_id is not None
            and p.expr == f"f:{n.latest_fact_id}"
            and ids == {n.latest_fact_id}
            for n in r.notes
        )
    ]


def compiler_notes(kept: Sequence[Claim], results: Sequence[NumericResult]) -> list[str]:
    """The must-mention notes of the figures that are kept, each once, in claim order."""
    notes = [
        n.text
        for claim in kept
        for r in _results_cited(claim, results)
        for n in r.notes
        if n.must_mention
    ]
    return list(dict.fromkeys(notes))


def period_line(ctx: VerifyContext) -> str | None:
    """Says that code, not the user, chose the period. The engine's own period note is written
    for the composer ("Say so in the answer.") and is never shown."""
    if not ctx.assumed_period or not ctx.plan.periods:
        return None
    periods = ", ".join(_label(p) for p in ctx.plan.periods)
    if ctx.plan.period_text:
        read = f'"{ctx.plan.period_text}" was read as {periods}'
    else:
        read = f"No period was named, so this uses {periods}"
    return f"{read}, based on the latest filings on record."


def render(decision: PolicyDecision, ctx: VerifyContext) -> str:
    """Kept claims in composer order; ``[n]`` is the claim's position in ``Answer.claims``."""
    cited = [f"{claim.text.strip()} [{n}]" for n, claim in enumerate(decision.kept, start=1)]
    extra = [f"Note: {note}" for note in compiler_notes(decision.kept, ctx.results)]
    period = period_line(ctx)
    if period and cited:
        extra.append(period)
    if decision.refusal is None:
        parts = [" ".join(cited), *extra]
    else:
        parts = [decision.refusal.reason]
        if cited:
            parts += [
                "\n".join([SUPPORTED_HEADING, *(f"- {line}" for line in cited)]),
                *extra,
            ]
    return "\n\n".join(p for p in parts if p)
