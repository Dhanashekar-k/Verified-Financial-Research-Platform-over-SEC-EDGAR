"""Period normalization (M05 section 3.2): free text -> PeriodRef, anchored on what was FILED.

Written test-first against the stub in ``app/engine/plan/periods.py`` (Shekar implements it). Every
assertion is on ``PeriodResolution`` fields (``periods``, ``clarification``, ``assumed``), so any
implementation strategy passes. Rules pinned here:

* explicit periods parse case/whitespace-insensitively; a bare "2023" is FY2023;
* relative periods ("last year", "most recent quarter") resolve against ``LatestFiled``, never
  against today's date, and never from a calendar date;
* no period (None / blank) means the latest FY with ``assumed=True`` so the answer says so;
* anything unsupported or ambiguous returns ``clarification`` and NO periods (never guess).

Open decisions the spec left to the implementer are marked ``DECISION`` in the docstrings.
"""

from __future__ import annotations

import datetime as real_datetime
import time as real_time
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.engine.plan import periods as periods_module
from app.engine.plan.periods import LatestFiled, PeriodResolution, resolve_period
from app.engine.plan.schema import PeriodRef

MIN_YEAR = 1993  # first year EDGAR full-text/XBRL-era filings exist: lower bound of valid years


def fy(year: int) -> PeriodRef:
    return PeriodRef(fiscal_year=year, fiscal_period="FY")


def q(year: int, n: int) -> PeriodRef:
    return PeriodRef(fiscal_year=year, fiscal_period=f"Q{n}")  # type: ignore[arg-type]


def filed_fy(year: int) -> LatestFiled:
    """Company whose most recent filing is the FY ``year`` 10-K (no newer quarter)."""
    return LatestFiled(fiscal_year=year, latest=fy(year))


def filed_quarter(fy_year: int, quarter_year: int, n: int) -> LatestFiled:
    """Latest FY is ``fy_year`` but a newer 10-Q (``quarter_year`` Q``n``) has been filed since."""
    return LatestFiled(fiscal_year=fy_year, latest=q(quarter_year, n))


LATEST_FY = filed_fy(2024)  # latest filing is the FY2024 10-K
LATEST_Q = filed_quarter(2024, 2025, 2)  # FY2024 10-K, then the Q2 FY2025 10-Q
WIDE = filed_fy(2026)  # wide valid range 1993..2026 for the property tests


def assert_resolved(
    res: PeriodResolution, expected: list[PeriodRef], assumed: bool = False
) -> None:
    assert isinstance(res, PeriodResolution)
    assert res.clarification is None
    assert res.periods == expected
    assert res.assumed is assumed


def assert_clarifies(res: PeriodResolution) -> None:
    """Unsupported/ambiguous: a non-blank clarification, no periods, and not 'assumed'."""
    assert isinstance(res, PeriodResolution)
    assert isinstance(res.clarification, str)
    assert res.clarification.strip() != ""
    assert res.periods == []
    assert res.assumed is False


# --- explicit fiscal years ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "FY2023",
        "fy2023",
        "FY 2023",
        "fiscal 2023",
        "Fiscal 2023",
        "fiscal year 2023",
        "Fiscal Year 2023",
        "2023",
        "  2023  ",
        "  fy2023\t",
        "FISCAL   YEAR   2023",
        "fiscal\tyear\n2023",
    ],
)
def test_fiscal_year_forms_resolve_to_fy(text: str) -> None:
    """Every spelling of FY2023 (case, inner/outer whitespace) gives exactly FY2023, not assumed.

    A bare "2023" means the fiscal year 2023 (spec), not a calendar year."""
    assert_resolved(resolve_period(text, LATEST_FY), [fy(2023)])


