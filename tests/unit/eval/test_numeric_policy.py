"""Data-quality policies for numeric gold (ADR-044): screening, versions, tolerances."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from eval.gen.numeric_policy import (
    Fact,
    ScreenLog,
    agree,
    denominator_ok,
    normalize_uom,
    screen,
    select_versions,
    tolerance_for,
    uom_matches,
)


def fact(**kw: object) -> Fact:
    base: dict[str, object] = dict(
        fact_id=1,
        cik=10,
        tag="Revenues",
        taxonomy="us-gaap",
        uom="USD",
        qtrs=4,
        period_start=date(2022, 1, 1),
        period_end=date(2022, 12, 31),
        fiscal_year=2022,
        fiscal_period="FY",
        value=Decimal("1000000000"),
        accession="0000000010-23-000001",
        form="10-K",
        filed=date(2023, 2, 1),
        is_latest=True,
        is_original=True,
        is_restated=False,
        derived_from=None,
    )
    base.update(kw)
    return Fact(**base)  # type: ignore[arg-type]


# --- screening ---------------------------------------------------------------------------------


def test_qtrs0_on_duration_metric_is_quarantined_and_logged() -> None:
    log = ScreenLog()
    f = fact(qtrs=0)
    assert not screen(f, unit="USD", period_type="duration", log=log)
    assert [q.reason for q in log.quarantined] == ["qtrs0_on_duration_metric"]
    assert log.quarantined[0].fact_id == f.fact_id


@pytest.mark.parametrize("tag", ["EarningsPerShareBasic", "EarningsPerShareDiluted"])
def test_fsds_usd_eps_is_admitted_and_normalized_to_per_share(tag: str) -> None:
    """FSDS records EPS with uom 'USD'; companyfacts files it under 'USD/shares' (ADR-044)."""
    log = ScreenLog()
    f = fact(tag=tag, uom="USD", value=Decimal("2.5000"))
    assert screen(f, unit="USD/shares", period_type="duration", log=log)
    assert not log.quarantined
    assert normalize_uom(f, "USD/shares").uom == "USD/shares"
    assert normalize_uom(f, "USD/shares").value == f.value


@pytest.mark.parametrize(
    ("uom", "unit"),
    [
        ("CAD", "USD/shares"),  # foreign-currency EPS: still a mismatch
        ("USD", "shares"),  # the alias is scoped to per-share metrics only
        ("USD/shares", "USD"),
        ("shares", "USD"),
    ],
)
def test_other_unit_mismatches_stay_quarantined(uom: str, unit: str) -> None:
    log = ScreenLog()
    assert not screen(fact(uom=uom), unit=unit, period_type="duration", log=log)
    assert log.quarantined[0].reason == "uom_mismatch"


@pytest.mark.parametrize("unit", ["USD", "USD/shares", "shares", "ratio"])
@pytest.mark.parametrize("uom", ["USD", "USD/shares", "shares", "CAD", "pure"])
def test_every_admitted_fact_is_relabelled_to_the_metric_unit(uom: str, unit: str) -> None:
    """Admission (uom_matches) and relabelling (normalize_uom) can't drift apart."""
    f = fact(uom=uom)
    if uom_matches(uom, unit):
        assert normalize_uom(f, unit).uom == unit
    else:
        assert normalize_uom(f, unit) is f


def test_normalize_leaves_non_per_share_facts_alone() -> None:
    f = fact(uom="USD")
    assert normalize_uom(f, "USD") is f
    assert normalize_uom(fact(uom="CAD"), "USD/shares").uom == "CAD"


@pytest.mark.parametrize(("fp", "qtrs"), [("Q2", 2), ("Q3", 3), ("Q1", 4), ("FY", 1)])
def test_ytd_or_wrong_length_never_stands_in_for_the_period(fp: str, qtrs: int) -> None:
    log = ScreenLog()
    assert not screen(
        fact(fiscal_period=fp, qtrs=qtrs), unit="USD", period_type="duration", log=log
    )
    assert log.excluded == {"ytd_or_period_length_mismatch": 1}
    assert not log.quarantined


@pytest.mark.parametrize("fp", ["H1", "9M"])
def test_h1_and_9m_are_never_gold_periods(fp: str) -> None:
    log = ScreenLog()
    assert not screen(fact(fiscal_period=fp, qtrs=2), unit="USD", period_type="duration", log=log)
    assert log.excluded == {"not_a_gold_period": 1}


def test_quarter_and_fy_with_right_length_pass() -> None:
    log = ScreenLog()
    assert screen(fact(), unit="USD", period_type="duration", log=log)
    assert screen(fact(fiscal_period="Q3", qtrs=1), unit="USD", period_type="duration", log=log)
    assert not log.quarantined and not log.excluded


def test_instant_needs_qtrs0() -> None:
    log = ScreenLog()
    assert screen(fact(qtrs=0, tag="Assets"), unit="USD", period_type="instant", log=log)
    assert not screen(fact(qtrs=4, tag="Assets"), unit="USD", period_type="instant", log=log)
    assert log.quarantined[0].reason == "nonzero_qtrs_on_instant_metric"


