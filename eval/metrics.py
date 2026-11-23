"""Metric definitions (Shekar-owned; initial stub by Claude, M04 3.7).

Every function below raises ``NotImplementedError`` until Shekar writes it. ``eval/run.py``
catches that and records the question as unscored (``correct = null``), so the harness runs
end to end before the definitions exist. The baseline and the engine are scored by these same
functions through the same ``Answer`` schema.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.engine.answer import Answer
from eval.schemas import NumericRow


def refused(answer: Answer) -> bool:
    """Deterministic refusal test: ``answer.refusal is not None``.

    Used for the unanswerable set (refusal rate) and the answerable sets (false-refusal rate).
    """
    raise NotImplementedError


def normalize_value(value: Decimal, unit: str | None, gold_unit: str) -> Decimal:
    """Bring a claimed value into the gold's unit: percent <-> ratio (29.8 % vs 0.298),
    thousands/millions/billions vs base units (gold is stored in base units, USD not millions),
    and per-share units. Return the normalized value; raise ValueError when the units can't be
    reconciled (that's an incorrect answer, not a crash)."""
    raise NotImplementedError


def numeric_correct(answer: Answer, row: NumericRow) -> bool:
    """True when the primary claim's value, after ``normalize_value``, is within
    ``row.tolerance_rel`` of ``row.gold_value`` (relative error; define the gold == 0 case).
    A refusal or a missing primary numeric claim is incorrect."""
    raise NotImplementedError


def financebench_correct(judge_label: str) -> bool:
    """Map the correctness judge's label (correct | incorrect | refused) to a 0/1 score.
    Refusals count as incorrect for accuracy and are reported separately as refusal rate."""
    raise NotImplementedError


def narrative_coverage(covered: Sequence[bool]) -> float:
    """Gold-point coverage in [0, 1] from the narrative judge's per-point booleans."""
    raise NotImplementedError


def citation_precision(supported: Sequence[bool]) -> float | None:
    """Share of cited chunks the support judge says support their claim; None with no
    citations (excluded from the mean, not counted as 0 or 1)."""
    raise NotImplementedError


def unsupported_claim_rate(labels: Sequence[str]) -> float | None:
    """Share of claims labelled unsupported by the independent support judge (partial counts
    as decided here); None when there are no claims."""
    raise NotImplementedError


def recall_at_k(retrieved_ids: Sequence[str], gold_ids: Sequence[str], k: int) -> float:
    """Hit-based recall@k: 1.0 if any gold id is in the first k retrieved, else 0.0
    (FinanceBench evidence pages; narrative gold chunks). Say in the definition whether this is
    hit@k or fractional recall, and use one definition for every system."""
    raise NotImplementedError
