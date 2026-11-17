"""Unit tests for the pure-Python fiscal-period reference (M01 section 3.5, rules 1-6).

Target: `pipelines/common/fiscal_ref.py` (`fye_date`, `y_end`, `label_offsets`, `quarter_index`,
`fiscal_period_label`, `calendar_frame`, `derive_fiscal`). No Spark. Expected values are
hand-derived and live next to the shared case data in `tests/fiscal_cases.py`.

Flag name pinned (in tests/fiscal_cases.py): 'label_offset_varies'. It is a proposal in the stub
docstring, not in the runbook; if Shekar renames it, change the strings in fiscal_cases.py.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import date
from typing import Any

import pytest

from pipelines.common.fiscal_ref import (
    LabelOffset,
    SubmissionPeriod,
    calendar_frame,
    derive_fiscal,
    fiscal_period_label,
    fye_date,
    label_offsets,
    quarter_index,
    y_end,
)
from tests.fiscal_cases import (
    A_K,
    APPLE,
    B_K,
    FISCAL_YEAR_SCENARIOS,
    K23,
    MV_K,
    MV_KT,
    NO_DERIVE_SCENARIOS,
    Q4_SCENARIOS,
    FiscalScenario,
    Sub,
    check_fiscal_scenario,
    check_no_derivation,
    to_rows,
)
from tests.fiscal_cases import submission as sub

# --------------------------------------------------------------------------------------
# Rule 1: fye_date
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "mmdd", "expected"),
    [
        (2023, "0930", date(2023, 9, 30)),
        (2022, "1231", date(2022, 12, 31)),
        (2023, "0131", date(2023, 1, 31)),
        (2024, "0630", date(2024, 6, 30)),
        (2023, "0228", date(2023, 2, 28)),
    ],
)
def test_fye_date_puts_mmdd_in_the_given_year(year: int, mmdd: str, expected: date) -> None:
    """Rule 1: fye_date(year, 'MMDD') is that month/day in calendar `year`."""
    assert fye_date(year, mmdd) == expected


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2023, date(2023, 2, 28)),  # non-leap year: Feb 29 does not exist
        (2024, date(2024, 2, 28)),  # leap year too: rule 1 says Feb 29 -> Feb 28 always
        (2025, date(2025, 2, 28)),
        (2100, date(2100, 2, 28)),  # 2100 is not a leap year
    ],
)
def test_fye_date_feb_29_becomes_feb_28_in_every_year(year: int, expected: date) -> None:
    """Rule 1: FYE '0229' maps to Feb 28 in leap and non-leap years alike (a stable anchor)."""
    assert fye_date(year, "0229") == expected


# --------------------------------------------------------------------------------------
# Rule 2: y_end
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period_end", "expected"),
    [
        # Apple-like FYE '0930', exact companyfacts dates (52/53-week years).
        (date(2021, 9, 25), 2021),  # 5 days before FYE: inside year 2021's window
        (date(2022, 9, 24), 2022),  # 6 days before FYE
        (date(2023, 9, 30), 2023),  # on FYE
        # FSDS rounds ddate to month end; the reference must handle both spellings.
        (date(2022, 9, 30), 2022),
        # Tolerance boundary: FYE + 7 days is inclusive, +8 belongs to the next year.
        (date(2022, 10, 7), 2022),
        (date(2022, 10, 8), 2023),
        (date(2023, 10, 7), 2023),
        (date(2023, 10, 8), 2024),
        # Quarter ends after the year end roll into the next fiscal year.
        (date(2022, 12, 31), 2023),
        (date(2023, 7, 1), 2023),
    ],
)
def test_y_end_apple_september_year_end(period_end: date, expected: int) -> None:
    """Rule 2: y_end is the smallest y with E <= fye_date(y) + 7 days (Sep 30 FYE)."""
    assert y_end(period_end, "0930") == expected


@pytest.mark.parametrize(
    ("period_end", "expected"),
    [
        (date(2022, 9, 30), 2023),  # Q1 of fiscal 2023 (FYE Jun 30)
        (date(2022, 12, 31), 2023),
        (date(2023, 3, 31), 2023),
        (date(2023, 6, 30), 2023),
        (date(2022, 7, 7), 2022),  # FYE 2022-06-30 + 7 days, inclusive
        (date(2022, 7, 8), 2023),
    ],
)
def test_y_end_june_year_end(period_end: date, expected: int) -> None:
    """Rule 2 for a Microsoft-like June FYE: Jul-Jun is one fiscal year named by its June."""
    assert y_end(period_end, "0630") == expected


@pytest.mark.parametrize(
    ("period_end", "expected"),
    [
        (date(2023, 12, 31), 2023),
        (date(2024, 1, 1), 2023),  # a 52/53-week year that ends the day after Dec 31
        (date(2024, 1, 7), 2023),  # Dec 31 + 7 days, inclusive
        (date(2024, 1, 8), 2024),
    ],
)
def test_y_end_december_year_end_with_52_53_week_tolerance(period_end: date, expected: int) -> None:
    """Rule 2: a Dec 31 FYE tolerates a year ending up to a week into January."""
    assert y_end(period_end, "1231") == expected


@pytest.mark.parametrize(
    ("period_end", "expected"),
    [
        (date(2023, 3, 7), 2023),  # 2023-02-28 + 7 days, inclusive
        (date(2023, 3, 8), 2024),
        # In a leap year the anchor is still Feb 28 (rule 1), so the window closes Mar 6, not Mar 7.
        (date(2024, 3, 6), 2024),
        (date(2024, 3, 7), 2025),
    ],
)
def test_y_end_feb_29_fye_uses_feb_28_anchor(period_end: date, expected: int) -> None:
    """Rules 1+2: with FYE '0229' the window end is Feb 28 + 7 days in every year."""
    assert y_end(period_end, "0229") == expected


# --------------------------------------------------------------------------------------
# Rule 3: label_offsets
# --------------------------------------------------------------------------------------


def sp(s: Sub) -> SubmissionPeriod:
    return SubmissionPeriod(s.cik, s.adsh, s.form, s.sub_fy, s.sub_period, s.fye)


def test_label_offset_is_zero_when_company_names_year_by_its_end_year() -> None:
    """Rule 3: Apple's sub.fy equals y_end of its balance-sheet date, so offset 0 and constant."""
    subs = [
        sub(APPLE, "10-K", 2021, date(2021, 9, 25), "0930"),
        sub(APPLE, "10-K", 2022, date(2022, 9, 24), "0930"),
        sub(APPLE, "10-K", 2023, date(2023, 9, 30), "0930"),
    ]
    assert label_offsets(subs) == {APPLE: LabelOffset(offset=0, is_constant=True)}


