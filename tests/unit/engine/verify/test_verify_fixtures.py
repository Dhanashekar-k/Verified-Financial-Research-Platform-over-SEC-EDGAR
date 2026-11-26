"""Stub-independent sanity checks of the verifier test inputs (``verify_cases.py``).

Unmarked on purpose: these call no verifier core and must pass today. They keep the xfail suites
honest, so that a "must pass" or "must fail" case really is what its docstring says it is.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.engine.answer import ChunkProvenance, ExprProvenance
from app.engine.compose.composer import growth_base
from app.engine.plan.catalog import dependencies
from tests.unit.engine.verify import verify_cases as vc


def evaluate(expr: str, case: vc.Case) -> Decimal:
    """A tiny evaluator for the fixtures' own expressions (not the verifier's)."""
    values = {f"f_{abs(i)}": f.value for i, f in case.evidence.facts.items() if i > 0}
    return eval(expr.replace("f:", "f_"), {"__builtins__": {}}, values)  # noqa: S307


NUMERIC_CASES = {
    "revenue": vc.revenue_case,
    "margin": vc.margin_case,
    "fallback_gross_profit": vc.fallback_gross_profit_case,
    "growth": vc.growth_case,
    "two_year_growth": vc.two_year_growth_case,
    "difference": lambda: vc.compare_case(2, "x"),
    "reverse_difference": lambda: vc.reverse_compare_case("x"),
    "derived_q4": vc.derived_q4_case,
    "quarter": vc.quarter_case,
    "eps": vc.eps_case,
    "assets": vc.instant_assets_case,
    "january_fye": vc.january_fye_case,
}


@pytest.mark.parametrize("name", NUMERIC_CASES)
def test_synthetic_result_is_what_its_facts_compute(name: str) -> None:
    """Each synthetic result's value equals its expression over its facts, and the claim copies
    the result, so a "correct claim" fixture is correct by construction."""
    case = NUMERIC_CASES[name]()
    provenance = case.claim.provenance
    assert isinstance(provenance, ExprProvenance)
    [res] = [r for r in case.ctx.results if r.expr == provenance.expr]
    assert set(res.fact_ids) == set(provenance.fact_ids) <= set(case.evidence.facts)
    assert evaluate(res.expr, case) == res.value == case.claim.value


def test_restated_fixture_latest_value_is_outside_the_relative_tolerance() -> None:
    """The original and restated figures differ by more than 0.5%, so a claim that mixes them
    up cannot pass on tolerance."""
    original = Decimal("383285000000")
    assert Decimal("390000000000") - original > original * Decimal("0.005")


def test_derived_q4_fixture_parents_subtract_to_the_stored_value() -> None:
    """The derived Q4 row equals first parent minus second parent."""
    facts = vc.derived_q4_case().evidence.facts
    assert facts[vc.Q4].value == facts[vc.Q4_FY].value - facts[vc.Q4_9M].value


def test_two_year_growth_base_is_not_the_previous_year() -> None:
    """The composer's own ``growth_base`` picks FY2021 for the two-period plan."""
    ctx = vc.two_year_growth_case().ctx
    assert growth_base(ctx.results[0], ctx.plan) == "FY2021"


def test_catalog_dependencies_cover_the_fallback_tags() -> None:
    """The gross-profit fallback operands are catalog dependencies of the metric."""
    assert {"revenue", "cost_of_revenue"} <= dependencies("gross_profit", vc.catalog())


MUST_PASS = [
    ("fb-00494", "c2"),
    ("fb-00746", "c1"),
    ("fb-00651", "c2"),
    ("fb-00724", "c1"),
    ("fb-03282", "c1"),
    ("fb-00651", "c1"),
    ("fb-00723", "c1"),
    ("fb-01328", "c2"),
    ("fb-01328", "c1"),
    ("fb-02419", "c1"),
]