# --- explicit quarters -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Q3 2022", q(2022, 3)),
        ("q3 2022", q(2022, 3)),
        ("Q3 FY2022", q(2022, 3)),
        ("q3 fy2022", q(2022, 3)),
        ("Q3 FY 2022", q(2022, 3)),
        ("  Q3   2022  ", q(2022, 3)),
        ("\tQ3\t2022\n", q(2022, 3)),
        ("third quarter 2022", q(2022, 3)),
        ("Third Quarter 2022", q(2022, 3)),
        ("THIRD   QUARTER   2022", q(2022, 3)),
        ("first quarter 2022", q(2022, 1)),
        ("second quarter 2022", q(2022, 2)),
        ("fourth quarter 2022", q(2022, 4)),
        ("Q1 2022", q(2022, 1)),
        ("Q2 2022", q(2022, 2)),
        ("Q4 2023", q(2023, 4)),
    ],
)
def test_quarter_forms_resolve_to_quarter(text: str, expected: PeriodRef) -> None:
    """Quarter spellings (Q3 2022, Q3 FY2022, 'third quarter 2022') map to the same PeriodRef.

    Q4 is allowed as a quarter (spec): it is not rewritten to FY."""
    assert_resolved(resolve_period(text, LATEST_FY), [expected])


def test_q4_is_a_quarter_not_fy() -> None:
    """'Q4 2023' returns fiscal_period 'Q4', never 'FY' (Q4 derivation is the compiler's job)."""
    res = resolve_period("Q4 2023", LATEST_FY)
    assert_resolved(res, [q(2023, 4)])
    assert res.periods[0].fiscal_period == "Q4"


def test_quarter_in_year_of_newer_filed_quarter_is_valid() -> None:
    """DECISION: the upper year bound is the newest filed year of ANY kind, so with FY2024 as the
    latest 10-K and a Q2 FY2025 10-Q on file, 'Q2 2025' resolves (the user is asking about a
    filed period), while the explicit-year-bound tests below still reject years past the filings."""
    assert_resolved(resolve_period("Q2 2025", LATEST_Q), [q(2025, 2)])
    assert_resolved(resolve_period("Q1 2025", LATEST_Q), [q(2025, 1)])


# --- no period given ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [None, "", " ", "   ", "\t", "\n", " \t\n "])
def test_no_period_uses_latest_fy_and_flags_assumed(text: str | None) -> None:
    """None/empty/whitespace -> latest FY with assumed=True, so the answer must say it assumed."""
    assert_resolved(resolve_period(text, LATEST_FY), [fy(2024)], assumed=True)


def test_no_period_uses_latest_fy_not_latest_quarter() -> None:
    """With a newer quarter filed, the default is still the latest FULL fiscal year (FY2024)."""
    assert_resolved(resolve_period(None, LATEST_Q), [fy(2024)], assumed=True)


@pytest.mark.parametrize("text", ["FY2023", "2023", "Q3 2022", "last year", "most recent quarter"])
def test_given_period_is_never_flagged_assumed(text: str) -> None:
    """assumed is True only when the user gave no period; relative phrases are still 'given'."""
    res = resolve_period(text, LATEST_Q)
    assert res.clarification is None
    assert res.assumed is False


# --- relative periods anchor on the latest FILED period ----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "last year",
        "Last Year",
        "  last   year ",
        "latest year",
        "latest fiscal year",
        "most recent fiscal year",
        "Most Recent Fiscal Year",
    ],
)
def test_last_year_is_the_latest_filed_fy_not_minus_one(text: str) -> None:
    """'last year' / 'latest year' / 'most recent fiscal year' -> latest.fiscal_year itself (2024).

    Anchored on filings: it is NOT latest-1 and NOT derived from today's date (2026-11)."""
    assert_resolved(resolve_period(text, LATEST_FY), [fy(2024)])


def test_last_year_ignores_a_newer_quarter() -> None:
    """With FY2024 10-K plus a Q2 FY2025 10-Q on file, 'last year' is FY2024 (latest full FY)."""
    assert_resolved(resolve_period("last year", LATEST_Q), [fy(2024)])


