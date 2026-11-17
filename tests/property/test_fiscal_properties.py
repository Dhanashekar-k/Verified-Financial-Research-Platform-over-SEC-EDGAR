"""Property tests (Hypothesis) for the pure-Python fiscal reference (`fiscal_ref.py`).

The example-based tests and the hand-derived scenarios live in `tests/unit/test_fiscal.py`.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from pipelines.common.fiscal_ref import (
    LabelOffset,
    calendar_frame,
    derive_fiscal,
    fiscal_period_label,
    fye_date,
    label_offsets,
    quarter_index,
    y_end,
)
from tests.fiscal_cases import K23, Q3_23, Q4_SCENARIOS, check_fiscal_scenario, to_rows
from tests.fiscal_cases import submission as sub

_DAYS_IN_MONTH = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}


ANY_FYE = st.sampled_from(
    [f"{m:02d}{d:02d}" for m, n in _DAYS_IN_MONTH.items() for d in range(1, n + 1)]
)


MONTH_END_FYE = st.sampled_from([f"{m:02d}{n:02d}" for m, n in _DAYS_IN_MONTH.items()])


FYE_STRATEGY = st.one_of(ANY_FYE, MONTH_END_FYE)  # month ends are the common real-world case


ANY_DATE = st.dates(min_value=date(1990, 1, 1), max_value=date(2100, 12, 31))


@given(year=st.integers(1990, 2100), mmdd=FYE_STRATEGY)
def test_fye_date_is_in_year_and_matches_mmdd_property(year: int, mmdd: str) -> None:
    """For every valid FYE, the result lies in `year` and keeps month/day (0229 -> Feb 28)."""
    result = fye_date(year, mmdd)
    expected_mmdd = "0228" if mmdd == "0229" else mmdd
    assert result.year == year
    assert (result.month, result.day) == (int(expected_mmdd[:2]), int(expected_mmdd[2:]))


@settings(max_examples=300)
@given(period_end=ANY_DATE, mmdd=FYE_STRATEGY)
def test_y_end_is_the_unique_year_whose_window_contains_the_date(
    period_end: date, mmdd: str
) -> None:
    """Property (a): exactly one year y has fye_date(y-1)+7d < E <= fye_date(y)+7d, and y_end
    returns it. Neighbouring years must fail the window test, so no date is double-assigned.
    """
    week = timedelta(days=7)

    def in_window(y: int) -> bool:
        return fye_date(y - 1, mmdd) + week < period_end <= fye_date(y, mmdd) + week

    y = y_end(period_end, mmdd)
    assert in_window(y)
    for other in (y - 3, y - 2, y - 1, y + 1, y + 2, y + 3):
        assert not in_window(other), f"{period_end} also fits year {other} for FYE {mmdd}"


@given(offset=st.integers(-3, 3), n=st.integers(1, 6), cik=st.integers(1, 10**7))
def test_label_offset_constant_property(offset: int, n: int, cik: int) -> None:
    """Property: n 10-Ks at Dec 31 whose sub.fy is the calendar year + o yield exactly (o, True)."""
    subs = [sub(cik, "10-K", 2010 + i + offset, date(2010 + i, 12, 31)) for i in range(n)]
    assert label_offsets(subs) == {cik: LabelOffset(offset=offset, is_constant=True)}


@given(
    data=st.data(),
    major=st.integers(-2, 2),
    minor=st.integers(-2, 2),
    n_major=st.integers(2, 6),
)
def test_label_offset_strict_majority_wins_in_any_order_property(
    data: st.DataObject, major: int, minor: int, n_major: int
) -> None:
    """Property: with a strict majority offset the mode is that offset, whatever the input order,
    and is_constant is False because a second distinct offset exists.
    """
    if major == minor:
        minor += 1
    n_minor = data.draw(st.integers(1, n_major - 1))
    subs = [sub(4, "10-K", 2000 + i + major, date(2000 + i, 12, 31)) for i in range(n_major)]
    subs += [sub(4, "10-K", 2100 + i + minor, date(2100 + i, 12, 31)) for i in range(n_minor)]
    shuffled = data.draw(st.permutations(subs))
    assert label_offsets(shuffled) == {4: LabelOffset(offset=major, is_constant=False)}


@settings(max_examples=300)
@given(
    fye=st.sampled_from(
        [
            "0131",
            "0228",
            "0229",
            "0331",
            "0430",
            "0531",
            "0630",
            "0731",
            "0831",
            "0930",
            "1031",
            "1130",
            "1231",
        ]
    ),  # fmt: skip
    year=st.integers(1995, 2090),
    k=st.integers(1, 4),
    drift=st.integers(-3, 3),
)
def test_quarter_index_at_calendar_month_end_quarters_property(
    fye: str, year: int, k: int, drift: int
) -> None:
    """Property: for a month-end FYE, the end of fiscal quarter k of fiscal `year` (last day of
    month fye_month - 12 + 3k) plus or minus up to 3 days of 52/53-week drift has y_end == year
    and quarter_index == k.
    """
    fye_month = int(fye[:2])
    total = (year - 1) * 12 + (fye_month - 1) + 3 * k  # zero-based month index of quarter end
    q_year, q_month = divmod(total, 12)
    q_end = date(q_year, q_month + 1, calendar.monthrange(q_year, q_month + 1)[1])
    e = q_end + timedelta(days=drift)
    assert y_end(e, fye) == year
    assert quarter_index(e, fye) == k


@settings(max_examples=300)
@given(period_end=ANY_DATE, fye=FYE_STRATEGY)
def test_quarter_index_is_always_between_1_and_4_property(period_end: date, fye: str) -> None:
    """Property (b): for any date and FYE the quarter index is in {1,2,3,4} (never 0 or 5, even
    on the first days of a fiscal year or just past a year end).
    """
    assert quarter_index(period_end, fye) in {1, 2, 3, 4}


@given(period_end=ANY_DATE, fye=FYE_STRATEGY)
def test_fiscal_period_label_quarter_matches_quarter_index_property(
    period_end: date, fye: str
) -> None:
    """Property: qtrs 1 -> 'Q' + quarter_index; qtrs 2, 3, 4 ignore the date; qtrs 0 is FY or Qn."""
    assert fiscal_period_label(period_end, 1, fye) == f"Q{quarter_index(period_end, fye)}"
    assert fiscal_period_label(period_end, 2, fye) == "H1"
    assert fiscal_period_label(period_end, 3, fye) == "9M"
    assert fiscal_period_label(period_end, 4, fye) == "FY"
    # An instant is FY at the year end (quarter index 4) and Q1-Q3 otherwise; never 'INSTANT'.
    quarter = quarter_index(period_end, fye)
    assert fiscal_period_label(period_end, 0, fye) == ("FY" if quarter == 4 else f"Q{quarter}")


@given(period_end=ANY_DATE)
def test_calendar_frame_quarter_and_instant_follow_the_calendar_quarter_property(
    period_end: date,
) -> None:
    """Property: qtrs 1 -> CY{year}Q{calendar quarter}; qtrs 0 -> the same plus 'I'; 2/3 -> None."""
    quarter = f"CY{period_end.year}Q{(period_end.month - 1) // 3 + 1}"
    assert calendar_frame(period_end, 1) == quarter
    assert calendar_frame(period_end, 0) == quarter + "I"
    assert calendar_frame(period_end, 2) is None
    assert calendar_frame(period_end, 3) is None


@given(order=st.permutations(list(Q4_SCENARIOS[2].facts)))
def test_derive_fiscal_order_independence_property(order: list[Any]) -> None:
    """Property: any permutation of the two-year Apple facts yields the same two derived rows."""
    out = derive_fiscal(list(order))
    check_fiscal_scenario(Q4_SCENARIOS[2], to_rows(out))


@settings(max_examples=200)
@given(
    fy=st.integers(-(10**24), 10**24),
    nine_m=st.integers(-(10**24), 10**24),
)
def test_derived_q4_plus_9m_equals_fy_property(fy: int, nine_m: int) -> None:
    """Property (c): for random 4-decimal flow values (negative allowed: losses, and a Q4 below
    zero when 9M exceeds FY), derived Q4 + 9M == FY exactly: no rounding, no float.
    """
    fy_value = Decimal(fy).scaleb(-4)
    nine_value = Decimal(nine_m).scaleb(-4)
    facts = [
        K23.fact("NetIncomeLoss", date(2023, 9, 30), 4, fy_value),
        Q3_23.fact("NetIncomeLoss", date(2023, 7, 1), 3, nine_value),
    ]
    out = derive_fiscal(facts)
    derived = [o for o in out if o.derived_from is not None]
    assert len(out) == 3
    assert len(derived) == 1
    q4 = derived[0].fact.value
    assert q4 is not None
    assert q4 + nine_value == fy_value
    assert derived[0].fact.qtrs == 1
    assert derived[0].fiscal_period == "Q4"


@settings(max_examples=100)
@given(
    fy=st.integers(1, 10**15),
    nine_m=st.integers(1, 10**15),
    jitter=st.integers(-1, 0),
)
def test_derived_q4_pairs_9m_ending_one_quarter_before_fy_property(
    fy: int, nine_m: int, jitter: int
) -> None:
    """Property: a 9M ending 91 or 92 days before the FY (exact vs FSDS-rounded dates) always
    pairs, so the exact and rounded spellings produce the same derived value.
    """
    nine_end = date(2023, 7, 1) + timedelta(days=jitter)  # 07-01 (91 days) or 06-30 (92 days)
    facts = [
        K23.fact("Revenues", date(2023, 9, 30), 4, Decimal(fy)),
        Q3_23.fact("Revenues", nine_end, 3, Decimal(nine_m)),
    ]
    derived = [o for o in derive_fiscal(facts) if o.derived_from is not None]
    assert len(derived) == 1
    assert derived[0].fact.value == Decimal(fy - nine_m)
    assert derived[0].derived_from == ((date(2023, 9, 30), 4, K23.adsh), (nine_end, 3, Q3_23.adsh))
