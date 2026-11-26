"""Deterministic check of a numeric claim (M06 3.2, ADR-049 D1-D3). **Shekar-owned.**
Stub: set ``IMPLEMENTED = True`` once ``check_numeric`` is written.

``check_numeric`` is called for every claim of kind ``numeric`` or ``numeric_derived``. No LLM
sees such a claim. It returns ``CheckOutcome(checks, reason)``; set each of the three checks it
decides to ``pass`` or ``fail`` (``n/a`` when an earlier failure makes it undecidable), leave
``quote_substring`` and ``entailment`` at ``n/a``, and say in ``reason`` what failed.

provenance_exec
  - The provenance is ``expr`` (a numeric claim citing a chunk fails here).
  - **D2:** ``(expr, set(fact_ids))`` equals that of a ``ctx.results`` entry (the *matched
    result*), or of a restated note on one: expr ``f:<note.latest_fact_id>``, only when the note
    has a ``latest_value`` and the result's unit is not ``ratio``. An expression the compiler did
    not build fails, even if its arithmetic is right.
  - Every cited fact is in ``evidence.facts``.
  - Re-evaluate ``expr`` in ``Decimal`` over the facts' values: ``v*``. (``app/engine/answer.py``
    has the grammar; a fact id may be negative.)
  - A cited fact with ``derived_from`` ([FY, 9M]) is recomputed: both parents are in the
    evidence and ``first.value - second.value`` equals the stored value. A missing parent fails
    (D7: fail closed).
  - **D1:** ``claim.value`` is present and ``|claim.value - v*| <=
    numbers.value_tolerance(v*, claim.display, rel=cfg.rel_tolerance)``. A ``display``, when
    present, states ``v*`` within the same tolerance.

number_in_text
  - At least one number of kind ``value`` in ``claim.text`` matches ``v*`` (``numbers.matches``:
    its own precision only).
  - No **other** ``value`` number in the text is unsupported. Years and labels are not values.
  - Percent and ratio are the same thing in base units.
  - With a direction word, a magnitude may be written unsigned: "decreased 36.7%" states -0.367
    (see the direction rule below). Without one, the sign must match.

metadata_match (facts)
  - Each cited fact is ``us-gaap``; its ``cik`` is the matched result's ``cik`` (for a
    ``difference``, ``cik`` or ``compare_cik``) and is in ``ctx.allowed_ciks`` when that is not
    empty.
  - Its fiscal year and period are the matched result's; for ``growth`` also the base period,
    which is not always t-1 (``compose.composer.growth_base(result, ctx.plan)``). A derived Q4
    row is matched on its own period; its FY and 9M parents are not period-matched.
  - Its ``tag`` is one of the catalog tags of the result's metric, or of an operand of its
    formula or fallback formula (``plan.catalog.dependencies(metric, ctx.catalog)``).
  - Its ``uom`` fits: USD metrics use ``USD``; ``USD/shares`` metrics accept ``USD`` or
    ``USD/shares``; ``shares`` uses ``shares``; a ratio's inputs use their own metrics' units.

metadata_match (wording, D3; fails this same check)
  - **Years:** every ``year`` number in the text is the matched result's fiscal year, the growth
    base year, or the calendar year of a cited fact's ``period_end`` (a fiscal 2023 may end in
    January 2024).
  - **Quarter:** every quarter named (``Q2``, "second quarter") is the matched result's (or, for
    growth, the base's). A full-year result's claim names no quarter.
  - **Company:** when the text names a company in ``ctx.companies`` (by name, ticker or alias),
    those named are exactly the matched result's ``cik`` (with ``compare_cik`` for a
    difference; a difference may name either or both). Naming another company fails, so two
    compare claims with their names swapped both fail. A claim naming no company passes.
  - **Direction:** for ``growth`` and ``difference``, "decreased / declined / fell / lower"
    requires ``v* < 0`` and "increased / grew / rose / higher" requires ``v* > 0``.
  - **Not checked (documented limit):** the metric's name in the text ("operating income" over a
    net-income fact passes).
"""

from __future__ import annotations

from app.engine.answer import Claim
from app.engine.verify.models import CheckOutcome, Evidence, VerifyConfig, VerifyContext

IMPLEMENTED = False


def check_numeric(
    claim: Claim, evidence: Evidence, ctx: VerifyContext, cfg: VerifyConfig
) -> CheckOutcome:
    """Check one numeric claim against the facts, the compiled results and its own wording."""
    raise NotImplementedError("Shekar-owned: verify/numeric.check_numeric")