@pytest.mark.parametrize(
    "text", ["prior year", "Prior Year", "the year before", "  the   year before "]
)
def test_prior_year_is_latest_fy_minus_one(text: str) -> None:
    """'prior year' / 'the year before' -> latest FY minus 1."""
    assert_resolved(resolve_period(text, LATEST_FY), [fy(2023)])
    assert_resolved(resolve_period(text, LATEST_Q), [fy(2023)])


def test_prior_year_before_first_valid_year_clarifies() -> None:
    """latest FY 1993 -> prior year is 1992, outside 1993..latest: clarify, do not return 1992."""
    assert_clarifies(resolve_period("prior year", filed_fy(1993)))


@pytest.mark.parametrize(
    "text",
    ["most recent quarter", "Most Recent Quarter", "latest quarter", "  most  recent   quarter "],
)
def test_most_recent_quarter_is_latest_filed_quarter(text: str) -> None:
    """'most recent quarter' -> LatestFiled.latest_quarter (Q2 FY2025), even though its fiscal
    year differs from latest.fiscal_year (2024)."""
    assert_resolved(resolve_period(text, LATEST_Q), [q(2025, 2)])


@pytest.mark.parametrize("n", [1, 2, 3])
def test_most_recent_quarter_follows_whichever_quarter_is_latest(n: int) -> None:
    """The quarter returned is exactly latest.latest for Q1, Q2 and Q3 (no hard-coded quarter)."""
    assert_resolved(
        resolve_period("most recent quarter", filed_quarter(2020, 2021, n)), [q(2021, n)]
    )


def test_most_recent_quarter_when_latest_filed_is_fy_clarifies() -> None:
    """DECISION: if the latest filed period is an FY there is no quarter newer than it, so
    'most recent quarter' asks for clarification instead of silently returning Q4 or the FY."""
    assert_clarifies(resolve_period("most recent quarter", LATEST_FY))


# --- unsupported and ambiguous input -> clarification, no periods ------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "trailing",
        "trailing twelve months",
        "Trailing 12 months",
        "TTM",
        "ttm",
        "LTM",
        "last twelve months",
        "year to date",
        "YTD",
    ],
)
def test_trailing_and_ttm_forms_clarify(text: str) -> None:
    """Trailing-window forms are unsupported (spec): clarification, no periods, not assumed."""
    assert_clarifies(resolve_period(text, LATEST_FY))


@pytest.mark.parametrize(
    "text", ["last 3 years", "past 5 years", "last three years", "last 4 quarters"]
)
def test_multi_period_windows_clarify(text: str) -> None:
    """DECISION: multi-period windows ('last 3 years') are not supported by this resolver (the
    planner emits explicit periods for trends), so they clarify rather than expand."""
    assert_clarifies(resolve_period(text, LATEST_FY))


@pytest.mark.parametrize("text", ["2022-2023", "2022 and 2023", "FY2022 FY2023", "2022 or 2023"])
def test_period_lists_and_ranges_clarify(text: str) -> None:
    """DECISION: exactly one period per call; ranges/lists are ambiguous and clarify."""
    assert_clarifies(resolve_period(text, LATEST_FY))


@pytest.mark.parametrize(
    "text",
    [
        "gibberish",
        "asdf qwer",
        "???",
        "FY",
        "fiscal year",
        "Q3",
        "third quarter",
        "quarter",
        "year",
        "next year",
        "H1 2023",
        "half year 2023",
        "FY23",
        "fy 23",
        "23",
        "'23",
        "FY2023x",
        "2023.5",
        "-2023",
        "FY20233",
        "Q 2023",
        "fiscal quarter 2023",
    ],
)
def test_garbage_and_incomplete_input_clarifies(text: str) -> None:
    """Gibberish and incomplete forms clarify. A quarter with no year ('Q3') is not completed from
    the filings, and two-digit years ('FY23') are ambiguous, so neither is guessed."""
    assert_clarifies(resolve_period(text, LATEST_FY))


