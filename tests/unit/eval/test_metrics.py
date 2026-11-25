"""Metric definitions (eval/metrics.py, Shekar-owned): the accuracy definitions every system is
scored by (ADR-044 tolerances, ADR-017 as-filed values, no scale inference)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.engine.answer import Answer, ChunkProvenance, Claim, Refusal
from app.engine.baseline import NUMERIC_UNITS
from eval import metrics
from eval.schemas import NumericRow

D = Decimal


def row(gold: str = "383285000000", unit: str = "USD", **kw: object) -> NumericRow:
    base: dict[str, object] = dict(
        id="n-1",
        question="q",
        qtype="single_value",
        cik=1,
        metric="m",
        fiscal_year=2022,
        fiscal_period="FY",
        gold_value=D(gold),
        unit=unit,
        tolerance_rel=D("0.0005"),
        tolerance_abs=D("0.5"),
        gold_fact_ids=[1],
        template_id="t",
        paraphrased=False,
        second_source_check="match",
    )
    base.update(kw)
    return NumericRow(**base)  # type: ignore[arg-type]


def ans(value: str | None, unit: str | None, kind: str = "numeric", primary: bool = True) -> Answer:
    claim = Claim(
        id="c1",
        kind=kind,  # type: ignore[arg-type]
        text="t",
        value=None if value is None else D(value),
        unit=unit,
        primary=primary,
        provenance=ChunkProvenance(type="chunk", chunk_id="a#1#0", quote="q"),
    )
    return Answer(answer_markdown="a", claims=[claim])


# --- normalize_value ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "unit", "gold_unit", "expected"),
    [
        ("383285000000", "USD", "USD", "383285000000"),
        ("383285", "USD millions", "USD", "383285000000"),
        ("383.285", "USD billions", "USD", "383285000000"),
        ("383285000", "USD thousands", "USD", "383285000000"),
        ("383.285", "usd   Billions", "USD", "383285000000"),  # case and spacing
    ],
)
def test_scale_words_convert_usd(value: str, unit: str, gold_unit: str, expected: str) -> None:
    assert metrics.normalize_value(D(value), unit, gold_unit) == D(expected)


def test_scale_words_convert_shares() -> None:
    assert metrics.normalize_value(D("15.5"), "shares millions", "shares") == D(15_500_000)
    assert metrics.normalize_value(D(15_500_000), "shares", "shares") == D(15_500_000)


@pytest.mark.parametrize("unit", ["percent", "%", "PCT"])
def test_percent_to_ratio(unit: str) -> None:
    assert metrics.normalize_value(D("29.8"), unit, "ratio") == D("0.298")


def test_ratio_label_is_not_rescaled() -> None:
    assert metrics.normalize_value(D("0.298"), "ratio", "ratio") == D("0.298")


@pytest.mark.parametrize("unit", ["USD/shares", "USD per share", "usd", "$"])
def test_per_share_labels(unit: str) -> None:
    assert metrics.normalize_value(D("6.13"), unit, "USD/shares") == D("6.13")


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_unit_is_a_missing_unit(blank: str) -> None:
    """A model that emits "" instead of null must not be scored differently."""
    assert metrics.normalize_value(D("383285000000"), blank, "USD") == D("383285000000")
    assert metrics.numeric_correct(ans("383285000000", blank), row())


# The unit vocabulary the baseline composer is told to use, and the gold unit each belongs to.
PROMPT_UNIT_GOLD = {
    "USD": "USD",
    "USD thousands": "USD",
    "USD millions": "USD",
    "USD billions": "USD",
    "USD/shares": "USD/shares",
    "shares": "shares",
    "shares thousands": "shares",
    "shares millions": "shares",
    "percent": "ratio",
    "ratio": "ratio",
}


def test_the_composer_prompt_vocabulary_is_exactly_what_the_normalizer_reads() -> None:
    """A label the prompt offers but normalize_value rejects would score a right answer wrong."""
    assert set(PROMPT_UNIT_GOLD) == set(NUMERIC_UNITS)  # adding a label forces a decision here
    for label, gold_unit in PROMPT_UNIT_GOLD.items():
        metrics.normalize_value(D(1), label, gold_unit)  # raises ValueError if unreadable


def test_full_precision_value_passes_where_the_rounded_display_number_fails() -> None:
    """ADR-044: Claim.value keeps every digit; 1.58 'USD billions' is 3,000,000 off 1,583M."""
    r = row(gold="1583000000")
    assert metrics.numeric_correct(ans("1583", "USD millions"), r)
    assert not metrics.numeric_correct(ans("1.58", "USD billions"), r)


def test_missing_unit_is_the_gold_unit() -> None:
    """Option A: no label, no conversion. A right base-unit number passes; nothing is guessed."""
    assert metrics.normalize_value(D("383285000000"), None, "USD") == D("383285000000")
    assert metrics.normalize_value(D("29.8"), None, "ratio") == D("29.8")  # still 100x off


@pytest.mark.parametrize(
    ("unit", "gold_unit"),
    [
        ("percent", "USD"),  # percent against a money gold
        ("USD millions", "ratio"),
        ("USD millions", "USD/shares"),  # scale words never apply to per-share
        ("shares", "USD"),
        ("USD", "shares"),
        ("EUR", "USD"),
        ("USD millions millions", "USD"),
        ("USD furlongs", "USD"),
        ("USD", "ratio"),
        ("USD", "pure"),  # unknown gold unit
    ],
)
def test_irreconcilable_units_raise(unit: str, gold_unit: str) -> None:
    with pytest.raises(ValueError):
        metrics.normalize_value(D(1), unit, gold_unit)


@given(st.decimals(min_value=D("-1e6"), max_value=D("1e6"), places=4, allow_nan=False))
def test_percent_ratio_round_trip_is_exact(v: Decimal) -> None:
    assert metrics.normalize_value(v * 100, "percent", "ratio") == v


@given(st.integers(min_value=1, max_value=10**6), st.sampled_from(["thousands", "millions"]))
def test_magnitude_alone_never_triggers_a_conversion(n: int, scale: str) -> None:
    """Without the scale word the value is taken as base units, however small it looks."""
    assert metrics.normalize_value(D(n), "USD", "USD") == D(n)
    assert metrics.normalize_value(D(n), f"USD {scale}", "USD") != D(n)


# --- numeric_correct ----------------------------------------------------------------------------


def test_exact_and_four_sig_fig_answers_are_correct() -> None:
    r = row()
    assert metrics.numeric_correct(ans("383285000000", "USD"), r)
    assert metrics.numeric_correct(ans("383.3", "USD billions"), r)  # 4 s.f.
    assert not metrics.numeric_correct(ans("380", "USD billions"), r)  # 0.86% off


def test_boundary_is_inclusive() -> None:
    r = row(gold="1000000", tolerance_rel=D("0.001"), tolerance_abs=D(0))
    assert metrics.numeric_correct(ans("1001000", "USD"), r)  # exactly 0.1%
    assert not metrics.numeric_correct(ans("1001001", "USD"), r)


def test_gold_zero_uses_the_absolute_tolerance_only() -> None:
    r = row(gold="0", tolerance_rel=D("0.0005"), tolerance_abs=D("0.5"))
    assert metrics.numeric_correct(ans("0", "USD"), r)
    assert metrics.numeric_correct(ans("0.5", "USD"), r)
    assert not metrics.numeric_correct(ans("1", "USD"), r)


def test_gold_zero_with_no_absolute_tolerance_needs_an_exact_zero() -> None:
    r = row(gold="0", tolerance_rel=D("0.01"), tolerance_abs=D(0))  # a pre-ADR-044 row
    assert metrics.numeric_correct(ans("0", "USD"), r)
    assert not metrics.numeric_correct(ans("0.0001", "USD"), r)


def test_max_of_the_two_tolerances_not_the_sum_or_the_min() -> None:
    r = row(gold="100", tolerance_rel=D("0.001"), tolerance_abs=D("0.5"))  # rel 0.1, abs 0.5
    assert metrics.numeric_correct(ans("100.5", "USD"), r)  # abs wins
    assert not metrics.numeric_correct(ans("100.6", "USD"), r)  # neither covers it (not 0.6)


def test_per_share_uses_half_a_cent() -> None:
    r = row(gold="6.13", unit="USD/shares", tolerance_rel=D(0), tolerance_abs=D("0.005"))
    assert metrics.numeric_correct(ans("6.135", "USD/shares"), r)
    assert not metrics.numeric_correct(ans("6.14", "USD/shares"), r)


def test_percent_answer_against_a_ratio_gold() -> None:
    r = row(gold="0.2982", unit="ratio", tolerance_rel=D(0), tolerance_abs=D("0.0005"))
    assert metrics.numeric_correct(ans("29.8", "percent", kind="numeric_derived"), r)
    assert not metrics.numeric_correct(ans("29.8", "ratio", kind="numeric_derived"), r)
    assert not metrics.numeric_correct(ans("30.1", "percent", kind="numeric_derived"), r)


def test_as_originally_reported_is_the_gold_not_the_restatement() -> None:
    r = row(gold="100000000", latest_value=D(90_000_000), is_restated=True)
    assert metrics.numeric_correct(ans("100000000", "USD"), r)
    assert not metrics.numeric_correct(ans("90000000", "USD"), r)


def test_unlabelled_millions_is_incorrect_not_guessed() -> None:
    assert not metrics.numeric_correct(ans("383285", None), row())


def test_irreconcilable_units_score_incorrect_not_crash() -> None:
    assert not metrics.numeric_correct(ans("29.8", "percent"), row())


def test_refusal_and_missing_claims_are_incorrect() -> None:
    r = row()
    refusal = Answer(answer_markdown="no", refusal=Refusal(reason="no data"))
    assert not metrics.numeric_correct(refusal, r)
    assert not metrics.numeric_correct(Answer(answer_markdown="x"), r)
    assert not metrics.numeric_correct(ans(None, None, kind="text"), r)
    assert not metrics.numeric_correct(ans(None, "USD"), r)  # a numeric claim with no value


def test_the_primary_claim_is_the_one_scored() -> None:
    other = Claim(
        id="c0",
        kind="numeric",
        text="t",
        value=D("383285000000"),
        unit="USD",
        provenance=ChunkProvenance(type="chunk", chunk_id="a#1#0", quote="q"),
    )
    a = ans("1", "USD")  # the primary claim is wrong
    a.claims.insert(0, other)  # a right, non-primary claim comes first
    assert not metrics.numeric_correct(a, row())


@given(st.decimals(min_value=D("-1e15"), max_value=D("1e15"), places=2, allow_nan=False))
def test_gold_is_always_correct_against_itself(g: Decimal) -> None:
    r = row(gold=str(g))
    assert metrics.numeric_correct(ans(str(g), "USD"), r)


# --- the other definitions ----------------------------------------------------------------------


def test_refused() -> None:
    assert metrics.refused(Answer(answer_markdown="x", refusal=Refusal(reason="r")))
    assert not metrics.refused(Answer(answer_markdown="x"))


@pytest.mark.parametrize(
    ("label", "expected"), [("correct", True), ("incorrect", False), ("refused", False)]
)
def test_financebench_correct(label: str, expected: bool) -> None:
    assert metrics.financebench_correct(label) is expected


def test_financebench_correct_rejects_an_unknown_label() -> None:
    with pytest.raises(ValueError):
        metrics.financebench_correct("maybe")


def test_narrative_coverage() -> None:
    assert metrics.narrative_coverage([True, False, True, True]) == 0.75
    assert metrics.narrative_coverage([False]) == 0.0
    with pytest.raises(ValueError):
        metrics.narrative_coverage([])  # a row with no gold points is malformed, not 0


def test_citation_precision() -> None:
    assert metrics.citation_precision([True, True, False, True]) == 0.75
    assert metrics.citation_precision([]) is None  # excluded from the mean, not 0 or 1


def test_unsupported_claim_rate_counts_only_unsupported() -> None:
    labels = ["supported", "unsupported", "partial", "supported"]
    assert metrics.unsupported_claim_rate(labels) == 0.25  # partial is not counted
    assert metrics.unsupported_claim_rate([]) is None
    with pytest.raises(ValueError):
        metrics.unsupported_claim_rate(["supported", "bogus"])


def test_recall_at_k_is_hit_at_k() -> None:
    got = ["a", "b", "c", "d"]
    assert metrics.recall_at_k(got, ["c", "zzz"], 3) == 1.0  # one hit is enough
    assert metrics.recall_at_k(got, ["c"], 2) == 0.0
    assert metrics.recall_at_k(got, ["a", "zzz"], 1) == 1.0  # not 0.5: hit@k, not fractional
    assert metrics.recall_at_k([], ["a"], 5) == 0.0


def test_recall_at_k_rejects_undefined_inputs() -> None:
    with pytest.raises(ValueError):
        metrics.recall_at_k(["a"], [], 5)  # no gold: hit@k is undefined
    with pytest.raises(ValueError):
        metrics.recall_at_k(["a"], ["a"], 0)


@given(st.lists(st.sampled_from("abcdef"), max_size=8), st.integers(min_value=1, max_value=10))
def test_recall_at_k_is_monotone_in_k(got: list[str], k: int) -> None:
    assert metrics.recall_at_k(got, ["a", "b"], k) <= metrics.recall_at_k(got, ["a", "b"], k + 1)
