"""Metric definitions (Shekar-owned; initial stub by Claude, M04 3.7).

Every system, the baseline and the engine, is scored by these functions through the same
``Answer`` schema. A function that raises ``NotImplementedError`` leaves the question unscored
(``correct = null``); one that raises ``ValueError`` for a malformed row is recorded as a scoring
error, unscored, and the run continues (``eval/run.py``).

Accuracy definitions in force: tolerances and as-filed values are ADR-044 and ADR-017; a unit is
read from an explicit label only, never inferred from the size of the number.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.engine.answer import Answer
from eval.schemas import NumericRow

# Scale words an explicit unit label may carry. Anything else is not a unit we can reconcile.
_SCALES: dict[str, Decimal] = {
    "thousand": Decimal(10) ** 3,
    "thousands": Decimal(10) ** 3,
    "million": Decimal(10) ** 6,
    "millions": Decimal(10) ** 6,
    "billion": Decimal(10) ** 9,
    "billions": Decimal(10) ** 9,
}
_MONEY = {"usd", "$", "dollars"}
_PERCENT = {"percent", "%", "pct"}
_GOLD_UNITS = {"USD", "shares", "USD/shares", "ratio"}  # the units a NumericRow gold may carry
_PER_SHARE = {"usd/shares", "usd/share", "usd per share", "$/share", "usd", "$", "dollars"}


def refused(answer: Answer) -> bool:
    """Deterministic refusal test: ``answer.refusal is not None``.

    Used for the unanswerable set (refusal rate) and the answerable sets (false-refusal rate).
    """
    return answer.refusal is not None


def _scaled(tokens: list[str], head: set[str]) -> Decimal | None:
    """Multiplier for ``<head> [scale word]``; None when the tokens are anything else."""
    if not tokens or tokens[0] not in head or len(tokens) > 2:
        return None
    if len(tokens) == 1:
        return Decimal(1)
    return _SCALES.get(tokens[1])


def normalize_value(value: Decimal, unit: str | None, gold_unit: str) -> Decimal:
    """Bring a claimed value into the gold's unit: percent <-> ratio (29.8 % vs 0.298),
    thousands/millions/billions vs base units (gold is stored in base units, USD not millions),
    and per-share units. Return the normalized value; raise ValueError when the units can't be
    reconciled (that's an incorrect answer, not a crash).

    The claim's unit LABEL decides, never the size of the number (ADR-044 dropped scale
    inference): a bare 383285 is taken as 383,285 base units, not guessed to be millions. A
    missing or blank label (``None``, ``""``) means the gold unit, no conversion. Scale words
    apply to ``USD`` and ``shares`` golds only; ``percent`` applies to a ``ratio`` gold only.
    """
    if unit is None or not unit.strip():  # a blank label is a missing label
        return value
    label = " ".join(unit.lower().split())
    tokens = label.split(" ")
    if gold_unit == "ratio":
        if label == "ratio":
            return value
        if label in _PERCENT:
            return value / Decimal(100)
    elif gold_unit == "USD":
        mult = _scaled(tokens, _MONEY)
        if mult is not None:
            return value * mult
    elif gold_unit == "shares":
        mult = _scaled(tokens, {"shares", "share"})
        if mult is not None:
            return value * mult
    elif gold_unit == "USD/shares":
        if label in _PER_SHARE:  # FSDS and many filers label per-share amounts plain USD
            return value
    else:
        raise ValueError(f"no conversion into gold unit {gold_unit!r}")
    raise ValueError(f"cannot reconcile claim unit {unit!r} with gold unit {gold_unit!r}")


def numeric_correct(answer: Answer, row: NumericRow) -> bool:
    """True when the primary claim's value, after ``normalize_value``, is within
    ``max(row.tolerance_abs, row.tolerance_rel * |row.gold_value|)`` of ``row.gold_value``
    (ADR-044), boundary inclusive, in exact ``Decimal``.

    ``gold_value`` is the as-filed value (ADR-017): a claim equal to ``latest_value`` on a
    restated row is incorrect. At gold == 0 the relative term is 0, so ``tolerance_abs`` alone
    decides. A refusal, a missing primary claim, a claim without a value, or units that can't be
    reconciled are all incorrect. A row whose own gold unit is not one of ``_GOLD_UNITS`` is
    malformed: it raises ValueError (a scoring error, unscored, counted by the 10% guard in
    ``eval/run.py``) for every system, instead of scoring them all incorrect.
    """
    if row.unit not in _GOLD_UNITS:
        raise ValueError(f"{row.id}: gold unit {row.unit!r} is not one of {sorted(_GOLD_UNITS)}")
    if refused(answer):
        return False
    claim = answer.primary_claim()
    if claim is None or claim.kind == "text" or claim.value is None:
        return False
    try:
        value = normalize_value(claim.value, claim.unit, row.unit)
    except ValueError:
        return False
    if not value.is_finite():
        return False
    bound = max(row.tolerance_abs, row.tolerance_rel * abs(row.gold_value))
    return abs(value - row.gold_value) <= bound


def financebench_correct(judge_label: str) -> bool:
    """Map the correctness judge's label (correct | incorrect | refused) to a 0/1 score.
    Refusals count as incorrect for accuracy and are reported separately as refusal rate.
    An unknown label raises ValueError (a judge-contract break, not a wrong answer)."""
    if judge_label == "correct":
        return True
    if judge_label in ("incorrect", "refused"):
        return False
    raise ValueError(f"unknown correctness label {judge_label!r}")


def narrative_coverage(covered: Sequence[bool]) -> float:
    """Gold-point coverage in [0, 1] from the narrative judge's per-point booleans.
    A row with no gold points is malformed: ValueError, not a coverage of 0."""
    if not covered:
        raise ValueError("no gold points to cover")
    return sum(covered) / len(covered)


def citation_precision(supported: Sequence[bool]) -> float | None:
    """Share of cited chunks the support judge says support their claim; None with no
    citations (excluded from the mean, not counted as 0 or 1)."""
    if not supported:
        return None
    return sum(supported) / len(supported)


def unsupported_claim_rate(labels: Sequence[str]) -> float | None:
    """Share of claims labelled unsupported by the independent support judge (partial counts
    as decided here); None when there are no claims.

    Decided here: only the label ``unsupported`` counts. ``partial`` does not, so the rate is
    not inflated against the system being scored. An unknown label raises ValueError."""
    if not labels:
        return None
    bad = [label for label in labels if label not in ("supported", "unsupported", "partial")]
    if bad:
        raise ValueError(f"unknown support labels {sorted(set(bad))}")
    return sum(label == "unsupported" for label in labels) / len(labels)


def recall_at_k(retrieved_ids: Sequence[str], gold_ids: Sequence[str], k: int) -> float:
    """Hit-based recall@k: 1.0 if any gold id is in the first k retrieved, else 0.0
    (FinanceBench evidence pages; narrative gold chunks). This is hit@k, NOT the fractional
    share of gold ids found, and every system is scored with it. ``k < 1`` or an empty gold
    list raises ValueError (hit@k is undefined there)."""
    if k < 1:
        raise ValueError("k must be at least 1")
    if not gold_ids:
        raise ValueError("no gold ids")
    return 1.0 if set(retrieved_ids[:k]) & set(gold_ids) else 0.0
