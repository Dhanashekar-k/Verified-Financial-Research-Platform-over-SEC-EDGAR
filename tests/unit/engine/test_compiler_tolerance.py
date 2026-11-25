"""The compiler's "same number" rule must stay the gold generator's (ADR-043/044, ADR-047).

`app/engine/plan/compiler.py` keeps its own copy so `app/` does not import `eval/`; these tests
fail if either side changes alone.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.engine.plan import compiler
from eval.gen import numeric_policy

UNITS = ["USD", "shares", "USD/shares", "ratio"]


def test_tolerance_constants_match_the_gold_policy() -> None:
    assert compiler.SIG_FIG_REL == numeric_policy.SIG_FIG_REL
    assert compiler.AMOUNT_ABS == numeric_policy.AMOUNT_ABS
    assert compiler.PER_SHARE_ABS == numeric_policy.PER_SHARE_ABS
    assert compiler.RATIO_ABS == numeric_policy.RATIO_ABS


@settings(max_examples=300, deadline=None)
@given(
    base=st.decimals(min_value=-(10**12), max_value=10**12, places=4),
    # offsets that straddle the 0.05% and half-cent boundaries
    rel=st.decimals(min_value=Decimal("-0.002"), max_value=Decimal("0.002"), places=6),
    shift=st.decimals(min_value=Decimal("-1"), max_value=Decimal("1"), places=4),
    unit=st.sampled_from(UNITS),
)
def test_agree_matches_the_gold_policy(
    base: Decimal, rel: Decimal, shift: Decimal, unit: str
) -> None:
    other = base * (1 + rel) + shift
    assert compiler.agree(base, other, unit) == numeric_policy.agree(base, other, unit)


@pytest.mark.parametrize("unit", UNITS)
def test_agree_on_zero_and_identical_values(unit: str) -> None:
    for a, b in [(Decimal(0), Decimal(0)), (Decimal(5), Decimal(5)), (Decimal(0), Decimal(10))]:
        assert compiler.agree(a, b, unit) == numeric_policy.agree(a, b, unit)


# --- the engine never picks a tag where the gold resolver would pick another or abstain --------

CONTRACT = "RevenueFromContractWithCustomerExcludingAssessedTax"
_AMOUNTS = st.sampled_from([40, 60, 100, 110, 140, 150, 1000, 1000.2]).map(
    lambda v: Decimal(str(v)) * 1_000_000
)


@settings(max_examples=400, deadline=None)
@given(
    contract=_AMOUNTS,
    total=_AMOUNTS,
    cost=st.none() | _AMOUNTS,
    gross=st.none() | _AMOUNTS,
)
def test_revenue_picks_agree_with_the_gold_resolver(
    contract: Decimal, total: Decimal, cost: Decimal | None, gross: Decimal | None
) -> None:
    """With no presentation data, the engine is the gold rule minus the `Revenues`-is-the-top-line
    step: same pick as gold, or an abstention where gold used that step."""
    from datetime import date

    from app.engine.plan.catalog import load_catalog
    from app.engine.plan.schema import PeriodRef
    from eval.gen.revenue_resolver import gross_profit_check, resolve

    catalog = load_catalog()
    period = PeriodRef(fiscal_year=2022)
    values = {CONTRACT: contract, "Revenues": total}
    if cost is not None:
        values["CostOfRevenue"] = cost
    if gross is not None:
        values["GrossProfit"] = gross
    run = compiler._Run(catalog, None, set(values))  # type: ignore[arg-type]
    run.cache[(1, 2022, "FY")] = {
        tag: [
            compiler._Row(i, i, 1, tag, "USD", 4, date(2022, 12, 31), value, "10-K", False, False)
        ]
        for i, (tag, value) in enumerate(values.items(), start=1)
    }
    engine = run.evaluate("revenue", 1, period)

    gold_candidates = [
        numeric_policy.Versioned(
            numeric_policy.Fact(
                i, 1, tag, "us-gaap", "USD", 4, None, date(2022, 12, 31), 2022, "FY",
                values[tag], "a", "10-K", date(2023, 2, 1), True, True, False,
            ),
            None,
            "as_filed",
        )
        for i, tag in enumerate((CONTRACT, "Revenues"), start=1)
    ]  # fmt: skip
    gold = resolve(
        "revenue",
        catalog["revenue"].tags or [],
        gold_candidates,
        reconciles=gross_profit_check(gross, [cost] if cost is not None else [], role="revenue"),
    )
    if isinstance(engine, compiler._Value):
        assert gold.chosen is not None, gold.reason
        assert engine.value == gold.chosen.value
    elif gold.chosen is not None:
        assert gold.reason == "total_tag_is_top_line"