@pytest.mark.parametrize(("question_id", "claim_id"), MUST_PASS)
def test_dev_claim_quote_is_verbatim_in_a_shown_allowed_chunk(
    question_id: str, claim_id: str
) -> None:
    """Each real dev claim used in the suites cites a shown chunk of an allowed company whose
    text holds the quote verbatim, so only the number rule can decide it."""
    case = vc.dev_case(question_id, claim_id)
    provenance = case.claim.provenance
    assert case.claim.kind == "text" and isinstance(provenance, ChunkProvenance)
    passage = case.evidence.chunks[provenance.chunk_id]
    assert provenance.quote in passage.text
    assert not case.ctx.allowed_ciks or passage.cik in case.ctx.allowed_ciks


@pytest.mark.parametrize(
    ("question_id", "claim_id", "in_claim", "absent_from_quote"),
    [
        ("fb-00494", "c2", "787", "787"),
        ("fb-00746", "c1", "12(b)", "12(b)"),
        ("fb-00651", "c2", "Q2 2023", "2023"),
        ("fb-00724", "c1", "Q2 2023", "2023"),
        ("fb-03282", "c1", "December 31, 2017", "2017"),
        ("fb-01328", "c1", "are 0.", "0"),
    ],
)
def test_dev_claim_has_the_token_its_test_is_about(
    question_id: str, claim_id: str, in_claim: str, absent_from_quote: str
) -> None:
    """The token each dev test is about is in the claim text and not in its quote."""
    claim = vc.dev_claim(question_id, claim_id)
    assert isinstance(claim.provenance, ChunkProvenance)
    assert in_claim in claim.text
    assert absent_from_quote not in claim.provenance.quote


SCALED = [
    ("fb-00438", "c2", "million"),
    ("fb-00517", "c1", "million"),
    ("fb-00517", "c2", "million"),
    ("fb-00517", "c3", "million"),
    ("fb-00711", "c1", "million"),
    ("fb-04171", "c1", "thousand"),
]


@pytest.mark.parametrize(("question_id", "claim_id", "scale"), SCALED)
def test_dev_scaled_claim_cites_a_passage_that_declares_its_scale(
    question_id: str, claim_id: str, scale: str
) -> None:
    """The six accept cases of decision E rest on real facts: the claim writes the scale, the
    quote does not, the shown passage declares it, and the company is allowed."""
    case = vc.dev_case(question_id, claim_id)
    provenance = case.claim.provenance
    assert isinstance(provenance, ChunkProvenance)
    passage = case.evidence.chunks[provenance.chunk_id]
    assert " ".join(provenance.quote.split()) in " ".join(passage.text.split())
    assert scale in case.claim.text and scale not in provenance.quote.lower()
    assert re.search(rf"\bin\s+{scale}s\b", passage.text, re.IGNORECASE)
    assert not case.ctx.allowed_ciks or passage.cik in case.ctx.allowed_ciks


def test_real_records_are_the_hard_case_for_company_names() -> None:
    """Decision A's premises: Microsoft's record has its filed name and no alias, tickers that
    are words are tracked, and NASDAQ is a tracked company."""
    companies = vc.dev_reference().companies
    microsoft = companies[vc.MICROSOFT]
    assert (microsoft.name, microsoft.aliases) == ("MICROSOFT CORP", [])
    tickers = {t for c in companies.values() for t in [c.ticker, *c.tickers] if t}
    assert {"A", "C", "ALL", "IT", "ON", "NOW", "MSFT"} <= tickers
    assert any(c.name == "NASDAQ, INC." for c in companies.values())


def test_num_0213_has_no_unit_on_a_metric_that_has_one() -> None:
    """The premise of the missing-unit pin: a USD metric, and a claim with no unit."""
    case = vc.dev_case("num-0213", "c1")
    assert case.claim.unit is None
    [res] = [r for r in case.ctx.results if r.expr == case.claim.provenance.expr]  # type: ignore[union-attr]
    assert (res.metric, res.operation) == ("capex", "difference")
    assert vc.catalog()["capex"].unit == "USD"
    assert {m.unit for m in vc.catalog().values()} == {"USD", "ratio", "USD/shares", "shares"}
