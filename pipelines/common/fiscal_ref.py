"""Pure-Python reference for the fiscal-period and version-selection rules (M01 section 3.5).

OWNED BY SHEKAR. Claude created this stub once and will not edit it again (CLAUDE.md rule 3;
`.claude/hooks/protect-paths.sh` blocks edits). Everything below raises NotImplementedError.

Two jobs:

1. It is the *specification in code*: `tests/unit/test_fiscal.py` and `test_versions.py` test
   these functions directly, with no Spark, so the rules are checked in milliseconds.
2. It is the oracle for `tests/integration/test_differential.py`, which asserts that the Spark
   functions (`pipelines/spark/fiscal.py`, `pipelines/spark/versions.py`) agree with it row for
   row. It is also reused by the M10 companyfacts Lambda, so it must not import Spark, pandas
   or anything that is not in the standard library.

The interface below is Claude's proposal. Change a signature if you like, then tell the tests'
owner (the test-writer subagent) or edit the tests yourself; the rules, not the names, are the
contract.

Rule numbers refer to M01 section 3.5 "Fiscal-period rules". Column names match
`docs/plan/05_data_contracts.md` section 2.2.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# --------------------------------------------------------------------------------------
# Inputs and outputs
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SubmissionPeriod:
    """The part of one `sub` row that rule 3 (label offset) needs."""

    cik: int
    adsh: str
    form: str  # '10-K', '10-K/A', '10-KT', '10-Q', ...
    sub_fy: int | None  # sub.fy: how the company labels this filing's fiscal year
    sub_period: date  # sub.period: the balance-sheet date of this filing
    fye_mmdd: str  # sub.fye of THIS submission, 'MMDD' (rule 1)


@dataclass(frozen=True)
class LabelOffset:
    """Rule 3 result for one company."""

    offset: int  # mode(sub_fy - y_end(sub_period)) over the company's 10-K submissions
    is_constant: bool  # False if the company has more than one distinct offset (flag it)


@dataclass(frozen=True)
class FactInput:
    """One consolidated fact joined to its submission: the input of `derive_fiscal`."""

    cik: int
    adsh: str
    tag: str
    taxonomy: str  # year-stripped family, or 'custom'
    uom: str
    qtrs: int  # 0 instant, 1 quarter, 2 H1, 3 9M, 4 FY
    period_end: date  # to_date(ddate)
    value: Decimal | None
    form: str
    filed: date
    fye_mmdd: str  # from the sub row of the SAME submission (rule 1)
    sub_fy: int | None
    sub_period: date


@dataclass(frozen=True)
class FiscalFact:
    """A `FactInput` plus the derived columns. Derived Q4 rows have `derived_from` set."""

    fact: FactInput
    period_start: date | None  # approximate: period_end - qtrs quarters + 1 day (instants: None)
    fiscal_year: int  # y_end(period_end, fye_mmdd) + label_offset(cik)
    fiscal_period: str  # 'FY' | 'Q1'..'Q4' | 'H1' | '9M' | 'INSTANT'
    calendar_frame: str | None  # 'CY2023' | 'CY2023Q3' | 'CY2023Q3I' | None (rule 6)
    dq_flags: tuple[str, ...]  # e.g. ('q4_derived',), ('label_offset_varies',)
    derived_from: tuple[tuple[date, int, str], ...] | None
    # derived Q4 only: ((FY period_end, 4, FY accession), (9M period_end, 3, 9M accession)),
    # i.e. the natural keys of the two parents. Spark turns these into fact_ids.


# --------------------------------------------------------------------------------------
# Rules 1-4 and 6: one function per rule
# --------------------------------------------------------------------------------------


def fye_date(year: int, fye_mmdd: str) -> date:
    """The fiscal-year-end date falling in calendar `year` (rule 1). Feb 29 becomes Feb 28."""
    raise NotImplementedError


def y_end(period_end: date, fye_mmdd: str) -> int:
    """Rule 2: the smallest year y such that period_end <= fye_date(y) + 7 days.

    The 7-day tolerance absorbs 52/53-week years (Apple's FY2022 ended 2022-09-24 and FY2023
    ended 2023-09-30, both inside the window of a '0930' year end).
    """
    raise NotImplementedError


def label_offsets(subs: Iterable[SubmissionPeriod]) -> dict[int, LabelOffset]:
    """Rule 3: per cik, the mode of (sub_fy - y_end(sub_period, fye_mmdd)) over its 10-K
    submissions (forms '10-K' and '10-KT'). `is_constant` is False if more than one distinct
    offset occurs. A cik with no usable 10-K submission is absent from the result.
    """
    raise NotImplementedError


def quarter_index(period_end: date, fye_mmdd: str) -> int:
    """Rule 4 helper: n in 1..4 = the number of whole quarters from fiscal-year start to
    `period_end`, with a tolerance for 52/53-week years. A period ending on the fiscal year end
    is quarter 4.
    """
    raise NotImplementedError


def fiscal_period_label(period_end: date, qtrs: int, fye_mmdd: str) -> str:
    """Rule 4: qtrs 4 -> 'FY'; 0 -> 'INSTANT'; 1 -> 'Q{n}'; 2 -> 'H1'; 3 -> '9M'."""
    raise NotImplementedError


def calendar_frame(period_end: date, qtrs: int) -> str | None:
    """Rule 6: 'CY{yyyy}' for about-12-month periods ending within +-30 days of Dec 31 (the
    year is that of the nearest Dec 31); 'CY{yyyy}Q{n}' for quarters, by the calendar quarter
    of `period_end`; a trailing 'I' for instants. None where no frame applies (for example H1
    and 9M, and annual periods not aligned to the calendar year).
    """
    raise NotImplementedError


# --------------------------------------------------------------------------------------
# Rules 1-6 over a whole set of facts (the oracle for the Spark function)
# --------------------------------------------------------------------------------------


def derive_fiscal(facts: Sequence[FactInput]) -> list[FiscalFact]:
    """Apply rules 1-6 to a set of consolidated facts and return one `FiscalFact` per input,
    plus one derived row per derivable Q4 (rule 5).

    Rule 5: Q4 = FY - 9M for flow concepts (qtrs 4 minus qtrs 3 for the same cik, tag,
    taxonomy, uom, with the 9M ending one quarter before the FY). The derived row has
    fiscal_period 'Q4', qtrs 1, period_end = the FY end, form '10-K', the FY row's accession,
    dq_flags containing 'q4_derived', and `derived_from` naming both parents. Never derive Q4
    for instants (qtrs 0) or per-share and ratio concepts (EPS is not additive): uom containing
    '/' (for example 'USD/shares') or equal to 'pure' is not derived.
    Input order does not matter; output order is unspecified.
    """
    raise NotImplementedError


# --------------------------------------------------------------------------------------
# Versions (reference twin of pipelines/spark/versions.py)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FactVersion:
    """One reported version of a fact: a `facts_history` row (only the columns that matter)."""

    fact_key: int  # opaque here: the grouping key of versions of the same fact
    fact_id: int  # unique per (fact_key, accession)
    period_end: date
    value: Decimal | None
    accession: str
    form: str
    filed: date
    is_amendment: bool
    filing_period: date  # sub.period of the accession: the filing's OWN reporting period


@dataclass(frozen=True)
class SelectedVersion:
    version: FactVersion
    is_latest: bool
    is_original: bool
    n_versions: int  # number of distinct versions (rows) in the group
    is_restated: bool  # more than one DISTINCT value across versions


def select_versions_ref(versions: Sequence[FactVersion]) -> list[SelectedVersion]:
    """Per fact_key: pick the latest and the original version.

    Latest = max(filed), ties broken by max(accession).
    Original = among versions whose `filing_period == period_end` (the filing whose own
    reporting period covers the fact), the earliest filed (ties: smallest accession); if no
    version matches, the earliest filed overall (ties: smallest accession).
    Output: one row per fact_key flagged is_latest and is_original when they have the same
    value (never restated); otherwise two rows, the latest (is_latest only) and the original
    (is_original only). Every output row carries n_versions and is_restated of its group.
    """
    raise NotImplementedError
