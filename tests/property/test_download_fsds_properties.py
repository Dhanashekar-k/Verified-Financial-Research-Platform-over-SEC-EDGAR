"""Property tests (Hypothesis) for the FSDS quarter arithmetic (`pipelines/fetch/download_fsds.py`).

The example-based tests live in `tests/unit/test_download_fsds.py`.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given
from hypothesis import strategies as st

from pipelines.fetch.download_fsds import FIRST_QUARTER, Quarter, current_quarter, quarter_range


@given(year=st.integers(2009, 2100), q=st.integers(1, 4))
def test_parse_str_round_trip_property(year: int, q: int):
    """parse(str(x)) == x for every valid quarter, in either case."""
    quarter = Quarter(year, q)
    assert Quarter.parse(str(quarter)) == quarter
    assert Quarter.parse(str(quarter).upper()) == quarter


@given(d=st.dates(min_value=date(2009, 1, 1), max_value=date(2100, 12, 31)))
def test_current_quarter_matches_month_arithmetic_property(d: date):
    """For every date, current_quarter is (year, ceil(month / 3))."""
    assert current_quarter(d) == Quarter(d.year, (d.month - 1) // 3 + 1)


@given(
    start_year=st.integers(2009, 2040),
    start_q=st.integers(1, 4),
    length=st.integers(0, 40),
)
def test_quarter_range_is_ascending_consecutive_and_inclusive_property(
    start_year: int, start_q: int, length: int
):
    """For any valid start and end, the range is exactly every quarter between them."""
    start = Quarter(start_year, start_q)
    if start < FIRST_QUARTER:
        start = FIRST_QUARTER
    s_idx = start.year * 4 + start.q - 1
    e_idx = s_idx + length
    end = Quarter(e_idx // 4, e_idx % 4 + 1)
    assert quarter_range(start, end) == [
        Quarter(i // 4, i % 4 + 1) for i in range(s_idx, e_idx + 1)
    ]