def test_label_offset_for_late_january_retailers_zero_and_minus_one() -> None:
    """Rule 3: retailer A names the year ending Jan 2023 'fiscal 2023' (offset 0); retailer B
    names it 'fiscal 2022' (offset -1). Both are constant, and ciks do not interact.
    """
    result = label_offsets([sp(s) for s in A_K + B_K])
    assert result == {
        A_K[0].cik: LabelOffset(offset=0, is_constant=True),
        B_K[0].cik: LabelOffset(offset=-1, is_constant=True),
    }


def test_label_offset_positive_when_company_names_year_by_the_following_year() -> None:
    """Rule 3: sub.fy one above y_end gives offset +1 (the sign is not forced non-positive)."""
    subs = [
        sub(7, "10-K", 2021, date(2020, 12, 31)),
        sub(7, "10-K", 2022, date(2021, 12, 31)),
    ]
    assert label_offsets(subs) == {7: LabelOffset(offset=1, is_constant=True)}


def test_label_offset_non_constant_company_takes_the_mode_and_is_flagged() -> None:
    """Rule 3: offsets {0,0,0,-1} -> mode 0, is_constant False (the company is flagged)."""
    subs = [
        sub(9, "10-K", 2019, date(2019, 12, 31)),
        sub(9, "10-K", 2020, date(2020, 12, 31)),
        sub(9, "10-K", 2021, date(2021, 12, 31)),
        sub(9, "10-K", 2021, date(2022, 12, 31)),
    ]
    assert label_offsets(subs) == {9: LabelOffset(offset=0, is_constant=False)}


def test_label_offset_mode_can_be_the_negative_value() -> None:
    """Rule 3: offsets {-1,-1,0} -> mode -1 (the mode, not min, max or the latest)."""
    subs = [
        sub(9, "10-K", 2019, date(2019, 12, 31)),
        sub(9, "10-K", 2019, date(2020, 12, 31)),
        sub(9, "10-K", 2020, date(2021, 12, 31)),
    ]
    assert label_offsets(subs) == {9: LabelOffset(offset=-1, is_constant=False)}


def test_label_offset_ignores_ten_q_submissions() -> None:
    """Rule 3: only 10-K/10-KT feed the mode; three 10-Qs with a weird sub.fy (offset -3)
    must not outvote two 10-Ks at offset 0.
    """
    subs = [
        sub(5, "10-K", 2021, date(2021, 12, 31)),
        sub(5, "10-K", 2022, date(2022, 12, 31)),
        sub(5, "10-Q", 2019, date(2022, 3, 31)),
        sub(5, "10-Q", 2019, date(2022, 6, 30)),
        sub(5, "10-Q", 2019, date(2022, 9, 30)),
    ]
    assert label_offsets(subs) == {5: LabelOffset(offset=0, is_constant=True)}


