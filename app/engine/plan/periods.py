"""Period normalization: free text -> PeriodRef, relative to the company's latest FILED period.

STUB created once by Claude (M5 step 2). OWNED BY SHEKAR: he implements it; Claude only reads,
tests and reviews (`tests/unit/engine/test_periods.py`). M05 section 3.2: relative periods never
use today's date, only what the company has filed; with no period given, the latest FY is used and
the answer must say so; "trailing" and other unsupported forms ask for clarification.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.engine.plan.schema import FiscalPeriodName, PeriodRef


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


def resolve_period(text: str | None, latest: LatestFiled) -> PeriodResolution:
    """Normalize ``text`` ("FY2023", "fiscal 2023", "2023", "Q3 2022", "last year", "most recent
    quarter", None, "trailing twelve months", ...) against ``latest``."""
    raise NotImplementedError("Shekar implements periods (M05 section 3.2)")


__all__ = ["FiscalPeriodName", "LatestFiled", "PeriodResolution", "resolve_period"]
