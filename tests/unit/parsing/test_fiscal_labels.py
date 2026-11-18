"""Tests for ``pipelines/parsing/fiscal_labels.py`` (ADR-023; contract section 2.2 filings).

The agreement test compares a filing's labels with ``derive_fiscal``'s labels for the facts of the
REPORTING PERIOD only. A filing's facts span several fiscal years by design (a FY2023 10-K reports
FY2022 and FY2021 comparatives), so comparing against every fact would fail on any real filing.
The test also asserts that comparatives DO derive to different years, so a constant labeller
cannot satisfy it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from pipelines.common.fiscal_ref import (
    FactInput,
    LabelOffset,
    SubmissionPeriod,
    derive_fiscal,
    label_offsets,
)
from pipelines.parsing.fiscal_labels import filing_fiscal_labels

D = date


def labels(**kw):
    base = {
        "form": "10-K",
        "period_of_report": D(2023, 9, 30),
        "fye_mmdd": "0930",
        "fye_from_companies": False,
        "offset": LabelOffset(0, True),
        "sub_fy": None,
        "sub_fp": None,
    }
    return filing_fiscal_labels(**(base | kw))


# --------------------------------------------------------------------------------------
# direct cases
# --------------------------------------------------------------------------------------


def test_a_10k_is_fy_of_the_year_its_period_ends_in_plus_the_offset():
    out = labels()

    assert (out.fiscal_year, out.fiscal_period, out.dq_flags) == (2023, "FY", ())


def test_a_negative_label_offset_names_a_january_year_end_for_the_year_it_began():
    out = labels(period_of_report=D(2024, 2, 3), fye_mmdd="0131", offset=LabelOffset(-1, True))

    assert (out.fiscal_year, out.fiscal_period) == (2023, "FY")


@pytest.mark.parametrize(
    ("period", "expected"),
    [(D(2023, 1, 1), "Q1"), (D(2023, 4, 1), "Q2"), (D(2023, 7, 1), "Q3")],
)
def test_a_10q_gets_the_quarter_label_and_the_fiscal_year_it_belongs_to(
    period: date, expected: str
):
    out = labels(form="10-Q", period_of_report=period)

    assert (out.fiscal_year, out.fiscal_period) == (2023, expected)


def test_amendments_label_like_their_originals():
    assert labels(form="10-K/A").fiscal_period == "FY"
    assert labels(form="10-Q/A", period_of_report=D(2023, 4, 1)).fiscal_period == "Q2"


def test_unsupported_forms_are_rejected():
    with pytest.raises(ValueError, match="form"):
        labels(form="8-K")


def test_a_company_with_no_offset_gets_zero_and_a_flag():
    out = labels(offset=None)

    assert out.fiscal_year == 2023
    assert out.dq_flags == ("label_offset_default",)


def test_an_inconstant_offset_is_flagged_like_in_facts():
    out = labels(offset=LabelOffset(0, False))

    assert out.dq_flags == ("label_offset_varies",)


def test_fye_taken_from_companies_is_flagged():
    assert labels(fye_from_companies=True).dq_flags == ("fye_from_companies",)


@pytest.mark.parametrize("missing", [{"period_of_report": None}, {"fye_mmdd": None}])
def test_when_the_derivation_is_impossible_the_fsds_sub_labels_are_used_and_flagged(missing):
    out = labels(sub_fy=2023, sub_fp="FY", **missing)

    assert (out.fiscal_year, out.fiscal_period) == (2023, "FY")
    assert out.dq_flags == ("fiscal_from_fsds_sub",)


def test_with_neither_source_the_labels_are_null_and_flagged():
    out = labels(period_of_report=None)

    assert (out.fiscal_year, out.fiscal_period) == (None, None)
    assert out.dq_flags == ("fiscal_unresolved",)


def test_a_sub_fy_without_a_sub_fp_is_not_enough_for_the_fallback():
    out = labels(period_of_report=None, sub_fy=2023, sub_fp=None)

    assert out.dq_flags == ("fiscal_unresolved",)


def test_an_impossible_fye_falls_back_instead_of_raising():
    out = labels(fye_mmdd="0231x", sub_fy=2022, sub_fp="FY")

    assert out.dq_flags == ("fiscal_from_fsds_sub",)


def test_disagreement_with_fsds_sub_is_flagged_and_the_derived_labels_win():
    out = labels(sub_fy=2024, sub_fp="FY")

    assert (out.fiscal_year, out.fiscal_period) == (2023, "FY")
    assert out.dq_flags == ("fiscal_differs_from_fsds_sub",)


def test_agreement_with_fsds_sub_adds_no_flag():
    assert labels(sub_fy=2023, sub_fp="FY").dq_flags == ()


# --------------------------------------------------------------------------------------
# text and facts agree (the point of ADR-023)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Company:
    cik: int
    fye: str
    label_offset: int  # how the company names its fiscal year relative to y_end
    ends: list[date]  # the three annual period ends; 10-Q ends are derived below


COMPANIES = [
    Company(320193, "0930", 0, [D(2021, 9, 25), D(2022, 9, 24), D(2023, 9, 30)]),
    Company(789019, "0630", 0, [D(2021, 6, 30), D(2022, 6, 30), D(2023, 6, 30)]),
    Company(1000001, "1231", 0, [D(2021, 12, 31), D(2022, 12, 31), D(2023, 12, 31)]),
    # a retailer: year ends in early February and is named for the year it BEGAN
    Company(1000002, "0131", -1, [D(2022, 1, 29), D(2023, 1, 28), D(2024, 2, 3)]),
]


def fact(c: Company, adsh: str, form: str, period: date, qtrs: int, end: date, sub_fy: int):
    return FactInput(
        cik=c.cik, adsh=adsh, tag="Revenues", taxonomy="us-gaap", uom="USD", qtrs=qtrs,
        period_end=end, value=Decimal(1), form=form, filed=period, fye_mmdd=c.fye,
        sub_fy=sub_fy, sub_period=period,
    )  # fmt: skip


def build(c: Company):
    """Facts for three 10-Ks and one 10-Q per year; each filing also carries comparatives."""
    facts: list[FactInput] = []
    filings: list[tuple[str, str, date]] = []
    for n, end in enumerate(c.ends):
        sub_fy = end.year + c.label_offset
        adsh = f"{c.cik:010d}-{end.year % 100:02d}-0000{n}K"
        filings.append((adsh, "10-K", end))
        facts.append(fact(c, adsh, "10-K", end, 4, end, sub_fy))  # reporting-period flow
        facts.append(fact(c, adsh, "10-K", end, 0, end, sub_fy))  # reporting-period instant
        if n > 0:  # comparatives: the previous fiscal year's flow and instant
            prior = c.ends[n - 1]
            facts.append(fact(c, adsh, "10-K", end, 4, prior, sub_fy))
            facts.append(fact(c, adsh, "10-K", end, 0, prior, sub_fy))
    for n in range(1, len(c.ends)):  # a Q1 10-Q a quarter after each prior year end
        q1_end = _months_after(c.ends[n - 1], 3)
        adsh = f"{c.cik:010d}-{q1_end.year % 100:02d}-0000Q"
        filings.append((adsh, "10-Q", q1_end))
        sub_fy = c.ends[n].year + c.label_offset
        facts.append(fact(c, adsh, "10-Q", q1_end, 1, q1_end, sub_fy))
        facts.append(fact(c, adsh, "10-Q", q1_end, 0, q1_end, sub_fy))
        facts.append(fact(c, adsh, "10-Q", q1_end, 1, _months_after(q1_end, -12), sub_fy))
    return facts, filings


def _months_after(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    day = min(
        d.day, [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month]
    )
    return date(year, month + 1, day)


@pytest.mark.parametrize("company", COMPANIES, ids=lambda c: str(c.cik))
def test_a_filings_labels_equal_derive_fiscal_for_its_reporting_period_facts(company: Company):
    facts, filings = build(company)
    derived = {id(f.fact): f for f in derive_fiscal(facts) if f.derived_from is None}
    by_input = {id(f): derived[id(f)] for f in facts}
    subs = [
        SubmissionPeriod(f.cik, f.adsh, f.form, f.sub_fy, f.sub_period, f.fye_mmdd)
        for f in facts
        if f.form == "10-K"
    ]
    offset = label_offsets(subs)[company.cik]
    checked = comparatives_differing = 0

    for adsh, form, period in filings:
        own = [f for f in facts if f.adsh == adsh]
        out = filing_fiscal_labels(
            form=form, period_of_report=period, fye_mmdd=company.fye, fye_from_companies=False,
            offset=offset, sub_fy=None, sub_fp=None,
        )  # fmt: skip
        flow_qtrs = 4 if form == "10-K" else 1
        primary = [f for f in own if f.period_end == period and f.qtrs in (flow_qtrs, 0)]
        comparatives = [f for f in own if f.period_end < period]
        assert primary, "no reporting-period facts: the test would be vacuous"
        for f in primary:
            got = by_input[id(f)]
            assert (got.fiscal_year, got.fiscal_period) == (out.fiscal_year, out.fiscal_period)
            checked += 1
        comparatives_differing += sum(
            by_input[id(f)].fiscal_year != out.fiscal_year
            for f in comparatives
            if f.qtrs == 4 and form == "10-K"
        )

    assert checked >= 6
    assert comparatives_differing >= 2, (
        "comparatives must derive to other years, or the test is blind"
    )


def test_the_retailer_fixture_really_exercises_a_nonzero_offset():
    facts, _ = build(COMPANIES[3])
    subs = [
        SubmissionPeriod(f.cik, f.adsh, f.form, f.sub_fy, f.sub_period, f.fye_mmdd)
        for f in facts
        if f.form == "10-K"
    ]

    assert label_offsets(subs)[1000002] == LabelOffset(-1, True)