@pytest.mark.parametrize(
    "text",
    [
        "Q5 2022",
        "Q0 2022",
        "Q9 FY2022",
        "fifth quarter 2022",
        "zeroth quarter 2022",
        "Q10 2022",
        "Q-1 2022",
    ],
)
def test_invalid_quarter_number_clarifies(text: str) -> None:
    """Only quarters 1..4 exist; Q5/Q0/Q10 clarify instead of rolling into the next year."""
    assert_clarifies(resolve_period(text, LATEST_FY))


# --- year range: 1993..latest -------------------------------------------------------------------


def test_first_valid_year_boundary_inclusive() -> None:
    """1993 is the lowest accepted year (inclusive) for every form."""
    assert_resolved(resolve_period("FY1993", LATEST_FY), [fy(1993)])
    assert_resolved(resolve_period("1993", LATEST_FY), [fy(1993)])
    assert_resolved(resolve_period("Q1 1993", LATEST_FY), [q(1993, 1)])


@pytest.mark.parametrize("text", ["FY1992", "1992", "Q4 1992", "fiscal 1900", "1000", "FY0000"])
def test_year_below_range_clarifies(text: str) -> None:
    """Years before 1993 predate EDGAR: clarify."""
    assert_clarifies(resolve_period(text, LATEST_FY))


def test_latest_year_boundary_inclusive() -> None:
    """latest+0 is accepted (FY2024 when the FY2024 10-K is the latest filing)."""
    assert_resolved(resolve_period("FY2024", LATEST_FY), [fy(2024)])
    assert_resolved(resolve_period("2024", LATEST_FY), [fy(2024)])
    assert_resolved(resolve_period("Q3 2024", LATEST_FY), [q(2024, 3)])


@pytest.mark.parametrize("text", ["FY2025", "2025", "Q1 2025", "fiscal 2099", "9999", "FY2026"])
def test_year_after_latest_filed_clarifies(text: str) -> None:
    """A year past the latest filed year (+1 and beyond) is not answerable: clarify, don't clamp.

    Today's date (2026-11) is irrelevant: FY2026 is rejected for a company last filed in FY2024."""
    assert_clarifies(resolve_period(text, LATEST_FY))


def test_year_range_follows_the_company_not_a_global_constant() -> None:
    """The upper bound moves with LatestFiled: 2030 is fine for a company filed through FY2030
    and 2011 is rejected for one filed through FY2010."""
    assert_resolved(resolve_period("FY2030", filed_fy(2030)), [fy(2030)])
    assert_clarifies(resolve_period("FY2011", filed_fy(2010)))


# --- never derived from today's date or a calendar date ----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "December 31, 2023",
        "2023-12-31",
        "2023-09-30",
        "12/31/2023",
        "Sep 2023",
        "September 2023",
        "as of 2023-12-31",
        "the 12 months ended June 30, 2023",
        "CY2023",
        "calendar 2023",
        "calendar year 2023",
        "today",
        "this year",
        "this quarter",
        "current year",
    ],
)
def test_fiscal_year_is_never_inferred_from_a_calendar_date(text: str) -> None:
    """Calendar dates/years, 'today', 'this year' and period-end dates are NOT fiscal periods: the
    fiscal year of a date depends on the company's fiscal calendar: ask, don't guess."""
    assert_clarifies(resolve_period(text, LATEST_FY))


# --- determinism and clock independence --------------------------------------------------------

SAMPLE_INPUTS: list[str | None] = [
    None,
    "",
    "FY2023",
    "2023",
    "fiscal year 2022",
    "Q3 2022",
    "third quarter 2022",
    "last year",
    "prior year",
    "most recent quarter",
    "most recent fiscal year",
    "trailing twelve months",
    "gibberish",
    "Q5 2022",
    "FY2099",
    "December 31, 2023",
]


