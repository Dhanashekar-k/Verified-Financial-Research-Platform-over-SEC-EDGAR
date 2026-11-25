"""Period normalization: free text -> PeriodRef, relative to the company's latest FILED period.

Shekar-owned module. Implemented by Claude at Shekar's request (2026-11-25, ADR-047) against
`tests/unit/engine/test_periods.py`; Shekar reviews it with /interview-me.

M05 section 3.2: relative periods never use today's date, only what the company has filed; with no
period given the latest FY is used and the answer must say so; "trailing" and other unsupported
forms ask for clarification.

The rule is a closed grammar: a phrase is either matched in full by one form below or it asks for
clarification. Nothing is guessed from a partial match, and nothing is inferred from a calendar
date, because the fiscal year of a date depends on the company's own year end.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from app.engine.plan.schema import FiscalPeriodName, PeriodRef

MIN_FISCAL_YEAR = 1993  # EDGAR's first year; nothing earlier can be in the filings


class LatestFiled(BaseModel):
    """What the company has filed (read from `facts`/`filings`), the anchor for relative periods."""

    model_config = ConfigDict(frozen=True)

    fiscal_year: int  # latest fiscal year with an annual (FY) filing
    latest: PeriodRef  # the most recent filed period of any kind (FY, or Q1 to Q3)

    @property
    def latest_quarter(self) -> PeriodRef | None:
        return self.latest if self.latest.fiscal_period != "FY" else None


class PeriodResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    periods: list[PeriodRef] = []
    clarification: str | None = None  # set when the text is unsupported or ambiguous
    assumed: bool = False  # no period given: the latest FY was used, the answer must state it


ASK_PERIOD = (
    "Which fiscal period do you mean? Name one, for example FY2023, Q3 2022, last year or most "
    "recent quarter."
)
ASK_NO_TRAILING = (
    "Trailing and year-to-date totals are not supported. Do you want a fiscal year (for example "
    "FY2023) or a single quarter (for example Q3 2022)?"
)
ASK_ONE_PERIOD = "Which fiscal years or quarters do you mean? Please name each one."
ASK_NO_QUARTER = (
    "The latest filing on record is an annual report, so there is no newer quarter. Which quarter "
    "do you mean (for example Q3 2022)?"
)

_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_YEAR = r"(?P<year>[0-9]{4})"
# "fy2023", "fy 2023", "fiscal 2023", "fiscal year 2023"
_FY_WORD = r"(?:fy ?|fiscal (?:year )?)"
_ORD = r"(?P<ord>first|second|third|fourth)"

_FISCAL_YEAR = re.compile(rf"{_FY_WORD}?{_YEAR}")
_QUARTERS = tuple(
    re.compile(pattern)
    for pattern in (
        # "q3 2022", "q3 fy2022", "q3 of fiscal 2022"
        rf"q(?P<n>[1-4]) (?:of )?{_FY_WORD}?{_YEAR}",
        # "fy2022 q3", "fiscal 2022 q3"
        rf"{_FY_WORD}{_YEAR} q(?P<n>[1-4])",
        # "third quarter 2022", "third quarter of fiscal year 2022"
        rf"{_ORD} (?:fiscal )?quarter (?:of )?{_FY_WORD}?{_YEAR}",
    )
)
# Leading words that change nothing: "in fiscal 2023", "the latest year".
_FILLER = re.compile(r"^(?:(?:in|for|during) )?(?:the )?")

_LATEST_FY = frozenset(
    {
        "last year",
        "last fiscal year",
        "latest year",
        "latest fiscal year",
        "most recent year",
        "most recent fiscal year",
    }
)
_PRIOR_FY = frozenset({"prior year", "prior fiscal year", "year before"})
_LATEST_QUARTER = frozenset(
    {"last quarter", "latest quarter", "most recent quarter", "most recent fiscal quarter"}
)
_TRAILING = re.compile(r"\b(?:trailing|ttm|ltm|ytd|year to date|last twelve months)\b")
_WINDOW = re.compile(
    r"\b(?:last|past|previous) (?:[0-9]+|two|three|four|five|six|seven|eight|nine|ten) "
    r"(?:fiscal )?(?:years|quarters)\b"
)


def _ask(question: str) -> PeriodResolution:
    return PeriodResolution(clarification=question)


def _checked(year: int, fiscal_period: FiscalPeriodName, upper: int) -> PeriodResolution:
    """One period, or a clarification when the year is outside what has been filed."""
    if not MIN_FISCAL_YEAR <= year <= upper:
        return _ask(
            f"The filings on record cover fiscal years {MIN_FISCAL_YEAR} to {upper}. Which of "
            "those do you mean?"
        )
    return PeriodResolution(periods=[PeriodRef(fiscal_year=year, fiscal_period=fiscal_period)])


def resolve_period(text: str | None, latest: LatestFiled) -> PeriodResolution:
    """Normalize ``text`` ("FY2023", "fiscal 2023", "2023", "Q3 2022", "last year", "most recent
    quarter", None, "trailing twelve months", ...) against ``latest``."""
    if text is None or not text.strip():
        return PeriodResolution(
            periods=[PeriodRef(fiscal_year=latest.fiscal_year, fiscal_period="FY")], assumed=True
        )
    # A newer 10-Q can carry a later fiscal year than the latest 10-K.
    upper = max(latest.fiscal_year, latest.latest.fiscal_year)
    phrase = " ".join(text.split()).lower()
    if _TRAILING.search(phrase):
        return _ask(ASK_NO_TRAILING)
    if _WINDOW.search(phrase):
        return _ask(ASK_ONE_PERIOD)
    phrase = _FILLER.sub("", phrase, count=1)

    if phrase in _LATEST_FY:
        return _checked(latest.fiscal_year, "FY", upper)
    if phrase in _PRIOR_FY:
        return _checked(latest.fiscal_year - 1, "FY", upper)
    if phrase in _LATEST_QUARTER:
        quarter = latest.latest_quarter
        if quarter is None:
            return _ask(ASK_NO_QUARTER)
        return _checked(quarter.fiscal_year, quarter.fiscal_period, upper)

    if match := _FISCAL_YEAR.fullmatch(phrase):
        return _checked(int(match["year"]), "FY", upper)
    for pattern in _QUARTERS:
        if match := pattern.fullmatch(phrase):
            groups = match.groupdict()
            n = int(groups["n"]) if groups.get("n") else _ORDINALS[groups["ord"]]
            names: tuple[FiscalPeriodName, ...] = ("Q1", "Q2", "Q3", "Q4")
            return _checked(int(groups["year"]), names[n - 1], upper)
    return _ask(ASK_PERIOD)


__all__ = ["FiscalPeriodName", "LatestFiled", "PeriodResolution", "resolve_period"]
