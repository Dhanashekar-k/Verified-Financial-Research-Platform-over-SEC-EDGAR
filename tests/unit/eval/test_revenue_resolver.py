"""Revenue / cost-of-revenue resolver (ADR-043): never the first populated tag; abstain when
candidates disagree materially and nothing reconciles them."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from eval.gen.numeric_policy import Versioned, select_versions
from eval.gen.revenue_resolver import gross_profit_check, resolve
from tests.unit.eval.test_numeric_policy import fact

REV = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"]
COST = ["CostOfRevenue", "CostOfGoodsAndServicesSold"]


def cand(tag: str, value: int, fact_id: int = 1, **kw: object) -> Versioned:
    (v,) = select_versions([fact(fact_id=fact_id, tag=tag, value=Decimal(value), **kw)])
    return v


def test_single_candidate_selected() -> None:
    r = resolve("revenue", REV, [cand("Revenues", 5_000_000_000)])
    assert r.ok and r.reason == "single_candidate" and r.tag == "Revenues"


def test_agreeing_candidates_cite_the_preferred_tag() -> None:
    r = resolve("revenue", REV, [cand("Revenues", 5_000_000_000, 2), cand(REV[0], 5_000_000_000)])
    assert r.reason == "candidates_agree" and r.tag == REV[0]


def test_unsettled_revenue_conflict_takes_revenues_as_top_line() -> None:
    """ADR-043 option (a): UDR-like, contract revenue $8.3M is a component of $1.67B."""
    r = resolve("revenue", REV, [cand(REV[0], 8_317_000), cand("Revenues", 1_671_842_000, 2)])
    assert r.ok and r.reason == "total_tag_is_top_line" and r.tag == "Revenues"
    assert {c.tag for c in r.candidates} == set(REV)


def test_revenues_below_contract_revenue_abstains() -> None:
    """The guard: a total below its component is not a top line."""
    r = resolve("revenue", REV, [cand(REV[0], 79_800_000_000), cand("Revenues", 29_600_000_000, 2)])
    assert not r.ok and r.reason == "total_below_component"


@pytest.mark.parametrize("gp", [None, Decimal(29_000_000_000)])
def test_below_component_guard_beats_presentation_and_identity(gp: Decimal | None) -> None:
    """Revenues 29.6B under contract revenue 79.8B abstains even when only Revenues is
    presented and even when the identity (gp 29.0B with cost 0.6B) would close on Revenues."""
    check = gross_profit_check(gp, [Decimal(600_000_000)], role="revenue")
    r = resolve(
        "revenue",
        REV,
        [cand(REV[0], 79_800_000_000), cand("Revenues", 29_600_000_000, 2)],
        presented_tags=frozenset({"Revenues"}),
        reconciles=check,
    )
    assert not r.ok and r.reason == "total_below_component"


def test_identity_contradicting_revenues_abstains() -> None:
    """Confirming check: gross profit 1.0B with cost 3.0B implies revenue 4.0B; neither the
    contract figure (3.5B) nor Revenues (4.4B) closes it, so Revenues is not taken."""
    check = gross_profit_check(Decimal(1_000_000_000), [Decimal(3_000_000_000)], role="revenue")
    r = resolve(
        "revenue",
        REV,
        [cand(REV[0], 3_500_000_000), cand("Revenues", 4_400_000_000, 2)],
        reconciles=check,
    )
    assert not r.ok and r.reason == "total_fails_gross_profit_identity"


def test_identity_that_confirms_revenues_still_resolves_by_identity() -> None:
    check = gross_profit_check(Decimal(1_400_000_000), [Decimal(3_000_000_000)], role="revenue")
    r = resolve(
        "revenue",
        REV,
        [cand(REV[0], 3_500_000_000), cand("Revenues", 4_400_000_000, 2)],
        reconciles=check,
    )
    assert r.reason == "reconciles_with_gross_profit" and r.tag == "Revenues"


def test_identity_beats_the_top_line_rule() -> None:
    """The identity picks contract revenue: Revenues is not taken even though it is larger."""
    check = gross_profit_check(Decimal(500_000_000), [Decimal(3_000_000_000)], role="revenue")
    r = resolve(
        "revenue",
        REV,
        [cand(REV[0], 3_500_000_000), cand("Revenues", 4_400_000_000, 2)],
        reconciles=check,
    )
    assert r.reason == "reconciles_with_gross_profit" and r.tag == REV[0]


def test_top_line_rule_is_scoped_to_revenue() -> None:
    """Cost of revenue has no total-over-component rule: an unsettled conflict abstains."""
    cost = ["CostOfRevenue", "CostOfGoodsAndServicesSold"]
    r = resolve(
        "cost_of_revenue",
        cost,
        [cand("CostOfRevenue", 6_760_000_000), cand("CostOfGoodsAndServicesSold", 143_900_000, 2)],
    )
    assert not r.ok and r.reason == "conflicting_candidates"


def test_income_statement_presentation_decides() -> None:
    r = resolve(
        "revenue",
        REV,
        [cand(REV[0], 4_000_000_000), cand("Revenues", 4_400_000_000, 2)],
        presented_tags=frozenset({"Revenues"}),
    )
    assert r.reason == "presented_on_income_statement" and r.tag == "Revenues"
    assert [c.presented for c in r.candidates] == [False, True]


def test_presented_member_of_an_agreeing_group_is_cited() -> None:
    """Chain [A, B, C]: A and B agree at 4.4B, C says 4.0B, only B is presented. The 4.4B
    group wins on presentation and the citation is B (presented), not A (chain-first)."""
    chain = ["A", "B", "C"]
    r = resolve(
        "revenue",
        chain,
        [cand("A", 4_400_000_000), cand("B", 4_400_000_000, 2), cand("C", 4_000_000_000, 3)],
        presented_tags=frozenset({"B"}),
    )
    assert r.reason == "presented_on_income_statement" and r.tag == "B"


def test_both_presented_and_different_falls_through_to_calculation() -> None:
    gp = gross_profit_check(Decimal(1_000_000_000), [Decimal(3_000_000_000)], role="revenue")
    r = resolve(
        "revenue",
        REV,
        [cand(REV[0], 4_000_000_000), cand("Revenues", 4_400_000_000, 2)],
        presented_tags=frozenset(REV),
        reconciles=gp,
    )
    assert r.reason == "reconciles_with_gross_profit" and r.tag == REV[0]


def test_block_cost_of_revenue_resolved_by_gross_profit_identity() -> None:
    """Block: CostOfRevenue $6.76B vs CostOfGoodsAndServicesSold $143.9M (a component)."""
    revenue, gp = Decimal(17_530_000_000), Decimal(10_770_000_000)
    check = gross_profit_check(gp, [revenue], role="cost")
    r = resolve(
        "cost_of_revenue",
        COST,
        [cand("CostOfGoodsAndServicesSold", 143_900_000, 2), cand("CostOfRevenue", 6_760_000_000)],
        reconciles=check,
    )
    assert r.reason == "reconciles_with_gross_profit" and r.tag == "CostOfRevenue"


def test_presentation_contradicted_by_calculation_abstains() -> None:
    check = gross_profit_check(Decimal(1_000_000_000), [Decimal(3_000_000_000)], role="revenue")
    r = resolve(
        "revenue",
        REV,
        [cand(REV[0], 4_000_000_000), cand("Revenues", 4_400_000_000, 2)],
        presented_tags=frozenset({"Revenues"}),
        reconciles=check,
    )
    assert not r.ok and r.reason == "presentation_contradicts_calculation"


def test_unknown_identity_falls_through_to_the_top_line_rule() -> None:
    check = gross_profit_check(None, [Decimal(1)], role="revenue")
    r = resolve(
        "revenue",
        REV,
        [cand(REV[0], 4_000_000_000), cand("Revenues", 9_000_000_000, 2)],
        reconciles=check,
    )
    assert r.reason == "total_tag_is_top_line" and r.tag == "Revenues"


def test_two_periods_for_one_label_abstain() -> None:
    r = resolve(
        "revenue",
        REV,
        [
            cand("Revenues", 1_000_000_000),
            cand("Revenues", 1_000_000_000, 2, period_end=date(2021, 12, 31)),
        ],
    )
    assert r.reason == "multiple_periods"


def test_tags_outside_the_chain_are_ignored() -> None:
    r = resolve("revenue", REV, [cand("RevenuesNetOfInterestExpense", 7_000_000_000)])
    assert r.reason == "no_candidate" and not r.candidates


@given(
    st.integers(min_value=10**6, max_value=10**12),
    st.integers(min_value=10**6, max_value=10**12),
    st.booleans(),
)
def test_outcome_never_depends_on_input_order(a: int, b: int, flip: bool) -> None:
    """First-populated-wins would make the input order matter; the resolver must not."""
    xs = [cand(REV[0], a * 1000), cand("Revenues", b * 1000, 2)]
    r1 = resolve("revenue", REV, xs)
    r2 = resolve("revenue", REV, list(reversed(xs)) if flip else xs)
    assert (r1.reason, r1.tag, r1.chosen and r1.chosen.value) == (
        r2.reason,
        r2.tag,
        r2.chosen and r2.chosen.value,
    )
    if not r1.ok:
        assert r1.reason == "total_below_component" and b < a
    elif r1.reason == "total_tag_is_top_line":
        assert r1.tag == "Revenues" and b > a
    if r1.reason not in ("candidates_agree", "single_candidate"):
        # a material disagreement with no other evidence: the pair rule always decides
        assert r1.reason == ("total_tag_is_top_line" if b > a else "total_below_component")