@pytest.mark.parametrize("text", SAMPLE_INPUTS)
@pytest.mark.parametrize("latest", [LATEST_FY, LATEST_Q], ids=["latest_fy", "latest_q"])
def test_resolution_is_deterministic(text: str | None, latest: LatestFiled) -> None:
    """Same input -> equal output across repeated calls and equal-valued LatestFiled copies."""
    first = resolve_period(text, latest)
    assert resolve_period(text, latest) == first
    assert resolve_period(text, latest.model_copy(deep=True)) == first
    assert resolve_period(text, LatestFiled(**latest.model_dump())) == first


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, frozen: real_datetime.datetime) -> None:
    """Pin every common clock source to ``frozen`` (module-level and ``datetime.*`` lookups)."""

    class FrozenDatetime(real_datetime.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> FrozenDatetime:  # type: ignore[override]
            return cls.fromtimestamp(frozen.timestamp(), tz)

        @classmethod
        def utcnow(cls) -> FrozenDatetime:  # type: ignore[override]
            return cls.utcfromtimestamp(frozen.timestamp())

        @classmethod
        def today(cls) -> FrozenDatetime:
            return cls.fromtimestamp(frozen.timestamp())

    class FrozenDate(real_datetime.date):
        @classmethod
        def today(cls) -> FrozenDate:
            return cls(frozen.year, frozen.month, frozen.day)

    monkeypatch.setattr(real_datetime, "datetime", FrozenDatetime)
    monkeypatch.setattr(real_datetime, "date", FrozenDate)
    monkeypatch.setattr(real_time, "time", lambda: frozen.timestamp())
    monkeypatch.setattr(real_time, "time_ns", lambda: int(frozen.timestamp() * 1e9))
    # `from datetime import date/datetime` style bindings inside the module under test
    for name, fake in (("date", FrozenDate), ("datetime", FrozenDatetime)):
        if hasattr(periods_module, name):
            monkeypatch.setattr(periods_module, name, fake)


@pytest.mark.parametrize(
    "frozen",
    [
        real_datetime.datetime(1999, 1, 1, 0, 0, 0),
        real_datetime.datetime(2010, 6, 15, 12, 0, 0),
        real_datetime.datetime(2026, 11, 25, 9, 30, 0),
        real_datetime.datetime(2031, 12, 31, 23, 59, 59),
        real_datetime.datetime(2050, 6, 30, 3, 0, 0),
    ],
    ids=lambda d: d.date().isoformat(),
)
def test_results_do_not_depend_on_the_system_clock(
    monkeypatch: pytest.MonkeyPatch, frozen: real_datetime.datetime
) -> None:
    """Results are identical whatever 'today' is (1999 .. 2050): relative phrases use only the
    filed periods. Compares against the same calls made before the clock is frozen."""
    expected = {
        (t, i): resolve_period(t, latest)
        for t in SAMPLE_INPUTS
        for i, latest in enumerate((LATEST_FY, LATEST_Q))
    }
    _freeze_clock(monkeypatch, frozen)
    for (text, i), want in expected.items():
        got = resolve_period(text, (LATEST_FY, LATEST_Q)[i])
        assert got == want, f"{text!r} changed when the clock was {frozen}"


@pytest.mark.parametrize("latest_year", [1995, 2010, 2024, 2031])
def test_last_year_tracks_filings_not_today(latest_year: int) -> None:
    """'last year' equals the company's latest filed FY whether that is far in the past (1995) or
    in the future relative to today (2031): proves the anchor is LatestFiled, not date.today()."""
    latest = filed_fy(latest_year)
    assert_resolved(resolve_period("last year", latest), [fy(latest_year)])
    assert_resolved(resolve_period(None, latest), [fy(latest_year)], assumed=True)
    assert_resolved(resolve_period("prior year", latest), [fy(latest_year - 1)])


# --- properties --------------------------------------------------------------------------------

PROP = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

YEARS = st.integers(min_value=1994, max_value=2026)
QUARTERS = st.integers(min_value=1, max_value=4)
PAD = st.sampled_from(["", " ", "  ", "\t", "\n "])

FY_FORMATS = [
    "FY{y}",
    "fy{y}",
    "Fy {y}",
    "FY  {y}",
    "fiscal {y}",
    "FISCAL {y}",
    "fiscal year {y}",
    "Fiscal   Year {y}",
    "{y}",
]
ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth"}
Q_FORMATS = [
    "Q{n} {y}",
    "q{n} {y}",
    "Q{n}   {y}",
    "Q{n} FY{y}",
    "q{n} fy{y}",
    "Q{n} FY {y}",
    "{ord} quarter {y}",
    "{Ord} Quarter {y}",
    "{ORD}  QUARTER  {y}",
]


@PROP
@given(year=YEARS, fmt=st.sampled_from(FY_FORMATS), lead=PAD, trail=PAD)
def test_property_every_fy_format_round_trips(year: int, fmt: str, lead: str, trail: str) -> None:
    """Any year 1994..2026 in any supported FY spelling, padded with any whitespace, resolves to
    the same PeriodRef(year, 'FY') and never to a clarification or an assumed default."""
    text = lead + fmt.format(y=year) + trail
    assert_resolved(resolve_period(text, WIDE), [fy(year)])


@PROP
@given(year=YEARS, n=QUARTERS, fmt=st.sampled_from(Q_FORMATS), lead=PAD, trail=PAD)
def test_property_every_quarter_format_round_trips(
    year: int, n: int, fmt: str, lead: str, trail: str
) -> None:
    """Any year 1994..2026 and quarter 1..4 in any supported spelling resolves to PeriodRef(year,
    'Q<n>'): all spellings of one period agree."""
    text = (
        lead
        + fmt.format(y=year, n=n, ord=ORDINALS[n], Ord=ORDINALS[n].title(), ORD=ORDINALS[n].upper())
        + trail
    )
    assert_resolved(resolve_period(text, WIDE), [q(year, n)])


@PROP
@given(year=YEARS, n=QUARTERS)
def test_property_all_spellings_of_one_period_agree(year: int, n: int) -> None:
    """Different spellings of the same period produce byte-identical resolutions."""
    fy_results = {resolve_period(f.format(y=year), WIDE).model_dump_json() for f in FY_FORMATS}
    assert len(fy_results) == 1
    q_results = {
        resolve_period(
            f.format(
                y=year, n=n, ord=ORDINALS[n], Ord=ORDINALS[n].title(), ORD=ORDINALS[n].upper()
            ),
            WIDE,
        ).model_dump_json()
        for f in Q_FORMATS
    }
    assert len(q_results) == 1


@PROP
@given(
    year=st.one_of(st.integers(0, MIN_YEAR - 1), st.integers(2027, 99999)),
    fmt=st.sampled_from(["FY{y}", "fiscal {y}", "{y}", "Q3 {y}", "Q2 FY{y}"]),
)
def test_property_years_outside_range_clarify(year: int, fmt: str) -> None:
    """Any integer below 1993 or above the latest filed year (2026) clarifies in every form."""
    assert_clarifies(resolve_period(fmt.format(y=year), WIDE))


@PROP
@given(
    quarter=st.one_of(st.integers(-5, 0), st.integers(5, 99)),
    year=YEARS,
    fmt=st.sampled_from(["Q{n} {y}", "Q{n} FY{y}"]),
)
def test_property_invalid_quarter_numbers_clarify(quarter: int, year: int, fmt: str) -> None:
    """Any quarter number outside 1..4 clarifies, for every valid year."""
    assert_clarifies(resolve_period(fmt.format(n=quarter, y=year), WIDE))


@PROP
@given(
    latest_year=st.integers(1994, 2060),
    quarter=st.sampled_from([None, 1, 2, 3]),
    ahead=st.booleans(),
)
def test_property_relative_periods_depend_only_on_latest_filed(
    latest_year: int, quarter: int | None, ahead: bool
) -> None:
    """For any LatestFiled: 'last year' = FY(latest), 'prior year' = FY(latest-1), None = FY(latest)
    assumed, 'most recent quarter' = latest quarter (clarify when the latest filing is an FY)."""
    if quarter is None:
        latest = filed_fy(latest_year)
    else:
        latest = filed_quarter(latest_year, latest_year + 1 if ahead else latest_year, quarter)
    assert_resolved(resolve_period("last year", latest), [fy(latest_year)])
    assert_resolved(resolve_period("prior year", latest), [fy(latest_year - 1)])
    assert_resolved(resolve_period(None, latest), [fy(latest_year)], assumed=True)
    if latest.latest_quarter is None:
        assert_clarifies(resolve_period("most recent quarter", latest))
    else:
        assert_resolved(resolve_period("most recent quarter", latest), [latest.latest_quarter])


@PROP
@given(
    d=st.dates(min_value=real_datetime.date(1990, 1, 1), max_value=real_datetime.date(2030, 12, 31))
)
def test_property_calendar_dates_never_become_fiscal_periods(d: real_datetime.date) -> None:
    """A calendar date in any common layout never yields a period: the fiscal year of a date
    depends on the company's year end, so the resolver must not infer it."""
    for text in (
        d.isoformat(),
        d.strftime("%m/%d/%Y"),
        d.strftime("%B %d, %Y"),
        d.strftime("%b %Y"),
        f"as of {d.isoformat()}",
    ):
        assert_clarifies(resolve_period(text, WIDE))


TOKENS = st.sampled_from(
    ["FY", "fy", "Q", "Q3", "Q5", "fiscal", "year", "quarter", "third", "2023", "1993", "99", "3",
     "last", "prior", "most", "recent", "trailing", "ttm", "-", "/", "'", " ", "  ", "and", "the"]
)  # fmt: skip


def _assert_well_formed(res: PeriodResolution, text: str | None, latest: LatestFiled) -> None:
    assert isinstance(res, PeriodResolution)
    resolved = res.clarification is None
    assert resolved == bool(res.periods), f"exactly one of periods/clarification for {text!r}"
    if not resolved:
        assert isinstance(res.clarification, str) and res.clarification.strip()
        assert res.assumed is False
    upper = max(latest.fiscal_year, latest.latest.fiscal_year)
    for p in res.periods:
        assert MIN_YEAR <= p.fiscal_year <= upper
    if res.assumed:
        assert res.periods == [fy(latest.fiscal_year)]
        assert text is None or text.strip() == ""


@PROP
@given(text=st.one_of(st.none(), st.text(max_size=60)))
def test_property_never_raises_on_arbitrary_text(text: str | None) -> None:
    """resolve_period never raises on arbitrary Unicode: it returns either valid periods in
    range or a clarification with no periods (and assumed only for blank input)."""
    _assert_well_formed(resolve_period(text, LATEST_Q), text, LATEST_Q)


@PROP
@given(text=st.lists(TOKENS, max_size=7).map("".join))
def test_property_never_raises_on_period_like_noise(text: str) -> None:
    """Shuffled period vocabulary ('FY Q5 2023 last', 'Q3-99') never raises and always meets the
    well-formedness invariants (near-miss inputs are the ones most likely to break a regex)."""
    _assert_well_formed(resolve_period(text, LATEST_FY), text, LATEST_FY)
    _assert_well_formed(resolve_period(text, LATEST_Q), text, LATEST_Q)


@PROP
@given(text=st.one_of(st.none(), st.text(max_size=40)))
def test_property_resolution_is_a_pure_function(text: str | None) -> None:
    """Calling twice yields equal results for arbitrary input (no hidden state or cache bleed)."""
    assert resolve_period(text, LATEST_FY) == resolve_period(text, LATEST_FY)