def test_derived_q4_excluded_unless_allowed() -> None:
    f = fact(fiscal_period="Q4", qtrs=1, derived_from=(7, 8))
    log = ScreenLog()
    assert not screen(f, unit="USD", period_type="duration", log=log)
    assert log.excluded == {"derived_q4": 1}
    assert screen(f, unit="USD", period_type="duration", log=ScreenLog(), allow_derived_q4=True)


def test_custom_taxonomy_excluded() -> None:
    log = ScreenLog()
    assert not screen(fact(taxonomy="custom"), unit="USD", period_type="duration", log=log)
    assert log.excluded == {"non_us_gaap_taxonomy": 1}


# --- versions ---------------------------------------------------------------------------------


def _restated_pair() -> list[Fact]:
    orig = fact(fact_id=1, value=Decimal(100_000_000), is_latest=False, is_restated=True)
    latest = fact(
        fact_id=2,
        value=Decimal(90_000_000),
        is_original=False,
        is_restated=True,
        accession="0000000010-24-000001",
        filed=date(2024, 2, 1),
    )
    return [latest, orig]


def test_as_filed_gold_is_the_original_and_records_the_latest() -> None:
    (v,) = select_versions(_restated_pair(), "as_filed")
    assert v.fact.fact_id == 1 and v.value == Decimal(100_000_000)
    assert v.latest_value == Decimal(90_000_000) and v.is_restated


def test_latest_restated_policy_picks_the_latest() -> None:
    (v,) = select_versions(_restated_pair(), "latest_restated")
    assert v.fact.fact_id == 2 and v.latest_value == Decimal(90_000_000)
    assert v.original_value == Decimal(100_000_000)


def test_single_collapsed_row_is_not_restated() -> None:
    (v,) = select_versions([fact()], "as_filed")
    assert not v.is_restated and v.latest_value is None


def test_distinct_periods_stay_separate_groups() -> None:
    vs = select_versions([fact(), fact(fact_id=3, period_end=date(2021, 12, 31))])
    assert len(vs) == 2


# --- tolerances --------------------------------------------------------------------------------


def test_usd_tolerance_is_tighter_than_flat_one_percent_on_large_values() -> None:
    tol = tolerance_for("USD", Decimal("383285000000"))
    assert tol.accepts(Decimal("383300000000"), Decimal("383285000000"))  # $383.3B, 4 s.f.
    assert not tol.accepts(Decimal("380000000000"), Decimal("383285000000"))  # 0.86% off: wrong


@pytest.mark.parametrize("unit", ["USD", "shares"])
def test_zero_and_round_golds_are_not_loosened(unit: str) -> None:
    """A reporting scale guessed from trailing zeros would accept +-500,000 around these."""
    zero = tolerance_for(unit, Decimal(0))
    assert zero.accepts(Decimal(0), Decimal(0)) and not zero.accepts(Decimal(1), Decimal(0))
    assert not agree(Decimal(0), Decimal(400_000), unit)
    round_ = tolerance_for(unit, Decimal(3_000_000))
    assert round_.accepts(Decimal(3_001_000), Decimal(3_000_000))  # 4 s.f.
    assert not round_.accepts(Decimal(3_400_000), Decimal(3_000_000))
    assert not agree(Decimal(3_000_000), Decimal(3_400_000), unit)


def test_per_share_tolerance_is_half_a_cent() -> None:
    tol = tolerance_for("USD/shares", Decimal("0.05"))
    assert tol.accepts(Decimal("0.054"), Decimal("0.05"))
    assert not tol.accepts(Decimal("0.06"), Decimal("0.05"))  # 1% would accept 0.0505 only


def test_ratio_tolerance_is_five_hundredths_of_a_point() -> None:
    tol = tolerance_for("ratio", Decimal("0.2345"))
    assert tol.accepts(Decimal("0.2349"), Decimal("0.2345"))
    assert not tol.accepts(Decimal("0.236"), Decimal("0.2345"))


def test_unknown_unit_has_no_tolerance() -> None:
    with pytest.raises(ValueError):
        tolerance_for("EUR", Decimal(1))


@given(st.integers(min_value=1, max_value=10**15), st.sampled_from(["USD", "shares"]))
def test_gold_always_accepts_itself_and_its_four_sig_fig_rounding(n: int, unit: str) -> None:
    gold = Decimal(n)
    tol = tolerance_for(unit, gold)
    assert tol.accepts(gold, gold)
    rounded = Decimal(f"{gold:.4g}") if gold >= 1000 else gold
    assert tol.accepts(rounded, gold)


def test_agree_is_symmetric_on_a_material_gap() -> None:
    a, b = Decimal("6760000000"), Decimal("143900000")  # Block: CostOfRevenue vs COGS
    assert not agree(a, b, "USD") and not agree(b, a, "USD")
    assert agree(Decimal("1000000000"), Decimal("1000000000"), "USD")


# --- denominators ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "why"),
    [
        (Decimal(0), "nonpositive_denominator"),
        (Decimal(-5_000_000), "nonpositive_denominator"),
        (Decimal(999_999), "near_zero_denominator"),
        (Decimal(1_000_000), None),
    ],
)
def test_denominator_guard_never_clamps(value: Decimal, why: str | None) -> None:
    assert denominator_ok(value) == why