def test_label_offset_counts_ten_kt_transition_filings() -> None:
    """Rule 3 / stub: forms '10-K' and '10-KT' both feed the mode. A 10-K at offset 0 plus two
    10-KTs at offset -1 gives mode -1 (it would be 0 if 10-KT were dropped).
    """
    subs = [
        sub(6, "10-K", 2021, date(2021, 12, 31), "1231"),
        sub(6, "10-KT", 2021, date(2022, 6, 30), "0630"),  # y_end 2022, offset -1
        sub(6, "10-KT", 2022, date(2023, 6, 30), "0630"),  # y_end 2023, offset -1
    ]
    assert label_offsets(subs) == {6: LabelOffset(offset=-1, is_constant=False)}


def test_label_offset_does_not_count_ten_k_a_amendments() -> None:
    """Task spec / stub: ONLY '10-K' and '10-KT' feed rule 3. Two 10-K/As at offset -1 must not
    outvote one 10-K at offset 0. (The runbook says '10-K submissions'; see the report.)
    """
    subs = [
        sub(8, "10-K", 2021, date(2021, 12, 31)),
        sub(8, "10-K/A", 2020, date(2021, 12, 31)),
        sub(8, "10-K/A", 2020, date(2021, 12, 31)),
    ]
    assert label_offsets(subs) == {8: LabelOffset(offset=0, is_constant=True)}


def test_label_offset_uses_each_submissions_own_fye() -> None:
    """Rules 1+3: a company that moves FYE 0930 -> 0630 has offset 0 on both sides of the 10-KT.
    Using one fye for every submission would make one of them look off by one.
    """
    result = label_offsets([sp(MV_K), sp(MV_KT)])
    assert result == {MV_K.cik: LabelOffset(offset=0, is_constant=True)}


def test_label_offset_absent_for_ciks_without_a_usable_ten_k() -> None:
    """Stub: a cik with no usable 10-K submission is absent from the result. A cik with only
    10-Qs, or whose only 10-K has sub_fy None, has nothing to take a mode over.
    """
    subs = [
        sub(1, "10-Q", 2022, date(2022, 3, 31)),
        sub(2, "10-K", None, date(2022, 12, 31)),
        sub(3, "10-K", 2022, date(2022, 12, 31)),
        sub(3, "10-K", None, date(2021, 12, 31)),
    ]
    assert label_offsets(subs) == {3: LabelOffset(offset=0, is_constant=True)}


def test_label_offsets_of_nothing_is_empty() -> None:
    """No submissions, no offsets."""
    assert label_offsets([]) == {}


# --------------------------------------------------------------------------------------
# Rule 4: quarter_index and fiscal_period_label
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period_end", "fye", "expected"),
    [
        # June FYE: quarter ends 09-30, 12-31, 03-31, 06-30 are Q1..Q4.
        (date(2022, 9, 30), "0630", 1),
        (date(2022, 12, 31), "0630", 2),
        (date(2023, 3, 31), "0630", 3),
        (date(2023, 6, 30), "0630", 4),  # a period ending on the FYE is quarter 4
        # Apple 52/53-week quarter ends (nominal ends Dec 31, Mar 31, Jun 30, Sep 30).
        (date(2021, 12, 25), "0930", 1),  # 6 days early
        (date(2022, 3, 26), "0930", 2),  # 5 days early
        (date(2022, 6, 25), "0930", 3),  # 5 days early
        (date(2022, 9, 24), "0930", 4),  # 6 days early
        (date(2022, 12, 31), "0930", 1),
        (date(2023, 4, 1), "0930", 2),  # 1 day late
        (date(2023, 7, 1), "0930", 3),  # 1 day late
        (date(2023, 9, 30), "0930", 4),
        # Late-January retailer.
        (date(2022, 4, 30), "0131", 1),
        (date(2022, 7, 30), "0131", 2),
        (date(2022, 10, 29), "0131", 3),
        (date(2023, 1, 28), "0131", 4),
        # Calendar year, including a quarter that ends a day into the next month.
        (date(2023, 3, 31), "1231", 1),
        (date(2023, 4, 1), "1231", 1),
        (date(2023, 6, 30), "1231", 2),
        (date(2023, 9, 30), "1231", 3),
        (date(2023, 12, 30), "1231", 4),
        (date(2023, 12, 31), "1231", 4),
        (date(2024, 1, 1), "1231", 4),  # 52/53-week year ending Jan 1 is still quarter 4
    ],
)
def test_quarter_index_examples(period_end: date, fye: str, expected: int) -> None:
    """Rule 4: n = whole quarters from fiscal-year start to E, tolerant of 52/53-week drift;
    the FYE itself is quarter 4. Works for instants too (same function).
    """
    assert quarter_index(period_end, fye) == expected


