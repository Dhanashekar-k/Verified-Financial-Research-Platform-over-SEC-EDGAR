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

import calendar
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

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
    month, day = int(fye_mmdd[:2]), int(fye_mmdd[2:])
    if (month, day) == (2, 29):
        day = 28
    return date(year, month, day)


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def _period_start(period_end: date, qtrs: int) -> date | None:
    """Start of a qtrs-quarter period ending at period_end; None for an instant."""
    if qtrs == 0:
        return None
    return _add_months(period_end + timedelta(days=1), -3 * qtrs)


# the forms an FY (qtrs 4) parent may come from (ADR-022)
ANNUAL_FORMS = ("10-K", "10-KT", "10-K/A")


def _stable_key(f: FactInput) -> tuple[Any, ...]:
    """A total order over facts: accession, then value ascending with NULLs last, then period end,
    quarters and the series identity. `derive_fiscal` sorts its input by it, so every tie that
    `min`/`max` and `Counter.most_common` break by list position has exactly one answer."""
    return (
        f.adsh,
        f.value is None,
        f.value if f.value is not None else Decimal(0),
        f.period_end,
        f.qtrs,
        f.cik,
        f.tag,
        f.taxonomy,
        f.uom,
    )


def _retained(facts: Sequence[FactInput]) -> tuple[FactInput, FactInput]:
    """Version selection over one fact_key: (original, latest).
    latest = max(filed, accession); original = among sub_period == period_end the earliest filed
    (ties: smallest accession), else earliest overall. Equal values collapse to the original
    (so the derived Q4 cites the native filing — decision 2026-11-17)."""
    latest = max(facts, key=lambda f: (f.filed, f.adsh))
    native = [f for f in facts if f.sub_period == f.period_end]
    original = min(native or list(facts), key=lambda f: (f.filed, f.adsh))
    return (original, original) if latest.value == original.value else (original, latest)


def _retained_nine_months(facts: Sequence[FactInput]) -> tuple[FactInput, FactInput]:
    """Across all 9M candidates (possibly several period-end spellings), the original-most and
    the latest-filed retained rows; ties go to the latest/earliest filed, then accession."""
    by_pe: dict[date, list[FactInput]] = defaultdict(list)
    for f in facts:
        by_pe[f.period_end].append(f)
    origs, latests = zip(*(_retained(g) for g in by_pe.values()), strict=True)
    nm_orig = min(origs, key=lambda f: (f.filed, f.adsh))
    nm_latest = max(latests, key=lambda f: (f.filed, f.adsh))
    return nm_orig, nm_latest


def _derived_q4(
    fy: FactInput, nine_m: FactInput, fiscal_year: int, base_flags: tuple[str, ...]
) -> FiscalFact:
    assert fy.value is not None and nine_m.value is not None
    q4 = FactInput(
        cik=fy.cik,
        adsh=fy.adsh,
        tag=fy.tag,
        taxonomy=fy.taxonomy,
        uom=fy.uom,
        qtrs=1,
        period_end=fy.period_end,
        value=fy.value - nine_m.value,
        form=fy.form,
        filed=fy.filed,
        fye_mmdd=fy.fye_mmdd,
        sub_fy=fy.sub_fy,
        sub_period=fy.sub_period,
    )
    return FiscalFact(
        fact=q4,
        period_start=_period_start(fy.period_end, 1),
        fiscal_year=fiscal_year,
        fiscal_period="Q4",
        calendar_frame=calendar_frame(fy.period_end, 1),
        dq_flags=base_flags + ("q4_derived",),
        derived_from=((fy.period_end, 4, fy.adsh), (nine_m.period_end, 3, nine_m.adsh)),
    )


def y_end(period_end: date, fye_mmdd: str) -> int:
    """Rule 2: the smallest year y such that period_end <= fye_date(y) + 7 days.

    The 7-day tolerance absorbs 52/53-week years (Apple's FY2022 ended 2022-09-24 and FY2023
    ended 2023-09-30, both inside the window of a '0930' year end).
    """
    for y in (period_end.year - 1, period_end.year, period_end.year + 1):
        if period_end <= fye_date(y, fye_mmdd) + timedelta(days=7):
            return y
    raise ValueError(f"no fiscal year for {period_end!r} with FYE {fye_mmdd!r}")


def label_offsets(subs: Iterable[SubmissionPeriod]) -> dict[int, LabelOffset]:
    """Rule 3: per cik, the mode of (sub_fy - y_end(sub_period, fye_mmdd)) over its 10-K
    submissions (forms '10-K' and '10-KT'). `is_constant` is False if more than one distinct
    offset occurs. A cik with no usable 10-K submission is absent from the result.
    """
    by_cik: dict[int, list[int]] = defaultdict(list)
    for s in subs:
        if s.form in ("10-K", "10-KT") and s.sub_fy is not None:
            by_cik[s.cik].append(s.sub_fy - y_end(s.sub_period, s.fye_mmdd))
    return {
        cik: LabelOffset(Counter(offs).most_common(1)[0][0], len(set(offs)) == 1)
        for cik, offs in by_cik.items()
    }


def quarter_index(period_end: date, fye_mmdd: str) -> int:
    """Rule 4 helper: n in 1..4 = the number of whole quarters from fiscal-year start to
    `period_end`, with a tolerance for 52/53-week years. A period ending on the fiscal year end
    is quarter 4.
    """
    y = y_end(period_end, fye_mmdd)
    fy_end = fye_date(y, fye_mmdd)
    for n in (1, 2, 3):
        q_end = _add_months(fy_end, -3 * (4 - n))
        if period_end <= q_end + timedelta(days=7):
            return n
    return 4


