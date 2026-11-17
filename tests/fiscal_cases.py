"""Shared, hand-derived case data for the M01 fiscal-period and version-selection tests.

Used by `tests/unit/test_fiscal.py`, `tests/unit/test_versions.py` and the Spark twins in
`tests/integration/`. Every expected value below was derived by hand from the rules in
`docs/plan/M01_xbrl_lakehouse.md` section 3.5, not by running any implementation. The comment
next to each expectation says why it follows from the rules.

Facts are normalised to plain dicts (see `INPUT_FIELDS` and the checker functions) so that the
pure-Python reference and the Spark functions are held to exactly the same expectations.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from pipelines.common.fiscal_ref import FactInput, FactVersion, FiscalFact, SubmissionPeriod


class _Any:
    """Wildcard for an expectation that is deliberately left unchecked."""

    def __repr__(self) -> str:
        return "ANY"


ANY: Any = _Any()

INPUT_FIELDS = (
    "cik",
    "adsh",
    "tag",
    "taxonomy",
    "uom",
    "qtrs",
    "period_end",
    "value",
    "form",
    "filed",
    "fye_mmdd",
    "sub_fy",
    "sub_period",
)

Natural = tuple[date, int, str]  # (period_end, qtrs, accession): a parent in `derived_from`


def acc(cik: int, yy: int, n: int) -> str:
    """A well-formed accession number."""
    return f"{cik:010d}-{yy:02d}-{n:06d}"


@dataclass(frozen=True)
class Sub:
    """One submission (a `sub` row) from which facts are issued."""

    cik: int
    adsh: str
    form: str
    filed: date
    fye: str
    sub_fy: int | None
    sub_period: date

    def fact(
        self,
        tag: str,
        period_end: date,
        qtrs: int,
        value: str | Decimal = "1.0000",
        *,
        uom: str = "USD",
        taxonomy: str = "us-gaap",
    ) -> FactInput:
        return FactInput(
            cik=self.cik,
            adsh=self.adsh,
            tag=tag,
            taxonomy=taxonomy,
            uom=uom,
            qtrs=qtrs,
            period_end=period_end,
            value=Decimal(value),
            form=self.form,
            filed=self.filed,
            fye_mmdd=self.fye,
            sub_fy=self.sub_fy,
            sub_period=self.sub_period,
        )


@dataclass(frozen=True)
class Expect:
    """Expected derived columns of one output row, found by its natural key."""

    sub: Sub
    tag: str
    period_end: date
    qtrs: int
    fiscal_year: int
    fiscal_period: str
    calendar_frame: Any = ANY  # str | None | ANY
    dq_flags: frozenset[str] = frozenset()  # exact for original rows; a subset for derived rows
    value: Any = ANY
    period_start: Any = ANY
    derived_from: tuple[Natural, ...] | None = None  # set => this is the derived Q4 row

    @property
    def key(self) -> tuple[int, str, str, date, int]:
        return (self.sub.cik, self.sub.adsh, self.tag, self.period_end, self.qtrs)


def to_rows(out: Sequence[FiscalFact]) -> list[dict[str, Any]]:
    """Normalise reference output to the row-dict shape the checkers use."""
    rows = []
    for ff in out:
        r: dict[str, Any] = {k: getattr(ff.fact, k) for k in INPUT_FIELDS}
        r.update(
            period_start=ff.period_start,
            fiscal_year=ff.fiscal_year,
            fiscal_period=ff.fiscal_period,
            calendar_frame=ff.calendar_frame,
            dq_flags=frozenset(ff.dq_flags),
            derived_from=ff.derived_from,
        )
        rows.append(r)
    return rows


_adsh_counter = iter(range(1, 10**6))


def submission(
    cik: int, form: str, sub_fy: int | None, period: date, fye: str = "1231"
) -> SubmissionPeriod:
    """A `SubmissionPeriod` with a fresh accession, for the label-offset tests."""
    return SubmissionPeriod(cik, f"adsh-{next(_adsh_counter):06d}", form, sub_fy, period, fye)


def ex(
    sub: Sub,
    tag: str,
    period_end: date,
    qtrs: int,
    fiscal_year: int,
    fiscal_period: str,
    calendar_frame: Any = ANY,
    *,
    flags: Sequence[str] = (),
    value: Any = ANY,
    period_start: Any = ANY,
    derived_from: tuple[Natural, ...] | None = None,
) -> Expect:
    return Expect(
        sub,
        tag,
        period_end,
        qtrs,
        fiscal_year,
        fiscal_period,
        calendar_frame,
        frozenset(flags),
        value,
        period_start,
        derived_from,
    )


@dataclass(frozen=True)
class FiscalScenario:
    name: str
    facts: tuple[FactInput, ...]
    expect: tuple[Expect, ...]
    n_derived: int = 0

    def __repr__(self) -> str:
        return self.name


def scenario(
    name: str,
    rows: Sequence[tuple[FactInput, Expect] | FactInput],
    derived: Sequence[Expect] = (),
) -> FiscalScenario:
    """Facts with an expectation are `(fact, Expect)` pairs; bare facts are context only."""
    facts: list[FactInput] = []
    expects: list[Expect] = []
    for r in rows:
        if isinstance(r, tuple):
            facts.append(r[0])
            expects.append(r[1])
        else:
            facts.append(r)
    return FiscalScenario(name, tuple(facts), tuple(expects) + tuple(derived), len(derived))


def row(
    sub: Sub,
    tag: str,
    period_end: date,
    qtrs: int,
    fiscal_year: int,
    fiscal_period: str,
    calendar_frame: Any = ANY,
    *,
    value: str = "1.0000",
    uom: str = "USD",
    flags: Sequence[str] = (),
) -> tuple[FactInput, Expect]:
    """A fact plus the expectation for its output row."""
    fact = sub.fact(tag, period_end, qtrs, value, uom=uom)
    return fact, ex(
        sub, tag, period_end, qtrs, fiscal_year, fiscal_period, calendar_frame, flags=flags
    )


# --------------------------------------------------------------------------------------
# Companies and submissions
# --------------------------------------------------------------------------------------

APPLE = 320193
X = "0000320193-23-000106"  # Apple 10-K FY2023, filed 2023-11-03
Y = "0000320193-23-000077"  # Apple 10-Q for the quarter ended 2023-07-01, filed 2023-08-04
Q1_23_ADSH = "0000320193-23-000006"
Q2_23_ADSH = "0000320193-23-000064"
K24_ADSH = "0000320193-24-000123"  # Apple 10-K FY2024, filed 2024-11-01
Q3_24_ADSH = "0000320193-24-000081"  # Apple 10-Q for the quarter ended 2024-06-29
Q3_23A_ADSH = "0000320193-23-000090"  # an amended 10-Q/A for the quarter ended 2023-07-01

K23 = Sub(APPLE, X, "10-K", date(2023, 11, 3), "0930", 2023, date(2023, 9, 30))
Q3_23 = Sub(APPLE, Y, "10-Q", date(2023, 8, 4), "0930", 2023, date(2023, 7, 1))
Q1_23 = Sub(APPLE, Q1_23_ADSH, "10-Q", date(2023, 2, 3), "0930", 2023, date(2022, 12, 31))
Q2_23 = Sub(APPLE, Q2_23_ADSH, "10-Q", date(2023, 5, 5), "0930", 2023, date(2023, 4, 1))

K24 = Sub(APPLE, K24_ADSH, "10-K", date(2024, 11, 1), "0930", 2024, date(2024, 9, 28))
Q3_24 = Sub(APPLE, Q3_24_ADSH, "10-Q", date(2024, 8, 2), "0930", 2024, date(2024, 6, 29))
Q3_23A = Sub(APPLE, Q3_23A_ADSH, "10-Q/A", date(2023, 9, 15), "0930", 2023, date(2023, 7, 1))

FY23_END = date(2023, 9, 30)
FY22_END = date(2022, 9, 24)

# Apple net sales in USD (FY2023 10-K): FY 383,285M; 9M 293,787M; so Q4 = 89,498M.
REV_FY23 = "383285000000.0000"
REV_9M23 = "293787000000.0000"
REV_Q4_23 = "89498000000.0000"  # 383285 - 293787 = 89498
REV_FY22 = "394328000000.0000"
REV_9M22 = "304182000000.0000"
REV_Q4_22 = "90146000000.0000"  # 394328 - 304182 = 90146

MSFT = 789019
MS_K = Sub(MSFT, acc(MSFT, 23, 1), "10-K", date(2023, 7, 27), "0630", 2023, date(2023, 6, 30))
MS_Q1 = Sub(MSFT, acc(MSFT, 22, 2), "10-Q", date(2022, 10, 25), "0630", 2023, date(2022, 9, 30))
MS_Q2 = Sub(MSFT, acc(MSFT, 23, 3), "10-Q", date(2023, 1, 24), "0630", 2023, date(2022, 12, 31))
MS_Q3 = Sub(MSFT, acc(MSFT, 23, 4), "10-Q", date(2023, 4, 25), "0630", 2023, date(2023, 3, 31))

A_RET = 1000001  # late-January FYE; names the year ending Jan 2023 "fiscal 2023" (offset 0)
B_RET = 1000002  # late-January FYE; names the year ending Jan 2023 "fiscal 2022" (offset -1)
A_K = [
    Sub(A_RET, acc(A_RET, 21, 1), "10-K", date(2021, 3, 25), "0131", 2021, date(2021, 1, 31)),
    Sub(A_RET, acc(A_RET, 22, 1), "10-K", date(2022, 3, 24), "0131", 2022, date(2022, 1, 30)),
    Sub(A_RET, acc(A_RET, 23, 1), "10-K", date(2023, 3, 23), "0131", 2023, date(2023, 1, 29)),
]
A_Q = Sub(A_RET, acc(A_RET, 22, 2), "10-Q", date(2022, 6, 3), "0131", 2023, date(2022, 4, 30))
B_K = [
    Sub(B_RET, acc(B_RET, 21, 1), "10-K", date(2021, 3, 25), "0131", 2020, date(2021, 1, 31)),
    Sub(B_RET, acc(B_RET, 22, 1), "10-K", date(2022, 3, 24), "0131", 2021, date(2022, 1, 30)),
    Sub(B_RET, acc(B_RET, 23, 1), "10-K", date(2023, 3, 23), "0131", 2022, date(2023, 1, 29)),
]
B_Q = Sub(B_RET, acc(B_RET, 22, 2), "10-Q", date(2022, 6, 3), "0131", 2022, date(2022, 4, 30))
B_Q3 = Sub(B_RET, acc(B_RET, 22, 3), "10-Q", date(2022, 12, 2), "0131", 2022, date(2022, 10, 29))

VARY = 1000003  # calendar-year company whose label offsets are {0, 0, 0, -1}
V_K = [
    Sub(VARY, acc(VARY, 20, 1), "10-K", date(2020, 2, 20), "1231", 2019, date(2019, 12, 31)),
    Sub(VARY, acc(VARY, 21, 1), "10-K", date(2021, 2, 20), "1231", 2020, date(2020, 12, 31)),
    Sub(VARY, acc(VARY, 22, 1), "10-K", date(2022, 2, 20), "1231", 2021, date(2021, 12, 31)),
    Sub(VARY, acc(VARY, 23, 1), "10-K", date(2023, 2, 20), "1231", 2021, date(2022, 12, 31)),
]
V_Q = Sub(VARY, acc(VARY, 22, 2), "10-Q", date(2022, 5, 6), "1231", 2022, date(2022, 3, 31))

CAL = 1000004  # calendar-year company
CAL_K = Sub(CAL, acc(CAL, 24, 1), "10-K", date(2024, 2, 15), "1231", 2023, date(2023, 12, 31))
CAL_Q = Sub(CAL, acc(CAL, 23, 2), "10-Q", date(2023, 5, 5), "1231", 2023, date(2023, 3, 31))

MOVER = 1000005  # moves its fiscal year end from Sep 30 to Jun 30 via a 10-KT
MV_K = Sub(MOVER, acc(MOVER, 22, 1), "10-K", date(2022, 11, 18), "0930", 2022, date(2022, 9, 30))
MV_KT = Sub(MOVER, acc(MOVER, 23, 1), "10-KT", date(2023, 8, 25), "0630", 2023, date(2023, 6, 30))

QCO = 1000006  # 10-Qs carry a different sub.fy label from the 10-Ks
QK = [
    Sub(QCO, acc(QCO, 22, 1), "10-K", date(2022, 2, 20), "1231", 2021, date(2021, 12, 31)),
    Sub(QCO, acc(QCO, 23, 1), "10-K", date(2023, 2, 20), "1231", 2022, date(2022, 12, 31)),
]
QQ = [
    Sub(QCO, acc(QCO, 22, 2), "10-Q", date(2022, 5, 5), "1231", 2021, date(2022, 3, 31)),
    Sub(QCO, acc(QCO, 22, 3), "10-Q", date(2022, 8, 5), "1231", 2021, date(2022, 6, 30)),
    Sub(QCO, acc(QCO, 22, 4), "10-Q", date(2022, 11, 4), "1231", 2021, date(2022, 9, 30)),
]

MODECO = 1000007  # one submission with many facts (offset 0) and two with one fact (offset -1)
MD_K = [
    Sub(MODECO, acc(MODECO, 21, 1), "10-K", date(2021, 2, 20), "1231", 2020, date(2020, 12, 31)),
    Sub(MODECO, acc(MODECO, 22, 1), "10-K", date(2022, 2, 20), "1231", 2020, date(2021, 12, 31)),
    Sub(MODECO, acc(MODECO, 23, 1), "10-K", date(2023, 2, 20), "1231", 2021, date(2022, 12, 31)),
]

OTHER = 999999  # a company with an Apple-like Sep FYE, used for different-cik negatives
OT_K = Sub(OTHER, acc(OTHER, 23, 1), "10-K", date(2023, 11, 3), "0930", 2023, date(2023, 9, 30))
OT_Q3 = Sub(OTHER, acc(OTHER, 23, 2), "10-Q", date(2023, 8, 4), "0930", 2023, date(2023, 7, 1))

# --------------------------------------------------------------------------------------
# Scenarios: fiscal_year / fiscal_period / calendar_frame / dq_flags (rules 1-4, 6)
# --------------------------------------------------------------------------------------

_apple_periods = scenario(
    "apple_52_53_week_periods",
    [
        # Rules 1/2: FYE '0930'; FY ends 2023-09-30 and 2022-09-24 are inside the +7d window.
        # Rule 6: a Sep 30 FY is ~273 days from Dec 31, so no calendar frame.
        row(K23, "Revenues", FY23_END, 4, 2023, "FY", None),
        # The FY2023 10-K also carries FY2022 as a comparative: it must be FY2022, not FY2023.
        row(K23, "Revenues", FY22_END, 4, 2022, "FY", None),
        # Instants (qtrs 0) get a real period label: FY when the date is the year end (quarter
        # index 4), else Q1-Q3. The frame is the calendar quarter with a trailing I (Sep => Q3).
        row(K23, "Assets", FY23_END, 0, 2023, "FY", "CY2023Q3I"),
        row(K23, "Assets", FY22_END, 0, 2022, "FY", "CY2022Q3I"),
        # 2022-12-31 is one quarter after FYE 2022-09-30 -> Q1 of fiscal 2023; Dec => CY Q4.
        row(Q1_23, "Revenues", date(2022, 12, 31), 1, 2023, "Q1", "CY2022Q4"),
        # 2021-12-25 is 6 days before the nominal Q1 end of fiscal 2022 -> Q1, fiscal 2022.
        row(Q1_23, "Revenues", date(2021, 12, 25), 1, 2022, "Q1", "CY2021Q4"),
        # 2023-04-01: one day past the nominal Q2 end (Mar 31) -> Q2; April => CY Q2.
        row(Q2_23, "Revenues", date(2023, 4, 1), 1, 2023, "Q2", "CY2023Q2"),
        row(Q2_23, "GrossProfit", date(2023, 4, 1), 2, 2023, "H1", None),  # qtrs 2 => H1, no frame
        row(Q3_23, "GrossProfit", date(2023, 7, 1), 3, 2023, "9M", None),  # qtrs 3 => 9M, no frame
        # 2023-07-01: one day past the nominal Q3 end (Jun 30) -> Q3; July => CY Q3.
        row(Q3_23, "Revenues", date(2023, 7, 1), 1, 2023, "Q3", "CY2023Q3"),
        # Mid-year balance sheets: Q1-Q3 by quarter index; qtrs 0 and the I frame mark the instant.
        row(Q1_23, "Assets", date(2022, 12, 31), 0, 2023, "Q1", "CY2022Q4I"),
        row(Q2_23, "Assets", date(2023, 4, 1), 0, 2023, "Q2", "CY2023Q2I"),
        row(Q3_23, "Assets", date(2023, 7, 1), 0, 2023, "Q3", "CY2023Q3I"),
    ],
)

_apple_fsds_rounded = scenario(
    "apple_fsds_month_end_rounded_fy_ends",
    [
        # FSDS rounds ddate to month end: Sep 30 of each year, all within FYE+7d of their year.
        row(K23, "Revenues", date(2023, 9, 30), 4, 2023, "FY", None),
        row(K23, "Revenues", date(2022, 9, 30), 4, 2022, "FY", None),
        row(K23, "Revenues", date(2021, 9, 30), 4, 2021, "FY", None),
    ],
)

_msft = scenario(
    "msft_june_fye_quarters",
    [
        # FYE '0630': fiscal 2023 runs 2022-07-01..2023-06-30; sub.fy is 2023 everywhere (offset 0).
        row(MS_Q1, "Revenues", date(2022, 9, 30), 1, 2023, "Q1", "CY2022Q3"),
        row(MS_Q2, "Revenues", date(2022, 12, 31), 1, 2023, "Q2", "CY2022Q4"),
        row(MS_Q3, "Revenues", date(2023, 3, 31), 1, 2023, "Q3", "CY2023Q1"),
        row(MS_K, "Revenues", date(2023, 6, 30), 1, 2023, "Q4", "CY2023Q2"),
        row(MS_Q2, "NetIncomeLoss", date(2022, 12, 31), 2, 2023, "H1", None),
        row(MS_Q3, "NetIncomeLoss", date(2023, 3, 31), 3, 2023, "9M", None),
        # A Jun 30 FY is 183 days from Dec 31: no calendar frame (rule 6).
        row(MS_K, "OperatingIncomeLoss", date(2023, 6, 30), 4, 2023, "FY", None),
        row(MS_K, "Assets", date(2023, 6, 30), 0, 2023, "FY", "CY2023Q2I"),
    ],
)

_msft_instants = scenario(
    "msft_instants_have_real_period_labels",
    [
        # One fiscal year of balance sheets (FYE 0630) is four distinct (fiscal_year, period)
        # labels, so "total assets, Q2 FY2023" and "total assets, FY2023" cannot collide.
        row(MS_Q1, "Assets", date(2022, 9, 30), 0, 2023, "Q1", "CY2022Q3I"),
        row(MS_Q2, "Assets", date(2022, 12, 31), 0, 2023, "Q2", "CY2022Q4I"),
        row(MS_Q3, "Assets", date(2023, 3, 31), 0, 2023, "Q3", "CY2023Q1I"),
        row(MS_K, "Assets", date(2023, 6, 30), 0, 2023, "FY", "CY2023Q2I"),
    ],
)

_cal = scenario(
    "calendar_year_company_frames",
    [
        row(CAL_K, "Revenues", date(2023, 12, 31), 4, 2023, "FY", "CY2023"),  # rule 6 annual
        row(CAL_K, "Assets", date(2023, 12, 31), 0, 2023, "FY", "CY2023Q4I"),
        row(CAL_Q, "Revenues", date(2023, 3, 31), 1, 2023, "Q1", "CY2023Q1"),
    ],
)

_ret_a = scenario(
    "retailer_label_offset_0",
    [
        # Rule 3: sub.fy - y_end(sub.period) = 0 for all three 10-Ks, so fiscal_year = y_end.
        row(A_K[0], "Assets", date(2021, 1, 31), 0, 2021, "FY"),
        row(A_K[1], "Assets", date(2022, 1, 30), 0, 2022, "FY"),
        row(A_K[2], "Assets", date(2023, 1, 29), 0, 2023, "FY"),
        # y_end(2023-01-28) = 2023 (inside the FYE 2023-01-31 window); offset 0 => 2023.
        row(A_K[2], "Revenues", date(2023, 1, 28), 4, 2023, "FY"),
        # y_end(2022-04-30) = 2023 (after FYE 2022-01-31); 3 months in => Q1 of fiscal 2023.
        row(A_Q, "Revenues", date(2022, 4, 30), 1, 2023, "Q1"),
    ],
)

_ret_b = scenario(
    "retailer_label_offset_minus_1",
    [
        # Rule 3: sub.fy - y_end(sub.period) = 2020-2021 = -1 etc., constant => offset -1.
        row(B_K[0], "Assets", date(2021, 1, 31), 0, 2020, "FY"),
        row(B_K[1], "Assets", date(2022, 1, 30), 0, 2021, "FY"),
        row(B_K[2], "Assets", date(2023, 1, 29), 0, 2022, "FY"),
        row(B_K[2], "Revenues", date(2023, 1, 28), 4, 2022, "FY"),  # y_end 2023 + (-1)
        row(B_Q, "Revenues", date(2022, 4, 30), 1, 2022, "Q1"),  # y_end 2023 + (-1), Q1
    ],
)

_vary = scenario(
    "label_offset_not_constant_flags_every_fact",
    [
        # Offsets per 10-K: 0, 0, 0, -1 -> mode 0, not constant. fiscal_year uses the mode, so
        # the 2022-12-31 balance sheet (sub.fy 2021) is still fiscal 2022. Every fact is flagged.
        row(
            V_K[0],
            "Assets",
            date(2019, 12, 31),
            0,
            2019,
            "FY",
            "CY2019Q4I",
            flags=["label_offset_varies"],
        ),
        row(
            V_K[1],
            "Assets",
            date(2020, 12, 31),
            0,
            2020,
            "FY",
            "CY2020Q4I",
            flags=["label_offset_varies"],
        ),
        row(
            V_K[2],
            "Assets",
            date(2021, 12, 31),
            0,
            2021,
            "FY",
            "CY2021Q4I",
            flags=["label_offset_varies"],
        ),
        row(
            V_K[3],
            "Assets",
            date(2022, 12, 31),
            0,
            2022,
            "FY",
            "CY2022Q4I",
            flags=["label_offset_varies"],
        ),
        row(
            V_Q,
            "Revenues",
            date(2022, 3, 31),
            1,
            2022,
            "Q1",
            "CY2022Q1",
            flags=["label_offset_varies"],
        ),
    ],
)

_mode_over_subs = scenario(
    "label_offset_mode_is_over_submissions_not_facts",
    [
        # Submission offsets: MD_K[0] 2020-2020 = 0 (five facts); MD_K[1] 2020-2021 = -1 and
        # MD_K[2] 2021-2022 = -1 (one fact each). Mode over SUBMISSIONS is -1 (2 of 3); a mode
        # over facts would be 0 (5 of 7). Not constant either way => flagged.
        row(MD_K[0], "Assets", date(2020, 12, 31), 0, 2019, "FY", flags=["label_offset_varies"]),
        row(
            MD_K[0],
            "Liabilities",
            date(2020, 12, 31),
            0,
            2019,
            "FY",
            flags=["label_offset_varies"],
        ),
        row(
            MD_K[0],
            "StockholdersEquity",
            date(2020, 12, 31),
            0,
            2019,
            "FY",
            flags=["label_offset_varies"],
        ),
        row(MD_K[0], "Cash", date(2020, 12, 31), 0, 2019, "FY", flags=["label_offset_varies"]),
        row(
            MD_K[0],
            "Inventory",
            date(2020, 12, 31),
            0,
            2019,
            "FY",
            flags=["label_offset_varies"],
        ),
        row(MD_K[1], "Assets", date(2021, 12, 31), 0, 2020, "FY", flags=["label_offset_varies"]),
        row(MD_K[2], "Assets", date(2022, 12, 31), 0, 2021, "FY", flags=["label_offset_varies"]),
    ],
)

_ten_q_ignored = scenario(
    "ten_q_sub_fy_does_not_move_label_offset",
    [
        # 10-K offsets: 0, 0. Three 10-Qs have sub.fy one lower (offset -1 each); if they were
        # counted the mode would flip to -1. Only 10-K/10-KT submissions feed rule 3.
        row(QK[0], "Assets", date(2021, 12, 31), 0, 2021, "FY"),
        row(QK[1], "Assets", date(2022, 12, 31), 0, 2022, "FY"),
        row(QQ[0], "Revenues", date(2022, 3, 31), 1, 2022, "Q1"),
        row(QQ[1], "Revenues", date(2022, 6, 30), 1, 2022, "Q2"),
        row(QQ[2], "Revenues", date(2022, 9, 30), 1, 2022, "Q3"),
    ],
)

_ten_kt = scenario(
    "ten_kt_transition_uses_own_submission_fye",
    [
        # Rule 1: fye comes from the SAME submission. 10-K FY2022 has fye 0930: y_end(2022-09-30)
        # = 2022. With the later fye 0630 it would wrongly be 2023.
        row(MV_K, "Revenues", date(2022, 9, 30), 4, 2022, "FY", None),
        # 10-KT (fye 0630): y_end(2023-06-30) = 2023; qtrs 3 => '9M'. Offsets: 0 and 0.
        row(MV_KT, "Revenues", date(2023, 6, 30), 3, 2023, "9M", None),
    ],
)

FISCAL_YEAR_SCENARIOS = [
    _apple_periods,
    _apple_fsds_rounded,
    _msft,
    _msft_instants,
    _cal,
    _ret_a,
    _ret_b,
    _vary,
    _mode_over_subs,
    _ten_q_ignored,
    _ten_kt,
]

# --------------------------------------------------------------------------------------
# Scenarios: rule 5 (Q4 = FY - 9M)
# --------------------------------------------------------------------------------------


def _apple_q4(name: str, nine_m_end: date) -> FiscalScenario:
    return scenario(
        name,
        [
            row(K23, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
            row(Q3_23, "Revenues", nine_m_end, 3, 2023, "9M", None, value=REV_9M23),
        ],
        derived=[
            ex(
                K23,
                "Revenues",
                FY23_END,
                1,
                2023,  # the FY row's fiscal_year
                "Q4",
                "CY2023Q3",  # qtrs 1 ending Sep 30 => calendar quarter 3
                flags=["q4_derived"],
                value=Decimal(REV_Q4_23),  # 383285000000 - 293787000000
                period_start=date(2023, 7, 1),  # Sep 30 minus 3 months = Jun 30, plus 1 day
                derived_from=((FY23_END, 4, X), (nine_m_end, 3, Y)),
            )
        ],
    )


_q4_exact = _apple_q4("q4_apple_exact_9m_ends_2023_07_01", date(2023, 7, 1))  # 91 days before FY
_q4_rounded = _apple_q4("q4_apple_fsds_rounded_9m_ends_2023_06_30", date(2023, 6, 30))  # 92 days

_q4_two_years = scenario(
    "q4_apple_two_fiscal_years_pair_within_own_year",
    [
        row(K23, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
        row(K23, "Revenues", FY22_END, 4, 2022, "FY", None, value=REV_FY22),
        row(Q3_23, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value=REV_9M23),
        row(Q3_23, "Revenues", date(2022, 6, 25), 3, 2022, "9M", None, value=REV_9M22),
    ],
    derived=[
        ex(
            K23,
            "Revenues",
            FY23_END,
            1,
            2023,
            "Q4",
            "CY2023Q3",
            flags=["q4_derived"],
            value=Decimal(REV_Q4_23),
            derived_from=((FY23_END, 4, X), (date(2023, 7, 1), 3, Y)),
        ),
        # FY2022 (2022-09-24) pairs with the 9M ending 2022-06-25 (91 days earlier), never with
        # the 9M of 2023; fiscal_year is the FY2022 row's.
        ex(
            K23,
            "Revenues",
            FY22_END,
            1,
            2022,
            "Q4",
            "CY2022Q3",
            flags=["q4_derived"],
            value=Decimal(REV_Q4_22),
            derived_from=((FY22_END, 4, X), (date(2022, 6, 25), 3, Y)),
        ),
    ],
)

_q4_retailer = scenario(
    "q4_retailer_label_offset_minus_1",
    [
        row(B_K[0], "Assets", date(2021, 1, 31), 0, 2020, "FY"),
        row(B_K[1], "Assets", date(2022, 1, 30), 0, 2021, "FY"),
        row(B_K[2], "Assets", date(2023, 1, 29), 0, 2022, "FY"),
        row(B_K[2], "Revenues", date(2023, 1, 28), 4, 2022, "FY", value="157000000.0000"),
        row(B_Q3, "Revenues", date(2022, 10, 29), 3, 2022, "9M", value="100250000.0000"),
    ],
    derived=[
        # 2022-10-29 is 91 days before 2023-01-28. Fiscal year = FY row's = 2023 - 1 = 2022.
        # quarter_index(2023-01-28) is 4 (3 days before FYE 2023-01-31). 157000000 - 100250000.
        ex(
            B_K[2],
            "Revenues",
            date(2023, 1, 28),
            1,
            2022,
            "Q4",
            flags=["q4_derived"],
            value=Decimal("56750000.0000"),
            derived_from=((date(2023, 1, 28), 4, B_K[2].adsh), (date(2022, 10, 29), 3, B_Q3.adsh)),
        ),
    ],
)

# Provenance tiers (decision of 2026-11-17): Q4 is derived per tier within
# (cik, tag, taxonomy, uom, fiscal_year), from RETAINED parents only (the rows `curated.facts`
# keeps: the latest and the original version of each parent; one row, the ORIGINAL version, when
# they have equal values). The original tier subtracts the original 9M from the original FY, the
# latest tier the latest from the latest. Each derived row carries its FY parent's form and
# accession. Tiers that land on the same fact_id (only the 9M restated) collapse to the row whose
# 9M parent was filed latest. Ties between parent candidates go to the latest filed. A directly
# reported Q4 for the same (cik, tag, taxonomy, uom, fiscal_year), in any accession, blocks it all.

REV_FY23_RESTATED = "380000000000.0000"  # FY2023 as restated in the FY2024 10-K
REV_Q4_23_LATEST = "86213000000.0000"  # 380000000000 - 293787000000

_q4_restated_fy = scenario(
    "q4_restated_fy_gets_one_q4_per_tier",
    [
        # FY2023 was reported in the 2023 10-K (original: filed in its own period) and restated in
        # the 2024 10-K (latest); the 9M has a single version, which is both original and latest.
        row(K23, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
        row(K24, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23_RESTATED),
        row(Q3_23, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value=REV_9M23),
    ],
    derived=[
        # original tier: original FY (2023 10-K) - the 9M; carries the 2023 10-K's accession.
        ex(
            K23,
            "Revenues",
            FY23_END,
            1,
            2023,
            "Q4",
            "CY2023Q3",
            flags=["q4_derived"],
            value=Decimal(REV_Q4_23),
            derived_from=((FY23_END, 4, X), (date(2023, 7, 1), 3, Y)),
        ),
        # latest tier: latest FY (2024 10-K) - the same 9M; carries the 2024 10-K's accession.
        ex(
            K24,
            "Revenues",
            FY23_END,
            1,
            2023,
            "Q4",
            "CY2023Q3",
            flags=["q4_derived"],
            value=Decimal(REV_Q4_23_LATEST),
            derived_from=((FY23_END, 4, K24_ADSH), (date(2023, 7, 1), 3, Y)),
        ),
    ],
)

_q4_comparatives = scenario(
    "q4_equal_comparatives_derive_once_from_the_native_filings",
    [
        # FY2023 and its 9M are repeated unchanged as comparatives in the FY2024 10-K and the Q3
        # FY2024 10-Q. Equal values collapse to ONE retained row per parent: the ORIGINAL version
        # (the native filing, what the answer cites). So exactly one Q4 is derived, from the 2023
        # 10-K and the 2023 10-Q, and it carries the 2023 10-K's accession, form and filed date.
        row(K23, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
        row(K24, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
        row(Q3_23, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value=REV_9M23),
        row(Q3_24, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value=REV_9M23),
    ],
    derived=[
        ex(
            K23,
            "Revenues",
            FY23_END,
            1,
            2023,
            "Q4",
            "CY2023Q3",
            flags=["q4_derived"],
            value=Decimal(REV_Q4_23),
            derived_from=((FY23_END, 4, X), (date(2023, 7, 1), 3, Y)),
        ),
    ],
)

_q4_only_9m_restated = scenario(
    "q4_only_9m_restated_keeps_one_row_with_the_latest_9m",
    [
        # FY2023 has one version; the 9M was restated in the Q3 FY2024 10-Q (293000M vs 293787M).
        # Both 9M versions are retained, so both tiers derive from the same FY row and land on
        # the same fact_id (same fact_key, same FY accession). The row whose 9M parent was filed
        # latest survives: 383285M - 293000M = 90285M, from the 2024 10-Q; the other is dropped.
        row(K23, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
        row(Q3_23, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value=REV_9M23),
        row(Q3_24, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value="293000000000.0000"),
    ],
    derived=[
        ex(
            K23,
            "Revenues",
            FY23_END,
            1,
            2023,
            "Q4",
            "CY2023Q3",
            flags=["q4_derived"],
            value=Decimal("90285000000.0000"),
            derived_from=((FY23_END, 4, X), (date(2023, 7, 1), 3, Q3_24_ADSH)),
        ),
    ],
)

_q4_direct_blocks_only_its_year = scenario(
    "q4_direct_row_blocks_only_its_own_fiscal_year",
    [
        # FY2022 has a directly reported Q4 (no FY - 9M for it); FY2023 does not and still derives.
        row(K23, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
        row(Q3_23, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value=REV_9M23),
        row(K23, "Revenues", FY22_END, 4, 2022, "FY", None, value=REV_FY22),
        row(Q3_23, "Revenues", date(2022, 6, 25), 3, 2022, "9M", None, value=REV_9M22),
        row(K23, "Revenues", FY22_END, 1, 2022, "Q4", "CY2022Q3", value="90000000000.0000"),
    ],
    derived=[
        ex(
            K23,
            "Revenues",
            FY23_END,
            1,
            2023,
            "Q4",
            "CY2023Q3",
            flags=["q4_derived"],
            value=Decimal(REV_Q4_23),
            derived_from=((FY23_END, 4, X), (date(2023, 7, 1), 3, Y)),
        ),
    ],
)

_q4_tie = scenario(
    "q4_tie_between_9m_candidates_goes_to_the_latest_filed",
    [
        # Two 9M facts qualify for the same FY (91 and 92 days before it): one ends 2023-07-01
        # (10-Q, filed 2023-08-04), one 2023-06-30 (10-Q/A, filed 2023-09-15, value restated).
        # They are different fact_keys, both retained; the tie goes to the latest filed.
        row(K23, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
        row(Q3_23, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value=REV_9M23),
        row(Q3_23A, "Revenues", date(2023, 6, 30), 3, 2023, "9M", None, value="293800000000.0000"),
    ],
    derived=[
        ex(
            K23,
            "Revenues",
            FY23_END,
            1,
            2023,
            "Q4",
            "CY2023Q3",
            flags=["q4_derived"],
            value=Decimal("89485000000.0000"),  # 383285000000 - 293800000000
            derived_from=((FY23_END, 4, X), (date(2023, 6, 30), 3, Q3_23A_ADSH)),
        ),
    ],
)

K23A_ADSH = "0000320193-24-000010"  # a 10-K/A for FY2023, filed between the two 10-Ks
K23A = Sub(APPLE, K23A_ADSH, "10-K/A", date(2024, 2, 1), "0930", 2023, date(2023, 9, 30))

_q4_middle_version = scenario(
    "q4_non_retained_middle_version_is_never_a_parent",
    [
        # Three versions of FY2023: the 10-K (original), a 10-K/A in between (381000M) and the 2024
        # 10-K (latest). `facts` keeps only the original and the latest, so the middle one is not
        # a retained parent: exactly two Q4 rows, none from the 10-K/A or pointing at it.
        row(K23, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
        row(K23A, "Revenues", FY23_END, 4, 2023, "FY", None, value="381000000000.0000"),
        row(K24, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23_RESTATED),
        row(Q3_23, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value=REV_9M23),
    ],
    derived=[
        ex(
            K23,
            "Revenues",
            FY23_END,
            1,
            2023,
            "Q4",
            "CY2023Q3",
            flags=["q4_derived"],
            value=Decimal(REV_Q4_23),
            derived_from=((FY23_END, 4, X), (date(2023, 7, 1), 3, Y)),
        ),
        ex(
            K24,
            "Revenues",
            FY23_END,
            1,
            2023,
            "Q4",
            "CY2023Q3",
            flags=["q4_derived"],
            value=Decimal(REV_Q4_23_LATEST),
            derived_from=((FY23_END, 4, K24_ADSH), (date(2023, 7, 1), 3, Y)),
        ),
    ],
)

Q4_SCENARIOS = [
    _q4_exact,
    _q4_rounded,
    _q4_two_years,
    _q4_retailer,
    _q4_restated_fy,
    _q4_comparatives,
    _q4_tie,
    _q4_middle_version,
    _q4_only_9m_restated,
    _q4_direct_blocks_only_its_year,
]

# --------------------------------------------------------------------------------------
# Scenarios: rule 5, NO derivation
# --------------------------------------------------------------------------------------

_REV_FY = K23.fact("Revenues", FY23_END, 4, REV_FY23)
_REV_9M = Q3_23.fact("Revenues", date(2023, 7, 1), 3, REV_9M23)

NO_DERIVE_SCENARIOS = [
    scenario(
        "no_q4_for_eps_per_share_uom",
        [  # uom contains '/' => per-share, not additive
            K23.fact("EarningsPerShareDiluted", FY23_END, 4, "6.1300", uom="USD/shares"),
            Q3_23.fact("EarningsPerShareDiluted", date(2023, 7, 1), 3, "4.6200", uom="USD/shares"),
        ],
    ),
    scenario(
        "no_q4_for_shares_uom",
        [  # weighted-average share counts are not additive: FY != sum of the quarters
            K23.fact(
                "WeightedAverageNumberOfSharesOutstandingBasic",
                FY23_END,
                4,
                "100.0000",
                uom="shares",
            ),
            Q3_23.fact(
                "WeightedAverageNumberOfSharesOutstandingBasic",
                date(2023, 7, 1),
                3,
                "98.0000",
                uom="shares",
            ),
        ],  # fmt: skip
    ),
    scenario(
        "no_q4_for_instants",
        [  # qtrs 0 is never FY - 9M
            K23.fact("Assets", FY23_END, 0, "352583000000.0000"),
            Q3_23.fact("Assets", date(2023, 7, 1), 0, "335038000000.0000"),
        ],
    ),
    scenario(
        "no_q4_for_pure_ratio",
        [  # uom 'pure' is a ratio, not additive
            K23.fact(
                "EffectiveIncomeTaxRateContinuingOperations", FY23_END, 4, "0.1470", uom="pure"
            ),
            Q3_23.fact(
                "EffectiveIncomeTaxRateContinuingOperations",
                date(2023, 7, 1),
                3,
                "0.1630",
                uom="pure",
            ),
        ],
    ),
    scenario("no_q4_fy_without_9m", [_REV_FY]),
    scenario(
        "no_q4_9m_without_fy",
        [_REV_9M, K23.fact("Assets", FY23_END, 0, "352583000000.0000")],  # 10-K anchors the cik
    ),
    scenario(
        "no_q4_when_uom_differs",
        [_REV_FY, Q3_23.fact("Revenues", date(2023, 7, 1), 3, REV_9M23, uom="EUR")],
    ),
    scenario(
        "no_q4_when_tag_differs",
        [_REV_FY, Q3_23.fact("SalesRevenueNet", date(2023, 7, 1), 3, REV_9M23)],
    ),
    scenario(
        "no_q4_when_taxonomy_differs",
        [_REV_FY, Q3_23.fact("Revenues", date(2023, 7, 1), 3, REV_9M23, taxonomy="custom")],
    ),
    scenario(
        "no_q4_when_cik_differs",
        [
            _REV_FY,
            OT_K.fact("Assets", date(2023, 9, 30), 0),  # the other company's 10-K
            OT_Q3.fact("Revenues", date(2023, 7, 1), 3, REV_9M23),
        ],
    ),
    scenario(
        "no_q4_from_a_prior_year_9m",
        # The only 9M ends 2022-06-25, 462 days before the FY end: not one quarter before it.
        [_REV_FY, Q3_23.fact("Revenues", date(2022, 6, 25), 3, REV_9M22)],
    ),
    scenario(
        "direct_q4_in_same_accession_wins",
        [
            row(K23, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
            row(Q3_23, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value=REV_9M23),
            # Directly reported Q4 (qtrs 1, same period_end, same accession as the FY): it stays
            # as is (value differs from FY - 9M on purpose) and nothing is derived.
            row(K23, "Revenues", FY23_END, 1, 2023, "Q4", "CY2023Q3", value="89500000000.0000"),
        ],
    ),
    scenario(
        "direct_q4_in_another_accession_wins",
        [
            row(K23, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
            row(Q3_23, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value=REV_9M23),
            # The direct Q4 only appears in the NEXT 10-K (as a comparative): still the same
            # (cik, tag, taxonomy, uom, fiscal_year), so nothing is derived for fiscal 2023.
            row(K24, "Revenues", FY23_END, 1, 2023, "Q4", "CY2023Q3", value="89500000000.0000"),
        ],
    ),
    scenario(
        "direct_q4_blocks_the_latest_tier_of_a_restated_fy_too",
        [
            row(K23, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
            row(K23, "Revenues", FY23_END, 1, 2023, "Q4", "CY2023Q3", value="89500000000.0000"),
            # FY2023 restated in the 2024 10-K: that would be a second tier, but a direct Q4
            # exists for the fiscal year, so no tier derives.
            row(K24, "Revenues", FY23_END, 4, 2023, "FY", None, value="380000000000.0000"),
            row(Q3_23, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value=REV_9M23),
        ],
    ),
    scenario(
        "direct_q4_with_another_period_end_spelling_wins",
        [
            row(K23, "Revenues", FY23_END, 4, 2023, "FY", None, value=REV_FY23),
            row(Q3_23, "Revenues", date(2023, 7, 1), 3, 2023, "9M", None, value=REV_9M23),
            # Keyed by fiscal year, not by an equal period_end: a Q4 ending 2023-09-24 is the same
            # fiscal 2023 Q4 as the FY ending 2023-09-30.
            row(
                K23,
                "Revenues",
                date(2023, 9, 24),
                1,
                2023,
                "Q4",
                "CY2023Q3",
                value="89400000000.0000",
            ),
        ],
    ),
]

# --------------------------------------------------------------------------------------
# Version selection (rule: "Dedup and restatements")
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpVersion:
    """One expected `curated.facts` row."""

    fact_key: int
    accession: str
    value: Decimal | None  # None: a version reported with a NULL value
    is_latest: bool
    is_original: bool
    n_versions: int
    is_restated: bool

    def as_tuple(self) -> tuple[int, str, Decimal | None, bool, bool, int, bool]:
        return (
            self.fact_key,
            self.accession,
            self.value,
            self.is_latest,
            self.is_original,
            self.n_versions,
            self.is_restated,
        )


@dataclass(frozen=True)
class VersionScenario:
    name: str
    versions: tuple[FactVersion, ...]
    expect: tuple[ExpVersion, ...]

    def __repr__(self) -> str:
        return self.name


def ver(
    key: int,
    n: int,
    accession: str,
    filed: date,
    filing_period: date | None,
    value: str | None,
    period_end: date,
    form: str = "10-K",
) -> FactVersion:
    """`value` None gives a NULL value; `filing_period` None a NULL sub.period (never native)."""
    return FactVersion(
        fact_key=key,
        fact_id=key * 100 + n,
        period_end=period_end,
        value=None if value is None else Decimal(value),
        accession=accession,
        form=form,
        filed=filed,
        is_amendment=form.endswith("/A"),
        filing_period=filing_period,  # type: ignore[arg-type]
    )


def one_row(key: int, v: FactVersion, n: int, restated: bool) -> ExpVersion:
    """The single row carrying both flags: always the ORIGINAL version `v` (the native filing)."""
    return ExpVersion(key, v.accession, v.value, True, True, n, restated)


def latest_row(key: int, v: FactVersion, n: int, restated: bool) -> ExpVersion:
    return ExpVersion(key, v.accession, v.value, True, False, n, restated)


def original_row(key: int, v: FactVersion, n: int, restated: bool) -> ExpVersion:
    return ExpVersion(key, v.accession, v.value, False, True, n, restated)


def _vscenario(name: str, versions: Sequence[FactVersion], expect: Sequence[ExpVersion]):
    return VersionScenario(name, tuple(versions), tuple(expect))


_A22 = "0000320193-22-000108"  # Apple FY2022 10-K
_C = acc(CAL, 22, 10)  # calendar company accessions used below
_C2 = acc(CAL, 23, 12)

_v1 = ver(1, 1, X, date(2023, 11, 3), date(2023, 9, 30), REV_FY23, FY23_END)
_v2a = ver(2, 1, _A22, date(2022, 10, 28), FY22_END, REV_FY22, FY22_END)
_v2b = ver(2, 2, X, date(2023, 11, 3), date(2023, 9, 30), REV_FY22, FY22_END)
_pe3 = date(2021, 12, 31)
_v3a = ver(3, 1, _C, date(2022, 2, 15), _pe3, "100.0000", _pe3)
_v3b = ver(3, 2, _C2, date(2023, 2, 14), date(2022, 12, 31), "90.0000", _pe3)
_pe4 = date(2022, 12, 31)
_v4a = ver(4, 1, acc(CAL, 23, 1), date(2023, 2, 15), _pe4, "500.0000", _pe4)
_v4b = ver(4, 2, acc(CAL, 23, 2), date(2023, 4, 20), _pe4, "480.0000", _pe4, "10-K/A")
_v5a = ver(5, 1, acc(CAL, 23, 1), date(2023, 2, 15), _pe4, "500.0000", _pe4)
_v5b = ver(5, 2, acc(CAL, 23, 2), date(2023, 4, 20), _pe4, "500.0000", _pe4, "10-K/A")
_pe6 = date(2022, 6, 30)  # a prior-year balance sheet only ever seen as a comparative
_v6a = ver(6, 1, acc(CAL, 23, 3), date(2023, 5, 5), date(2023, 3, 31), "5.0000", _pe6, "10-Q")
_v6b = ver(6, 2, acc(CAL, 23, 4), date(2023, 8, 4), date(2023, 6, 30), "6.0000", _pe6, "10-Q")
_v6c = ver(6, 3, acc(CAL, 23, 5), date(2023, 11, 3), date(2023, 9, 30), "7.0000", _pe6, "10-Q")
_pe7 = date(2023, 3, 31)
_v7a = ver(7, 1, acc(CAL, 23, 6), date(2023, 1, 10), date(2022, 12, 31), "80.0000", _pe7, "10-Q")
_v7b = ver(7, 2, acc(CAL, 23, 7), date(2023, 5, 5), _pe7, "100.0000", _pe7, "10-Q")
_v7c = ver(7, 3, acc(CAL, 23, 8), date(2023, 8, 4), date(2023, 6, 30), "100.0000", _pe7, "10-Q")
_v8a = ver(8, 1, acc(CAL, 23, 1), date(2023, 2, 15), _pe4, "10.0000", _pe4)
_v8b = ver(8, 2, acc(CAL, 23, 9), date(2023, 5, 1), date(2023, 3, 31), "20.0000", _pe4, "10-Q")
_v8c = ver(8, 3, acc(CAL, 23, 10), date(2023, 5, 1), date(2023, 3, 31), "30.0000", _pe4, "10-Q")
_v9a = ver(9, 1, acc(CAL, 23, 7), date(2023, 2, 15), _pe4, "10.0000", _pe4)
_v9b = ver(9, 2, acc(CAL, 23, 3), date(2023, 2, 15), _pe4, "11.0000", _pe4)
_v9c = ver(9, 3, acc(CAL, 23, 20), date(2023, 5, 1), date(2023, 3, 31), "12.0000", _pe4, "10-Q")
_v10a = ver(10, 1, acc(CAL, 23, 7), date(2023, 2, 15), date(2022, 12, 31), "1.0000", _pe6, "10-Q")
_v10b = ver(10, 2, acc(CAL, 23, 3), date(2023, 2, 15), date(2022, 12, 31), "2.0000", _pe6, "10-Q")
_v11a = ver(11, 1, acc(CAL, 23, 1), date(2023, 2, 15), _pe4, "100.0000", _pe4)
_v11b = ver(11, 2, acc(CAL, 23, 2), date(2023, 8, 1), date(2023, 6, 30), "90.0000", _pe4, "10-Q")
_v11c = ver(11, 3, acc(CAL, 24, 3), date(2024, 2, 15), date(2023, 12, 31), "100.0000", _pe4)
# NULL values (Python semantics: None is a value; None == None, None != Decimal)
_v12a = ver(12, 1, acc(CAL, 23, 1), date(2023, 2, 15), _pe4, None, _pe4)
_v12b = ver(12, 2, acc(CAL, 23, 3), date(2023, 5, 1), date(2023, 3, 31), "5.0000", _pe4, "10-Q")
_v13a = ver(13, 1, acc(CAL, 23, 1), date(2023, 2, 15), _pe4, None, _pe4)
_v13b = ver(13, 2, acc(CAL, 23, 3), date(2023, 5, 1), date(2023, 3, 31), None, _pe4, "10-Q")
_v14a = ver(14, 1, acc(CAL, 23, 1), date(2023, 2, 15), _pe4, None, _pe4)
_v14b = ver(14, 2, acc(CAL, 23, 3), date(2023, 5, 1), date(2023, 3, 31), "5.0000", _pe4, "10-Q")
_v14c = ver(14, 3, acc(CAL, 24, 3), date(2024, 2, 15), date(2023, 12, 31), None, _pe4)
# NULL filing_period (never 'native')
_v15a = ver(15, 1, acc(CAL, 23, 1), date(2023, 2, 1), None, "10.0000", _pe4, "10-Q")
_v15b = ver(15, 2, acc(CAL, 23, 3), date(2023, 5, 5), _pe4, "12.0000", _pe4)
_v16a = ver(16, 1, acc(CAL, 23, 1), date(2023, 2, 1), None, "10.0000", _pe4, "10-Q")
_v16b = ver(16, 2, acc(CAL, 23, 3), date(2023, 5, 5), None, "12.0000", _pe4, "10-Q")

VERSION_SCENARIOS = [
    _vscenario(
        "single_version",
        [_v1],
        [one_row(1, _v1, 1, False)],  # one version: both flags, never restated
    ),
    _vscenario(
        "same_value_reported_again_as_comparative",
        [_v2a, _v2b],
        # latest = the 2023 10-K (later filed), original = the 2022 10-K (filing_period ==
        # period_end); equal values => ONE row, carrying the ORIGINAL (native) filing's accession.
        [one_row(2, _v2a, 2, False)],
    ),
    _vscenario(
        "restated_100_to_90",
        [_v3a, _v3b],
        [latest_row(3, _v3b, 2, True), original_row(3, _v3a, 2, True)],
    ),
    _vscenario(
        "ten_k_then_ten_k_a_with_different_value",
        [_v4a, _v4b],
        # Both filings cover period_end; the original is the earlier filed (10-K), latest 10-K/A.
        [latest_row(4, _v4b, 2, True), original_row(4, _v4a, 2, True)],
    ),
    _vscenario(
        "ten_k_then_ten_k_a_with_same_value",
        [_v5a, _v5b],
        [
            one_row(5, _v5a, 2, False)
        ],  # same value => one row, carrying the original 10-K's accession
    ),
    _vscenario(
        "no_filing_period_matches_original_is_earliest_filed",
        [_v6b, _v6c, _v6a],  # deliberately not in filed order
        [latest_row(6, _v6c, 3, True), original_row(6, _v6a, 3, True)],
    ),
    _vscenario(
        "matching_filing_period_beats_an_earlier_filed_non_match",
        [_v7a, _v7b, _v7c],
        # original = _v7b (filing_period == period_end) = 100, NOT the earlier-filed _v7a (80).
        # latest = _v7c = 100, equal to the original => one row, carrying the original's (_v7b)
        # accession; 80 vs 100 => restated.
        [one_row(7, _v7b, 3, True)],
    ),
    _vscenario(
        "latest_tie_on_filed_takes_max_accession",
        [_v8a, _v8b, _v8c],
        # _v8b and _v8c are filed the same day; '...-000010' > '...-000009' as strings.
        [latest_row(8, _v8c, 3, True), original_row(8, _v8a, 3, True)],
    ),
    _vscenario(
        "original_tie_on_filed_takes_min_accession",
        [_v9a, _v9b, _v9c],
        # _v9a and _v9b both match filing_period and were filed the same day; min accession
        # is '...-000003' (_v9b).
        [latest_row(9, _v9c, 3, True), original_row(9, _v9b, 3, True)],
    ),
    _vscenario(
        "all_versions_filed_same_day_no_match",
        [_v10a, _v10b],
        # No filing_period matches; both filed together. latest = max accession (_v10a, '...7'),
        # original = min accession (_v10b, '...3').
        [latest_row(10, _v10a, 2, True), original_row(10, _v10b, 2, True)],
    ),
    _vscenario(
        "restated_then_restored_100_90_100",
        [_v11a, _v11b, _v11c],
        # original value 100 == latest value 100 => one row, carrying the original's (_v11a)
        # accession; three versions, two distinct values.
        [one_row(11, _v11a, 3, True)],
    ),
    _vscenario(
        "null_value_then_value_is_restated_and_not_collapsed",
        [_v12a, _v12b],
        # original = _v12a (native, value None), latest = _v12b (5.0000). None != 5 => TWO rows.
        # Distinct values {None, 5} => is_restated True (None counts as a value).
        [latest_row(12, _v12b, 2, True), original_row(12, _v12a, 2, True)],
    ),
    _vscenario(
        "null_value_twice_collapses_and_is_not_restated",
        [_v13a, _v13b],
        # latest _v13b (None) == original _v13a (None) => ONE row, original accession, both
        # flags. One distinct value {None} => is_restated False.
        [one_row(13, _v13a, 2, False)],
    ),
    _vscenario(
        "null_then_value_then_null_collapses_but_is_restated",
        [_v14a, _v14b, _v14c],
        # original _v14a (None) and latest _v14c (None) are equal => ONE row (original's
        # accession); the middle 5.0000 makes two distinct values {None, 5} => is_restated True.
        [one_row(14, _v14a, 3, True)],
    ),
    _vscenario(
        "null_filing_period_is_never_native",
        [_v15a, _v15b],
        # _v15a (filed first) has filing_period NULL: not native. The only native is _v15b, so
        # original = latest = _v15b (12.0000) => ONE row; 10 vs 12 => restated. A NULL treated
        # as native would pick _v15a as original and yield two rows.
        [one_row(15, _v15b, 2, True)],
    ),
    _vscenario(
        "all_filing_periods_null_original_is_earliest_filed",
        [_v16b, _v16a],
        # No native version (both NULL): fall back to the earliest filed overall (_v16a); latest
        # is _v16b. 10 != 12 => two rows.
        [latest_row(16, _v16b, 2, True), original_row(16, _v16a, 2, True)],
    ),
]


# --------------------------------------------------------------------------------------
# Checkers shared by the reference and the Spark tests
# --------------------------------------------------------------------------------------


def input_tuple_of(fact: FactInput) -> tuple[Any, ...]:
    return tuple(getattr(fact, k) for k in INPUT_FIELDS)


def check_fiscal_scenario(sc: FiscalScenario, rows: list[dict[str, Any]]) -> None:
    """Assert normalised output `rows` match the hand-derived expectations of `sc`.

    A row dict has the keys INPUT_FIELDS plus period_start, fiscal_year, fiscal_period,
    calendar_frame, dq_flags (frozenset) and derived_from (tuple of natural keys, or None).
    """
    assert len(rows) == len(sc.facts) + sc.n_derived, (
        f"{sc.name}: expected {len(sc.facts)} input rows + {sc.n_derived} derived, "
        f"got {len(rows)} rows"
    )
    originals = [r for r in rows if r["derived_from"] is None]
    derived = [r for r in rows if r["derived_from"] is not None]
    assert len(derived) == sc.n_derived, f"{sc.name}: {len(derived)} derived rows"
    assert Counter(tuple(r[k] for k in INPUT_FIELDS) for r in originals) == Counter(
        input_tuple_of(f) for f in sc.facts
    ), f"{sc.name}: the input rows must come through unchanged"
    for r in originals:
        assert "q4_derived" not in r["dq_flags"], f"{sc.name}: original row flagged q4_derived"
    for r in derived:
        assert r["fiscal_period"] == "Q4" and r["qtrs"] == 1, f"{sc.name}: derived row {r}"
        # form comes from the filing that supplies the FY figure (normally '10-K', but possibly a
        # 10-K/A or 10-KT), never hard-coded (contract section 2.2).
        fy_forms = {
            o["form"]
            for o in originals
            if (o["cik"], o["adsh"], o["tag"], o["uom"], o["period_end"], o["qtrs"])
            == (r["cik"], r["adsh"], r["tag"], r["uom"], r["period_end"], 4)
        }
        assert fy_forms == {r["form"]}, f"{sc.name}: derived form {r['form']} != FY form {fy_forms}"
        # The accession names one filing, so filed and sub_period must be that filing's too: version
        # selection ranks versions by filed and by sub_period == period_end.
        fy_stamp = {
            (o["filed"], o["sub_period"], o["sub_fy"], o["fye_mmdd"])
            for o in originals
            if (o["cik"], o["adsh"], o["tag"], o["uom"], o["period_end"], o["qtrs"])
            == (r["cik"], r["adsh"], r["tag"], r["uom"], r["period_end"], 4)
        }
        assert fy_stamp == {(r["filed"], r["sub_period"], r["sub_fy"], r["fye_mmdd"])}, (
            f"{sc.name}: derived row is not stamped with its FY parent's filing: {fy_stamp}"
        )
        assert "q4_derived" in r["dq_flags"], f"{sc.name}: derived row lacks q4_derived"
        assert len(r["derived_from"]) == 2

    for e in sc.expect:
        pool = derived if e.derived_from is not None else originals
        found = [
            r for r in pool if (r["cik"], r["adsh"], r["tag"], r["period_end"], r["qtrs"]) == e.key
        ]
        assert len(found) == 1, f"{sc.name}: expected exactly one row for {e.key}, got {found}"
        r = found[0]
        where = f"{sc.name} {e.key}"
        assert r["fiscal_year"] == e.fiscal_year, f"{where}: fiscal_year {r['fiscal_year']}"
        assert r["fiscal_period"] == e.fiscal_period, f"{where}: fiscal_period"
        if e.calendar_frame is not ANY:
            assert r["calendar_frame"] == e.calendar_frame, f"{where}: frame {r['calendar_frame']}"
        if e.derived_from is None:
            assert r["dq_flags"] == e.dq_flags, f"{where}: dq_flags {sorted(r['dq_flags'])}"
        else:
            assert e.dq_flags <= r["dq_flags"], f"{where}: dq_flags {sorted(r['dq_flags'])}"
            assert r["derived_from"] == e.derived_from, f"{where}: derived_from"
            fy_parent = next(
                f
                for f in sc.facts
                if (f.cik, f.adsh, f.tag, f.period_end, f.qtrs)
                == (e.sub.cik, e.derived_from[0][2], e.tag, e.derived_from[0][0], 4)
            )
            for k in ("uom", "taxonomy"):
                assert r[k] == getattr(fy_parent, k), f"{where}: {k} must equal the parents'"
        if e.value is not ANY:
            assert r["value"] == e.value, f"{where}: value {r['value']}"
        if e.period_start is not ANY:
            assert r["period_start"] == e.period_start, f"{where}: period_start"


def check_no_derivation(rows: list[dict[str, Any]], n_input: int) -> None:
    assert len(rows) == n_input, f"expected no derived row, got {len(rows) - n_input} extra"
    assert all(r["derived_from"] is None for r in rows)
    assert all("q4_derived" not in r["dq_flags"] for r in rows)


def sorted_version_tuples(
    rows: Sequence[tuple[int, str, Decimal | None, bool, bool, int, bool]],
) -> list[tuple[int, str, Decimal | None, bool, bool, int, bool]]:
    return sorted(rows, key=lambda t: (t[0], t[1], t[3], t[4]))