@pytest.mark.parametrize(
    ("period_end", "qtrs", "fye", "expected"),
    [
        (date(2022, 9, 30), 1, "0630", "Q1"),
        (date(2022, 12, 31), 1, "0630", "Q2"),
        (date(2023, 3, 31), 1, "0630", "Q3"),
        (date(2023, 6, 30), 1, "0630", "Q4"),
        (date(2022, 12, 31), 2, "0630", "H1"),
        (date(2023, 3, 31), 3, "0630", "9M"),
        (date(2023, 6, 30), 4, "0630", "FY"),
        # Instants (qtrs 0) get a real label: FY at the year end, Q1-Q3 at the quarter ends.
        (date(2023, 6, 30), 0, "0630", "FY"),
        (date(2022, 9, 30), 0, "0630", "Q1"),
        (date(2022, 12, 31), 0, "0630", "Q2"),
        (date(2023, 3, 31), 0, "0630", "Q3"),
        (date(2022, 9, 24), 0, "0930", "FY"),  # a 52/53-week year end is still quarter 4
        (date(2023, 7, 1), 0, "0930", "Q3"),
        (date(2022, 9, 24), 4, "0930", "FY"),  # qtrs 4 is FY whatever the date drift
        (date(2024, 1, 1), 4, "1231", "FY"),
        (date(2023, 7, 1), 1, "0930", "Q3"),
    ],
)
def test_fiscal_period_label_examples(period_end: date, qtrs: int, fye: str, expected: str) -> None:
    """Rule 4: qtrs 4 -> FY, 1 -> Q{n}, 2 -> H1, 3 -> 9M; an instant (qtrs 0) is FY when its
    quarter index is 4 and Q{n} otherwise. 'INSTANT' is never a label (decision 2026-11-17).
    """
    assert fiscal_period_label(period_end, qtrs, fye) == expected


# --------------------------------------------------------------------------------------
# Rule 6: calendar_frame
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period_end", "qtrs", "expected"),
    [
        # Annual frames: within +-30 days of Dec 31, year of the NEAREST Dec 31.
        (date(2023, 12, 31), 4, "CY2023"),
        (date(2022, 12, 31), 4, "CY2022"),
        (date(2023, 1, 1), 4, "CY2022"),  # a 52/53-week year ending Jan 1: nearest Dec 31 is 2022
        (date(2024, 1, 1), 4, "CY2023"),
        (date(2023, 12, 30), 4, "CY2023"),
        (date(2024, 1, 29), 4, "CY2023"),  # 29 days after Dec 31
        (date(2023, 12, 2), 4, "CY2023"),  # 29 days before Dec 31
        (date(2024, 1, 31), 4, None),  # 31 days after: outside the window
        (date(2023, 11, 30), 4, None),  # 31 days before: outside the window
        (date(2023, 9, 30), 4, None),  # Apple: not aligned to the calendar year
        (date(2023, 6, 30), 4, None),  # Microsoft: not aligned either
        # Quarters: calendar quarter of the period end.
        (date(2022, 9, 30), 1, "CY2022Q3"),
        (date(2023, 1, 28), 1, "CY2023Q1"),
        (date(2022, 3, 31), 1, "CY2022Q1"),
        (date(2022, 4, 1), 1, "CY2022Q2"),
        (date(2022, 6, 30), 1, "CY2022Q2"),
        (date(2022, 7, 1), 1, "CY2022Q3"),
        (date(2022, 12, 31), 1, "CY2022Q4"),
        # Instants: the same with a trailing I.
        (date(2022, 9, 30), 0, "CY2022Q3I"),
        (date(2023, 12, 31), 0, "CY2023Q4I"),
        (date(2022, 3, 31), 0, "CY2022Q1I"),
        # H1 and 9M never get a frame.
        (date(2022, 12, 31), 2, None),
        (date(2022, 6, 30), 2, None),
        (date(2022, 12, 31), 3, None),
        (date(2022, 9, 30), 3, None),
    ],
)
def test_calendar_frame_examples(period_end: date, qtrs: int, expected: str | None) -> None:
    """Rule 6: CY{yyyy} / CY{yyyy}Q{n} / CY{yyyy}Q{n}I, and None where no frame applies."""
    assert calendar_frame(period_end, qtrs) == expected


