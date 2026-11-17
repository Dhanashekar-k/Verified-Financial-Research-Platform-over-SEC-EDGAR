"""The ADR-022 behaviour of `pipelines.common.fiscal_ref.derive_fiscal`.

`fiscal_ref.py` is Shekar's file (ADR-022 added the sort of its input by a stable key and the
rule that derives Q4 only from FY rows of 10-K, 10-KT and 10-K/A filings). These tests state the
behaviour with the hand-derived scenarios of `tests/tie_cases.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from pipelines.common.fiscal_ref import derive_fiscal
from tests.fiscal_cases import (
    FiscalScenario,
    check_fiscal_scenario,
    check_no_derivation,
    to_rows,
)
from tests.tie_cases import (
    S1_INELIGIBLE,
    S1_MIXED,
    S2,
    S3_SMALLEST_IS_MINUS_ONE,
    S3_SMALLEST_IS_ZERO,
    S4,
    reorder,
    signature,
    tie_heavy,
)

TWO_ORDERS = ("given", "reversed")


def canonical(rows: list[dict[str, Any]]) -> list[str]:
    """Output rows in an order-free, comparable form (flags sorted: a frozenset has no order)."""
    return sorted(
        repr(sorted({**r, "dq_flags": tuple(sorted(r["dq_flags"]))}.items())) for r in rows
    )


def run(scenario: FiscalScenario, order: str) -> list[dict[str, Any]]:
    return to_rows(derive_fiscal(reorder(scenario.facts, order)))


def assert_same_in_both_orders_and_hand_derived(scenario: FiscalScenario) -> None:
    """The output is identical for the facts as given and reversed, and equals the expectation."""
    given, reversed_ = run(scenario, "given"), run(scenario, "reversed")
    assert canonical(given) == canonical(reversed_), "the output depends on the input order"
    check_fiscal_scenario(scenario, given)
    check_fiscal_scenario(scenario, reversed_)


@pytest.mark.parametrize("order", TWO_ORDERS)
@pytest.mark.parametrize("scenario", S1_INELIGIBLE, ids=repr)
def test_ref_fy_row_from_a_non_annual_form_is_never_a_parent(
    scenario: FiscalScenario, order: str
) -> None:
    """S1: an FY row from a 10-Q, 10-Q/A, S-1 or 10-KT/A filing does not even count as 'has an FY
    row', so with a 9M row to pair with, no Q4 is derived. (Today a Q4 is derived from it.)
    """
    check_no_derivation(run(scenario, order), len(scenario.facts))


@pytest.mark.parametrize("order", TWO_ORDERS)
def test_ref_ten_q_fy_row_next_to_a_ten_k_fy_row_derives_exactly_one_q4(order: str) -> None:
    """S1 mixed: a 10-Q FY row (999) filed after the 10-K FY row (100), plus a 9M (60): exactly
    one Q4, 100 - 60 = 40, from the 10-K row. (Today the 10-Q row is the 'latest' FY version and
    a second Q4, 939, is derived from it.)
    """
    check_fiscal_scenario(S1_MIXED, run(S1_MIXED, order))


def test_ref_fiscal_year_change_takes_the_smaller_fy_value_in_every_order() -> None:
    """S2: one 10-K, one filing date, two FY rows (2022-06-30 value 100, 2022-12-31 value 200),
    neither native, and a 9M of 50: the row that sorts first wins, so Q4 = 100 - 50 = 50 at
    2022-06-30 in either input order. (Today the first FY row in the list wins: 150 if reversed.)
    """
    assert_same_in_both_orders_and_hand_derived(S2)


@pytest.mark.parametrize("scenario", [S3_SMALLEST_IS_ZERO, S3_SMALLEST_IS_MINUS_ONE], ids=repr)
def test_ref_label_offset_tie_goes_to_the_smallest_accession_in_every_order(
    scenario: FiscalScenario,
) -> None:
    """S3: two 10-K submissions with offsets 0 and -1, once each: the offset of the smaller
    accession wins for every fact of the company, in either input order. (Today the offset of
    the first submission in the list wins, so reversing the input flips the fiscal years.)
    """
    assert_same_in_both_orders_and_hand_derived(scenario)


def test_ref_nine_month_tie_across_period_ends_takes_the_smaller_value_in_every_order() -> None:
    """S4: two 9M rows of one accession (2022-09-30 value 70, 2022-06-30 value 90) tie on
    (filed, accession): the group whose first row sorts first wins, so Q4 = 200 - 70 = 130 in
    either input order. (Today the first 9M row in the list wins: 110 if the 90 comes first.)
    """
    assert_same_in_both_orders_and_hand_derived(S4)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_ref_gives_one_answer_on_tie_heavy_data_in_either_order(seed: int) -> None:
    """On data full of ties and ineligible FY rows the reference gives one answer, whatever the
    order of the rows (the hand-derived scenarios above check the eligibility rule itself)."""
    facts = tie_heavy(seed)
    want = signature(derive_fiscal(facts))

    assert signature(derive_fiscal(facts[::-1])) == want
    assert signature(derive_fiscal(reorder(facts, "shuffle_1"))) == want
