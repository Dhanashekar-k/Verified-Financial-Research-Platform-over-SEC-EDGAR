"""Resolve a multi-tag metric (revenue, cost of revenue) to ONE candidate value, or abstain.

The M4 catalog review (``docs/m4/m4_catalog_decisions.md``, ADR-043) found that candidate tags
are not synonyms: ``RevenueFromContractWithCustomerExcludingAssessedTax`` and ``Revenues``
differ by more than 1% in 849 of 1,328 same-period pairs, and Block's ``CostOfRevenue`` is
$6.76B where ``CostOfGoodsAndServicesSold`` is $143.9M. "First populated tag wins" would put a
component, or a different scope, into gold. This module implements the review's selection
sequence for one (cik, fiscal_year, fiscal_period):

1. The metric definition fixes the candidate tags (the catalog chain; tax-inclusive and bank
   revenue are separate metrics with their own presentation basis, never candidates here).
2-3. The caller collects candidate facts for the exact entity and period, already screened by
   ``numeric_policy`` (us-gaap, consolidated, unit, qtrs, no derived Q4) and collapsed to one
   version per tag by the declared version policy (step 5).
4. Prefer the consolidated statement presentation: a tag the filing presents on its income
   statement (FSDS ``pre``, ``stmt = 'IS'``, not parenthetical) beats one that only appears in
   the notes (presence on the statement, not proof the line is the total: ADR-043 limits).
   Validate against the calculation relationship revenue - cost of revenue = gross
   profit when the filing reports all three.
6. If incompatible candidates remain, ABSTAIN with the candidates exposed. Never take the first
   populated value.

Candidates that agree within the metric's tolerance are the same number reported twice; the
chain's preference order only breaks that tie (it then cites the preferred tag).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from eval.gen.numeric_policy import Versioned, agree

Reason = Literal[
    "single_candidate",
    "candidates_agree",
    "presented_on_income_statement",
    "reconciles_with_gross_profit",
    "no_candidate",
    "multiple_periods",
    "conflicting_candidates",
    "presentation_contradicts_calculation",
]
SELECTED: frozenset[str] = frozenset(
    {
        "single_candidate",
        "candidates_agree",
        "presented_on_income_statement",
        "reconciles_with_gross_profit",
    }
)


@dataclass(frozen=True)
class Candidate:
    tag: str
    value: Decimal
    fact_id: int
    presented: bool | None  # None: no presentation data for this filing


@dataclass(frozen=True)
class Resolution:
    metric: str
    reason: Reason
    chosen: Versioned | None
    candidates: tuple[Candidate, ...]

    @property
    def ok(self) -> bool:
        return self.chosen is not None

    @property
    def tag(self) -> str | None:
        return self.chosen.fact.tag if self.chosen is not None else None


def _distinct(values: Sequence[Versioned], unit: str) -> list[Versioned]:
    """One representative per group of mutually agreeing values (first in chain order)."""
    reps: list[Versioned] = []
    for v in values:
        if not any(agree(v.value, r.value, unit) for r in reps):
            reps.append(v)
    return reps


def resolve(
    metric: str,
    chain: Sequence[str],
    candidates: Sequence[Versioned],
    *,
    unit: str = "USD",
    presented_tags: frozenset[str] | None = None,
    reconciles: Callable[[Decimal], bool | None] | None = None,
) -> Resolution:
    """Pick the consolidated total among ``candidates`` (one Versioned per tag, same entity
    and period), or abstain.

    ``presented_tags``: tags this filing presents on its income statement (None when the
    presentation is unknown). ``reconciles(value)``: True / False when the value does / doesn't
    satisfy the gross-profit identity, None when that can't be checked (an input is missing).
    """
    order = {t: i for i, t in enumerate(chain)}
    cands = sorted((c for c in candidates if c.fact.tag in order), key=lambda c: order[c.fact.tag])
    exposed = tuple(
        Candidate(
            c.fact.tag,
            c.value,
            c.fact.fact_id,
            None if presented_tags is None else c.fact.tag in presented_tags,
        )
        for c in cands
    )

    def done(reason: Reason, chosen: Versioned | None = None) -> Resolution:
        return Resolution(metric, reason, chosen, exposed)

    if not cands:
        return done("no_candidate")
    if len({c.fact.tag for c in cands}) != len(cands):
        return done("multiple_periods")  # one tag, several periods for one label: never guess
    if len({c.fact.period_end for c in cands}) > 1:
        return done("multiple_periods")
    groups = _distinct(cands, unit)
    if len(groups) == 1:
        return done("single_candidate" if len(cands) == 1 else "candidates_agree", groups[0])

    # Material disagreement. Statement presentation first, calculation check second.
    calc = {id(g): (reconciles(g.value) if reconciles else None) for g in groups}
    if presented_tags is not None:
        shown = [
            g
            for g in groups
            if any(c.fact.tag in presented_tags and agree(c.value, g.value, unit) for c in cands)
        ]
        if len(shown) == 1:
            # cite the presented candidate itself, not the group's chain-first member
            pick = next(
                c
                for c in cands
                if c.fact.tag in presented_tags and agree(c.value, shown[0].value, unit)
            )
            if calc[id(shown[0])] is False and any(calc[id(g)] is True for g in groups):
                return done("presentation_contradicts_calculation")
            return done("presented_on_income_statement", pick)
    passing = [g for g in groups if calc[id(g)] is True]
    if len(passing) == 1:
        return done("reconciles_with_gross_profit", passing[0])
    return done("conflicting_candidates")


def gross_profit_check(
    gross_profit: Decimal | None,
    counterpart: Sequence[Decimal],
    *,
    role: Literal["revenue", "cost"],
) -> Callable[[Decimal], bool | None]:
    """``reconciles`` for ``resolve``: revenue - cost = gross profit within tolerance.

    For revenue candidates ``counterpart`` holds the cost-of-revenue candidate values; for cost
    candidates it holds the revenue candidates. Any counterpart value that closes the identity
    counts (the counterpart's own ambiguity is resolved separately)."""

    def check(value: Decimal) -> bool | None:
        if gross_profit is None or not counterpart:
            return None
        for other in counterpart:
            implied = value - other if role == "revenue" else other - value
            if agree(implied, gross_profit, "USD"):
                return True
        return False

    return check