# --------------------------------------------------------------------------------------
# Rules 1-6 over a set of facts: derive_fiscal
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", FISCAL_YEAR_SCENARIOS, ids=repr)
def test_derive_fiscal_year_period_and_frame(scenario: FiscalScenario) -> None:
    """Rules 1-4 and 6 end to end: fiscal_year (y_end + label offset), fiscal_period, calendar
    frame and dq flags are as hand-derived, input rows pass through unchanged, and nothing is
    derived (no scenario here has a FY/9M pair of the same series).
    """
    out = derive_fiscal(list(scenario.facts))
    check_fiscal_scenario(scenario, to_rows(out))


@pytest.mark.parametrize("scenario", Q4_SCENARIOS, ids=repr)
def test_derive_fiscal_derives_q4_as_fy_minus_9m(scenario: FiscalScenario) -> None:
    """Rule 5: one derived row per provenance tier: qtrs 1, 'Q4', period_end = FY end, the FY
    parent's form, accession and fiscal_year, value FY - 9M, flag q4_derived, derived_from naming
    the two retained parents. A restated FY gives one Q4 per tier (original and latest), repeated
    equal comparatives give one Q4 from the latest accessions, and a tie between 9M candidates
    goes to the latest filed. The FY and 9M rows survive unchanged (n_out = n_in + n_derived).
    """
    out = derive_fiscal(list(scenario.facts))
    check_fiscal_scenario(scenario, to_rows(out))


@pytest.mark.parametrize("scenario", NO_DERIVE_SCENARIOS, ids=repr)
def test_derive_fiscal_does_not_derive_q4_when_it_would_be_wrong(scenario: FiscalScenario) -> None:
    """Rule 5 negatives: no Q4 for per-share ('/'), 'pure' or 'shares' uoms, instants, a FY
    without a 9M or a 9M without a FY, a mismatch of uom/tag/taxonomy/cik or a 9M from the
    wrong year; and a directly reported Q4 in the same accession wins (kept as is, nothing
    derived).
    """
    out = derive_fiscal(list(scenario.facts))
    rows = to_rows(out)
    check_no_derivation(rows, len(scenario.facts))
    check_fiscal_scenario(scenario, rows)


@pytest.mark.parametrize(
    ("period_end", "qtrs", "expected"),
    [
        (date(2023, 9, 30), 4, date(2022, 10, 1)),  # Sep 30 minus 12 months = 2022-09-30, +1 day
        (date(2023, 6, 30), 1, date(2023, 4, 1)),  # Jun 30 minus 3 months = Mar 31, +1 day
        (date(2022, 12, 31), 2, date(2022, 7, 1)),  # Dec 31 minus 6 months = Jun 30, +1 day
        (date(2023, 7, 1), 3, date(2022, 10, 2)),  # Jul 1 minus 9 months = Oct 1, +1 day
        (date(2023, 9, 30), 0, None),  # instants have no start
    ],
)
def test_derive_fiscal_period_start_is_period_end_minus_qtrs_quarters_plus_a_day(
    period_end: date, qtrs: int, expected: date | None
) -> None:
    """Rule (docstring): period_start ~ period_end - qtrs quarters + 1 day; None for instants.
    Dates were picked so month-end clamping gives one answer.
    """
    fact = K23.fact("OneOffTag", period_end, qtrs)
    (out,) = derive_fiscal([fact])
    assert out.period_start == expected


def test_derive_fiscal_has_no_dq_flags_for_an_ordinary_fact() -> None:
    """dq_flags is an empty tuple, not None, when nothing is flagged."""
    (out,) = derive_fiscal([K23.fact("Revenues", date(2023, 9, 30), 4)])
    assert out.dq_flags == ()
    assert out.derived_from is None


def _frozen(rows: list[dict[str, Any]]) -> Counter[tuple[Any, ...]]:
    return Counter(tuple(sorted(r.items())) for r in rows)


@pytest.mark.parametrize("scenario", [Q4_SCENARIOS[2], Q4_SCENARIOS[3]], ids=repr)
def test_derive_fiscal_ignores_input_order(scenario: FiscalScenario) -> None:
    """Docstring: input order does not matter. Reversed and shuffled input give the same rows."""
    facts = list(scenario.facts)
    expected = _frozen(to_rows(derive_fiscal(facts)))
    assert _frozen(to_rows(derive_fiscal(facts[::-1]))) == expected
    shuffled = facts[:]
    random.Random(7).shuffle(shuffled)
    assert _frozen(to_rows(derive_fiscal(shuffled))) == expected