def fiscal_period_label(period_end: date, qtrs: int, fye_mmdd: str) -> str:
    """Rule 4: qtrs 4 -> 'FY'; 1 -> 'Q{n}'; 2 -> 'H1'; 3 -> '9M'.
    An instant (qtrs 0) is 'FY' when its quarter_index is 4, else 'Q{n}'.
    """
    if qtrs == 4:
        return "FY"
    if qtrs == 3:
        return "9M"
    if qtrs == 2:
        return "H1"
    n = quarter_index(period_end, fye_mmdd)
    if qtrs == 0 and n == 4:
        return "FY"  # an instant at the fiscal year-end (e.g. total assets)
    return f"Q{n}"  # a quarter (incl. a direct Q4), or an instant at Q1–Q3


def calendar_frame(period_end: date, qtrs: int) -> str | None:
    """Rule 6: 'CY{yyyy}' for about-12-month periods ending within +-30 days of Dec 31 (the
    year is that of the nearest Dec 31); 'CY{yyyy}Q{n}' for quarters, by the calendar quarter
    of `period_end`; a trailing 'I' for instants. None where no frame applies (for example H1
    and 9M, and annual periods not aligned to the calendar year).
    """
    if qtrs == 4:  # annual: CY{year} only if it ends within ±30 days of a Dec 31
        nearest = min(
            (date(period_end.year - 1, 12, 31), date(period_end.year, 12, 31)),
            key=lambda c: abs((period_end - c).days),
        )
        if abs((period_end - nearest).days) <= 30:
            return f"CY{nearest.year}"
        return None
    if qtrs in (0, 1):  # quarter / instant: by the calendar quarter of period_end
        q = (period_end.month - 1) // 3 + 1
        frame = f"CY{period_end.year}Q{q}"
        return frame + "I" if qtrs == 0 else frame
    return None  # H1 (2) and 9M (3) never get a frame


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
    An FY parent must come from a 10-K-family filing (`ANNUAL_FORMS`); the 9M parent may have
    any form. Input order does not matter: the input is sorted by `_stable_key`. Output order is
    unspecified.
    """
    facts = sorted(facts, key=_stable_key)  # ADR-022: the output must not depend on input order
    subs = {
        (f.cik, f.adsh): SubmissionPeriod(f.cik, f.adsh, f.form, f.sub_fy, f.sub_period, f.fye_mmdd)
        for f in facts
    }
    offsets = label_offsets(subs.values())

    def fiscal_year_of(f: FactInput) -> int:
        off = offsets[f.cik].offset if f.cik in offsets else 0
        return y_end(f.period_end, f.fye_mmdd) + off

    def flags_of(f: FactInput) -> tuple[str, ...]:
        lo = offsets.get(f.cik)
        return ("label_offset_varies",) if lo is not None and not lo.is_constant else ()

    out: list[FiscalFact] = [
        FiscalFact(
            fact=f,
            period_start=_period_start(f.period_end, f.qtrs),
            fiscal_year=fiscal_year_of(f),
            fiscal_period=fiscal_period_label(f.period_end, f.qtrs, f.fye_mmdd),
            calendar_frame=calendar_frame(f.period_end, f.qtrs),
            dq_flags=flags_of(f),
            derived_from=None,
        )
        for f in facts
    ]

    groups: dict[tuple[Any, ...], list[FactInput]] = defaultdict(list)
    for f in facts:
        groups[(f.cik, f.tag, f.taxonomy, f.uom, fiscal_year_of(f))].append(f)

    for (_cik, _tag, _tax, uom, fy), members in groups.items():
        if "/" in uom or uom in ("pure", "shares"):
            continue  # per-share / ratio / share-count concepts are not additive
        if any(
            m.qtrs == 1 and fiscal_period_label(m.period_end, 1, m.fye_mmdd) == "Q4"
            for m in members
        ):
            continue  # a directly reported Q4 wins: derive nothing for this fiscal year
        fy_rows = [m for m in members if m.qtrs == 4 and m.form in ANNUAL_FORMS]
        nm_rows = [m for m in members if m.qtrs == 3]
        if not fy_rows or not nm_rows:
            continue
        fy_orig, fy_latest = _retained(fy_rows)
        nm_orig, nm_latest = _retained_nine_months(nm_rows)
        base = flags_of(fy_orig)
        # one Q4 per provenance tier; tiers sharing an FY accession collapse to the later 9M
        chosen: dict[str, tuple[FactInput, FactInput]] = {}
        for fyv, nmv in ((fy_orig, nm_orig), (fy_latest, nm_latest)):
            kept = chosen.get(fyv.adsh)
            if kept is None or (nmv.filed, nmv.adsh) > (kept[1].filed, kept[1].adsh):
                chosen[fyv.adsh] = (fyv, nmv)
        out.extend(_derived_q4(fyv, nmv, fy, base) for fyv, nmv in chosen.values())
    return out


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
    groups: dict[int, list[FactVersion]] = defaultdict(list)
    for v in versions:
        groups[v.fact_key].append(v)
    out: list[SelectedVersion] = []
    for group in groups.values():
        latest = max(group, key=lambda v: (v.filed, v.accession))
        native = [v for v in group if v.filing_period == v.period_end]
        original = min(native or group, key=lambda v: (v.filed, v.accession))
        n, restated = len(group), len({v.value for v in group}) > 1
        if latest.value == original.value:
            out.append(SelectedVersion(original, True, True, n, restated))
        else:
            out.append(SelectedVersion(latest, True, False, n, restated))
            out.append(SelectedVersion(original, False, True, n, restated))
    return out
