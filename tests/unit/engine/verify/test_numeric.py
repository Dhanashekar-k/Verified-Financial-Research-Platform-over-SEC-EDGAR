"""``verify/numeric.py``: provenance re-execution, number-in-text, metadata and wording checks
(M06 3.2, 4.1; ADR-049 D1-D3, D7). Written before the implementation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.engine.compose.format import format_value
from app.engine.verify import numbers, numeric
from app.engine.verify.models import CheckOutcome, VerifyConfig
from app.engine.verify.numeric import check_numeric
from tests.unit.engine.verify import verify_cases as vc

pytestmark = pytest.mark.xfail(
    condition=not (numeric.IMPLEMENTED and numbers.IMPLEMENTED),
    raises=NotImplementedError,
    strict=True,
    reason="Shekar-owned verify/numeric.py and verify/numbers.py are not implemented yet",
)

D = Decimal
CFG = VerifyConfig()
DECIDED = ("provenance_exec", "metadata_match", "number_in_text")


def run(case: vc.Case, cfg: VerifyConfig = CFG) -> CheckOutcome:
    return check_numeric(case.claim, case.evidence, case.ctx, cfg)


def assert_passes(outcome: CheckOutcome) -> None:
    got = {name: getattr(outcome.checks, name) for name in DECIDED}
    assert got == dict.fromkeys(DECIDED, "pass"), outcome.reason
    assert outcome.checks.quote_substring == "n/a"
    assert outcome.checks.entailment == "n/a"
    assert outcome.passed and outcome.reason == ""


def assert_fails(outcome: CheckOutcome, check: str, *, others_pass: tuple[str, ...] = ()) -> None:
    assert getattr(outcome.checks, check) == "fail", outcome
    for name in others_pass:
        assert getattr(outcome.checks, name) == "pass", (name, outcome)
    assert outcome.checks.quote_substring == "n/a"
    assert outcome.checks.entailment == "n/a"
    assert not outcome.passed and outcome.reason.strip()


def with_claim(case: vc.Case, **changes: object) -> vc.Case:
    return case._replace(claim=vc.edit(case.claim, **changes))


# --- correct claims are kept --------------------------------------------------------------------

CORRECT = {
    "reported_value": vc.revenue_case,
    "ratio_over_usd_inputs": vc.margin_case,
    "fallback_formula_operand_tags": vc.fallback_gross_profit_case,
    "growth_decreased_unsigned": vc.growth_case,
    "growth_over_two_years": vc.two_year_growth_case,
    "compare_apple": lambda: vc.compare_case(0, "Apple's FY2023 revenue was $383.3 billion."),
    "compare_microsoft": lambda: vc.compare_case(
        1, "Microsoft's FY2023 revenue was $211.9 billion."
    ),
    "difference_naming_both": lambda: vc.compare_case(
        2, "Apple's FY2023 revenue was $171.4 billion higher than Microsoft's."
    ),
    "derived_q4": vc.derived_q4_case,
    "quarter": vc.quarter_case,
    "eps_stored_as_usd": vc.eps_case,
    "instant_with_date": vc.instant_assets_case,
    "january_fiscal_year_end": vc.january_fye_case,
    "restated_latest_value": vc.restated_latest_case,
}


@pytest.mark.parametrize("name", CORRECT)
def test_correct_claim_is_kept(name: str) -> None:
    """A true claim built the way the composer builds it passes all three checks with no reason
    (M06 4.1: the correct claims measure false drops)."""
    assert_passes(run(CORRECT[name]()))


@pytest.mark.parametrize(
    "text",
    [
        "Apple's revenue was $383.3 billion.",
        "Apple's revenue in 2023 was $383.3 billion.",
        "In FY23, Apple's revenue was $383.3 billion.",
        "Apple's FY2023 10-K reports revenue of $383.3 billion [1].",
        "For the year ended September 30, 2023, revenue was $383.3 billion.",
        "Revenue was $383,285 million in fiscal 2023.",
        "Revenue was $383.3 billion, or $383,285 million, in fiscal 2023.",
        "Revenue was $383.3B in fiscal 2023.",
        "AAPL reported FY2023 revenue of $383.3 billion.",
        "The company reported revenue of $383 billion.",
    ],
)
def test_correct_wordings_of_one_value_are_kept(text: str) -> None:
    """Years, labels, dates, citation markers, another correct scaling, a coarser correct
    rounding and naming no company must not cause a false drop."""
    assert_passes(run(vc.revenue_case(text)))


def test_in_2018_is_not_an_unsupported_number() -> None:
    """ "In 2018" is a year: it must satisfy the year rule and never count as a value."""
    res = vc.result("265595000000", [vc.REV23], fiscal_year=2018)
    case = vc.Case(
        vc.numeric_claim("In 2018, Apple's revenue was $265.6 billion.", res),
        vc.evidence(vc.fact(vc.REV23, "265595000000", fiscal_year=2018)),
        vc.context([res], vc.plan(years=[2018])),
    )
    assert_passes(run(case))


def test_fact_ids_are_compared_as_a_set() -> None:
    """D2 compares ``set(fact_ids)``: the same expression with its ids listed in another order
    is the compiler's expression."""
    case = vc.margin_case()
    provenance = case.claim.provenance.model_dump()
    provenance["fact_ids"] = list(reversed(provenance["fact_ids"]))
    assert_passes(run(with_claim(case, provenance=provenance)))


def test_negative_fact_id_is_evaluated() -> None:
    """A fact id is a signed 64-bit hash, so ``f:-665...`` is one token, not a subtraction."""
    res = vc.result("383285000000", [vc.NEGATIVE_ID])
    case = vc.Case(
        vc.numeric_claim("Apple's FY2023 revenue was $383.3 billion.", res),
        vc.evidence(vc.fact(vc.NEGATIVE_ID, "383285000000")),
        vc.context([res]),
    )
    assert_passes(run(case))


def test_claim_without_a_display_is_judged_on_value_and_text() -> None:
    """``display`` is optional: its absence is not a failure."""
    assert_passes(run(with_claim(vc.revenue_case(), display=None)))


@given(st.integers(-(10**14), 10**14))
def test_composer_written_claim_over_any_amount_is_kept(amount: int) -> None:
    """For any amount, a claim that copies the value and writes the composer's own display
    passes. A failure is a systematic false drop."""
    res = vc.result(amount, [vc.REV23])
    case = vc.Case(
        vc.numeric_claim(f"The FY2023 figure was {format_value(D(amount), 'USD')}.", res),
        vc.evidence(vc.fact(vc.REV23, amount)),
        vc.context([res]),
    )
    assert_passes(run(case))


# --- D1: tolerance of claim.value and claim.display ---------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("385201425000", "pass"),  # v* + 0.5% exactly
        ("385201425001", "fail"),
        ("381368575000", "pass"),  # v* - 0.5% exactly
        ("381368574999", "fail"),
    ],
)
def test_claim_value_tolerance_boundary_is_inclusive(value: str, expected: str) -> None:
    """D1: ``|claim.value - v*| <= max(0.5% of |v*|, display precision)``, inclusive."""
    assert run(vc.revenue_case(value=value)).checks.provenance_exec == expected


@given(
    st.integers(10**9, 10**13),
    st.integers(-20_000, 20_000),
)
def test_claim_value_passes_exactly_inside_the_relative_tolerance(
    amount: int, millionths: int
) -> None:
    """With no display, provenance passes if and only if claim.value is within rel x |v*| of
    the re-executed value."""
    offset = D(amount) * D(millionths) / D(1_000_000)
    res = vc.result(amount, [vc.REV23])
    claim = vc.edit(
        vc.numeric_claim(f"The figure was {format_value(D(amount), 'USD')}.", res),
        value=D(amount) + offset,
        display=None,
    )
    outcome = check_numeric(claim, vc.evidence(vc.fact(vc.REV23, amount)), vc.context([res]), CFG)
    expected = "pass" if abs(offset) <= CFG.rel_tolerance * amount else "fail"
    assert outcome.checks.provenance_exec == expected


def test_relative_tolerance_comes_from_the_config() -> None:
    """A stricter configured ``rel_tolerance`` rejects a value the default accepts."""
    case = vc.revenue_case(value="384000000000")
    assert run(case).checks.provenance_exec == "pass"
    strict = VerifyConfig(rel_tolerance=D("0.0001"))
    assert run(case, strict).checks.provenance_exec == "fail"


def test_missing_claim_value_fails_provenance() -> None:
    """D1 requires ``claim.value``: scoring reads it, so a claim without one is not verified."""
    assert_fails(run(with_claim(vc.revenue_case(), value=None)), "provenance_exec")


def test_display_far_from_the_value_fails_provenance() -> None:
    """A display that states another figure fails even when value and text are right."""
    assert_fails(run(vc.revenue_case(display="$390.0 billion")), "provenance_exec")


def test_display_inside_the_relative_tolerance_passes() -> None:
    """The specified consequence of D1: the display uses the value tolerance, so "$385.0
    billion" as a display passes although the same string in the text would not."""
    assert_passes(run(vc.revenue_case(display="$385.0 billion")))


# --- adversarial (M06 4.1) ----------------------------------------------------------------------


def test_close_but_wrong_rounding_in_text_fails() -> None:
    """ "$385.0 billion" for 383,285,000,000 is within 0.5% yet wrong as written: text numbers
    get their own precision only."""
    outcome = run(vc.revenue_case("Apple's revenue in fiscal 2023 was $385.0 billion."))
    assert_fails(outcome, "number_in_text", others_pass=("provenance_exec", "metadata_match"))


def test_last_digit_off_in_text_fails() -> None:
    """One step in the last shown place is already wrong."""
    outcome = run(vc.revenue_case("Apple's revenue in fiscal 2023 was $383.4 billion."))
    assert_fails(outcome, "number_in_text")


def test_text_number_that_only_matches_the_claims_own_value_fails() -> None:
    """Text is compared with the re-executed v*, not with ``claim.value``: a claim cannot widen
    its own target by moving its value inside the tolerance."""
    case = vc.revenue_case("Apple's FY2023 revenue was $385.0 billion.", value="385000000000")
    outcome = run(case)
    assert_fails(outcome, "number_in_text", others_pass=("provenance_exec",))


def test_extra_unsupported_number_in_text_fails() -> None:
    """One right number does not license another that nothing supports."""
    case = vc.revenue_case("Apple's FY2023 revenue was $383.3 billion, including $91.7 billion.")
    assert_fails(run(case), "number_in_text")


def test_text_without_the_value_fails() -> None:
    """At least one value in the text must state v*; years alone do not."""
    case = vc.revenue_case("Apple reported higher revenue in fiscal 2023.")
    assert_fails(run(case), "number_in_text")


def test_million_for_billion_in_text_fails() -> None:
    """Scale errors in the text are caught."""
    case = vc.revenue_case("Apple's FY2023 revenue was $383.3 million.")
    assert_fails(run(case), "number_in_text", others_pass=("provenance_exec",))


def test_million_for_billion_in_value_fails() -> None:
    """Scale errors in ``claim.value`` are caught."""
    assert_fails(run(vc.revenue_case(value="383285000")), "provenance_exec")


def test_million_for_billion_in_display_fails() -> None:
    """Scale errors in ``claim.display`` are caught."""
    assert_fails(run(vc.revenue_case(display="$383.3 million")), "provenance_exec")


def test_percent_stored_as_value_fails() -> None:
    """29.8 is not the ratio 0.298: values are fractions in base units."""
    assert_fails(run(with_claim(vc.margin_case(), value=D("29.8"))), "provenance_exec")


@pytest.mark.parametrize("written", ["29.8", "0.298%", "29.8x"])
def test_percent_ratio_confusion_in_text_fails(written: str) -> None:
    """A percentage without its sign, or a fraction with one, states another number."""
    case = vc.margin_case(f"Apple's FY2023 operating margin was {written}.")
    assert_fails(run(case), "number_in_text")


def test_ratio_written_as_a_fraction_is_the_same_number() -> None:
    """Percent and ratio are one thing in base units, so "0.298" states 29.8%."""
    assert_passes(run(vc.margin_case("Apple's FY2023 operating margin was 0.298.")))


def test_sign_flip_in_value_fails() -> None:
    """A positive ``claim.value`` over a negative result fails."""
    case = with_claim(vc.growth_case(), value=D("0.367"))
    assert_fails(run(case), "provenance_exec")


def test_sign_flip_in_text_fails() -> None:
    """A negative figure in the text over a positive fact fails."""
    case = vc.revenue_case("Apple's FY2023 revenue was -$383.3 billion.")
    assert_fails(run(case), "number_in_text")


def test_unsigned_magnitude_without_a_direction_word_fails() -> None:
    """Without a direction word the sign must match: "changed by 36.7%" is not -36.7%."""
    case = vc.growth_case("Apple's revenue changed by 36.7% from fiscal 2022 to fiscal 2023.")
    assert_fails(run(case), "number_in_text")


def test_signed_change_without_a_direction_word_passes() -> None:
    """The composer's neutral wording with a signed figure is correct."""
    case = vc.growth_case("Apple's revenue changed by -36.7% from fiscal 2022 to fiscal 2023.")
    assert_passes(run(case))


@pytest.mark.parametrize("word", ["decreased", "declined", "fell"])
def test_downward_word_may_carry_an_unsigned_magnitude(word: str) -> None:
    """ "decreased 36.7%" states -0.367 (a required false-drop case)."""
    case = vc.growth_case(f"Apple's revenue {word} 36.7% in fiscal 2023.")
    assert_passes(run(case))


@pytest.mark.parametrize("word", ["increased", "grew", "rose"])
def test_upward_word_over_a_negative_growth_fails(word: str) -> None:
    """ "increased 36.7%" over -0.367 reverses the meaning and must fail the direction rule,
    although the magnitude is right."""
    outcome = run(vc.growth_case(f"Apple's revenue {word} 36.7% in fiscal 2023."))
    assert_fails(outcome, "metadata_match", others_pass=("provenance_exec",))


def test_higher_over_a_negative_difference_fails() -> None:
    """For a difference, "higher" requires v* > 0 (a false keep to close)."""
    case = vc.reverse_compare_case(
        "Microsoft's FY2023 revenue was $171.4 billion higher than Apple's."
    )
    assert_fails(run(case), "metadata_match", others_pass=("provenance_exec",))


def test_lower_over_a_negative_difference_passes() -> None:
    """ "lower" with an unsigned magnitude states the negative difference."""
    case = vc.reverse_compare_case(
        "Microsoft's FY2023 revenue was $171.4 billion lower than Apple's."
    )
    assert_passes(run(case))


def test_lower_over_a_positive_difference_fails() -> None:
    """The mirror case: "lower" requires v* < 0."""
    case = vc.compare_case(2, "Apple's FY2023 revenue was $171.4 billion lower than Microsoft's.")
    assert_fails(run(case), "metadata_match", others_pass=("provenance_exec",))


@pytest.mark.parametrize("wrong", ["2022", "FY2022", "FY22", "fiscal 2024"])
def test_wrong_year_in_text_fails_metadata(wrong: str) -> None:
    """The figure is right but filed under another year. It is not a value, so only the year
    wording rule can catch it."""
    case = vc.revenue_case(f"Apple's {wrong} revenue was $383.3 billion.")
    outcome = run(case)
    assert_fails(outcome, "metadata_match", others_pass=("provenance_exec", "number_in_text"))


def test_one_wrong_year_among_right_ones_fails() -> None:
    """Every year named must be allowed, not just one of them."""
    case = vc.revenue_case("In 2023, unlike 2019, revenue was $383.3 billion.")
    assert_fails(run(case), "metadata_match")


def test_cited_fact_of_another_year_fails_metadata() -> None:
    """The live fact's fiscal year must be the matched result's."""
    case = vc.revenue_case()
    case = case._replace(evidence=vc.replace_fact(case.evidence, vc.REV23, fiscal_year=2022))
    assert_fails(run(case), "metadata_match")


def test_cited_fact_of_another_period_fails_metadata() -> None:
    """A quarter fact cannot stand under a full-year result."""
    case = vc.revenue_case()
    case = case._replace(
        evidence=vc.replace_fact(case.evidence, vc.REV23, fiscal_period="Q4", qtrs=1)
    )
    assert_fails(run(case), "metadata_match")


def test_citing_another_years_fact_fails_provenance() -> None:
    """A claim that swaps in a fact the compiler did not select fails D2 even though the value
    agrees with that fact."""
    case = vc.revenue_case()
    claim = vc.expr_claim(
        "Apple's FY2023 revenue was $394.3 billion.",
        f"f:{vc.REV22}",
        [vc.REV22],
        "394328000000",
    )
    ev = vc.evidence(
        case.evidence.facts[vc.REV23], vc.fact(vc.REV22, "394328000000", fiscal_year=2022)
    )
    assert_fails(run(vc.Case(claim, ev, case.ctx)), "provenance_exec")


# --- wrong company ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Microsoft's FY2023 revenue was $383.3 billion.",
        "Microsoft Corp reported FY2023 revenue of $383.3 billion.",
        "MSFT reported FY2023 revenue of $383.3 billion.",
        "Big Blue reported FY2023 revenue of $383.3 billion.",
        "GOOG reported FY2023 revenue of $383.3 billion.",
        "Google's FY2023 revenue was $383.3 billion.",
        "Apple and Microsoft reported FY2023 revenue of $383.3 billion.",
    ],
)
def test_naming_another_company_fails_metadata(text: str) -> None:
    """Apple's figure attributed to a company named by name, ticker, share-class ticker or
    alias fails; so does adding a second company to a single-company figure."""
    outcome = run(vc.revenue_case(text))
    assert_fails(outcome, "metadata_match", others_pass=("provenance_exec", "number_in_text"))


@pytest.mark.parametrize(
    ("which", "text"),
    [
        (0, "Microsoft's FY2023 revenue was $383.3 billion."),
        (1, "Apple's FY2023 revenue was $211.9 billion."),
    ],
)
def test_compare_claims_with_swapped_names_both_fail(which: int, text: str) -> None:
    """Both halves of a swapped comparison fail, although each company is in the plan and each
    number is real (a required false-keep case)."""
    outcome = run(vc.compare_case(which, text))
    assert_fails(outcome, "metadata_match", others_pass=("provenance_exec", "number_in_text"))


@pytest.mark.parametrize(
    "text",
    [
        "Apple's FY2023 revenue was $171.4 billion higher.",
        "FY2023 revenue was $171.4 billion higher than Microsoft's.",
        "The FY2023 revenue difference was $171.4 billion.",
    ],
)
def test_difference_may_name_either_company_or_neither(text: str) -> None:
    """A difference belongs to two companies, and a claim naming no company passes."""
    assert_passes(run(vc.compare_case(2, text)))


def test_difference_naming_a_third_company_fails() -> None:
    """Only the two compared companies may be named."""
    case = vc.compare_case(2, "Apple's FY2023 revenue was $171.4 billion higher than Google's.")
    assert_fails(run(case), "metadata_match")


def test_alias_inside_another_word_does_not_name_a_company() -> None:
    """ "purchases" does not name Chase: substring matching of aliases would drop true capex
    claims, since every tracked company is in ``ctx.companies``."""
    res = vc.result("10959000000", [vc.REV23], metric="capex")
    case = vc.Case(
        vc.numeric_claim("Apple's FY2023 purchases of equipment were $11.0 billion.", res),
        vc.evidence(
            vc.fact(vc.REV23, "10959000000", tag="PaymentsToAcquirePropertyPlantAndEquipment")
        ),
        vc.context([res], vc.plan(metrics=["capex"])),
    )
    assert_passes(run(case))


def test_fact_of_another_company_fails_metadata() -> None:
    """The live fact's cik must be the matched result's, even if both are allowed."""
    case = vc.revenue_case()
    case = case._replace(
        evidence=vc.replace_fact(case.evidence, vc.REV23, cik=vc.MICROSOFT),
        ctx=case.ctx.model_copy(update={"allowed_ciks": frozenset({vc.APPLE, vc.MICROSOFT})}),
    )
    assert_fails(run(case), "metadata_match")


def test_company_outside_the_allowed_set_fails_metadata() -> None:
    """A fact must belong to an allowed company even when it agrees with the result."""
    case = vc.revenue_case()
    case = case._replace(ctx=case.ctx.model_copy(update={"allowed_ciks": frozenset({789019})}))
    assert_fails(run(case), "metadata_match")


def test_empty_allowed_set_is_no_restriction() -> None:
    """``allowed_ciks`` empty means unrestricted, not "nothing is allowed"."""
    case = vc.revenue_case()
    case = case._replace(ctx=case.ctx.model_copy(update={"allowed_ciks": frozenset()}))
    assert_passes(run(case))


def test_wrong_metric_name_in_text_is_not_checked() -> None:
    """Documented limit: the metric's name in the text is not checked, so "operating income"
    over a revenue fact passes. This pins the boundary, it does not endorse the claim."""
    assert_passes(run(vc.revenue_case("Apple's FY2023 operating income was $383.3 billion.")))


# --- quarters -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Apple's Q2 FY2023 revenue was $94.8 billion.",
        "Apple's revenue in the second quarter of fiscal 2023 was $94.8 billion.",
        "Apple's quarterly revenue was $94.8 billion.",
    ],
)
def test_right_or_absent_quarter_passes(text: str) -> None:
    """The matched quarter, written either way or left out, is accepted."""
    assert_passes(run(vc.quarter_case(text)))


@pytest.mark.parametrize(
    "text",
    [
        "Apple's Q3 FY2023 revenue was $94.8 billion.",
        "Apple's revenue in the third quarter of fiscal 2023 was $94.8 billion.",
        "Apple's first quarter revenue was $94.8 billion.",
    ],
)
def test_wrong_quarter_fails_metadata(text: str) -> None:
    """A real quarterly figure under another quarter fails, in digits or in words (a required
    false-keep case)."""
    outcome = run(vc.quarter_case(text))
    assert_fails(outcome, "metadata_match", others_pass=("provenance_exec", "number_in_text"))


@pytest.mark.parametrize("quarter", ["Q4", "fourth quarter"])
def test_full_year_claim_may_name_no_quarter(quarter: str) -> None:
    """A full-year figure presented as a quarter's fails."""
    case = vc.revenue_case(f"Apple's {quarter} 2023 revenue was $383.3 billion.")
    assert_fails(run(case), "metadata_match")


def test_derived_q4_in_words_passes() -> None:
    """The derived Q4 result is matched on its own period."""
    case = vc.derived_q4_case("Apple's fourth quarter fiscal 2023 revenue was $30.0 billion.")
    assert_passes(run(case))


# --- years that look wrong but are right --------------------------------------------------------


def test_december_31_date_passes() -> None:
    """ "December 31, 2021" is a day label plus an allowed year."""
    assert_passes(run(vc.instant_assets_case()))


@pytest.mark.parametrize(
    "text",
    [
        "Walmart's revenue was $648.1 billion in the year to January 2024.",
        "Walmart's fiscal 2023 revenue was $648.1 billion.",
    ],
)
def test_january_fiscal_year_end_accepts_both_years(text: str) -> None:
    """Fiscal 2023 may end in January 2024: the calendar year of a cited period_end is as
    legitimate as the fiscal year."""
    assert_passes(run(vc.january_fye_case(text)))


def test_january_fiscal_year_end_still_rejects_other_years() -> None:
    """The allowance adds one calendar year, not any neighbouring year."""
    case = vc.january_fye_case("Walmart's 2022 revenue was $648.1 billion.")
    assert_fails(run(case), "metadata_match")


# --- growth -------------------------------------------------------------------------------------


def test_two_year_growth_uses_the_plans_base_year() -> None:
    """With 2021 and 2023 named, the base is 2021, not t-1: both years may be named and the
    2021 base fact is correct."""
    assert_passes(run(vc.two_year_growth_case()))


def test_year_between_base_and_target_is_not_allowed() -> None:
    """ "from 2022" misstates a growth measured from 2021."""
    case = vc.two_year_growth_case("Apple's revenue grew 4.8% from fiscal 2022 to fiscal 2023.")
    assert_fails(run(case), "metadata_match")


def test_base_fact_from_the_wrong_year_fails_metadata() -> None:
    """The base fact must be of the base period the plan implies, even if the arithmetic and
    expression match."""
    case = vc.two_year_growth_case("Apple's revenue grew 4.8%.", base_year=2022)
    assert_fails(run(case), "metadata_match", others_pass=("provenance_exec",))


# --- provenance execution and D2 ----------------------------------------------------------------


def test_expression_the_compiler_never_built_fails_although_arithmetic_is_right() -> None:
    """D2: a difference the model assembled from real facts is rejected even though every digit
    of its arithmetic is right (a required false-keep case)."""
    case = vc.compare_case(0, "x")
    ctx = vc.context(vc.compare_results()[:2], case.ctx.plan)
    claim = vc.expr_claim(
        "Apple's FY2023 revenue was $171.4 billion higher than Microsoft's.",
        f"f:{vc.REV23} - f:{vc.MSFT_REV23}",
        [vc.REV23, vc.MSFT_REV23],
        "171370000000",
    )
    assert_fails(run(vc.Case(claim, case.evidence, ctx)), "provenance_exec")


def test_equivalent_but_different_expression_fails() -> None:
    """D2 is equality with a compiled expression, not algebraic equivalence."""
    case = vc.revenue_case()
    provenance = {"type": "expr", "expr": f"f:{vc.REV23} * 1", "fact_ids": [vc.REV23]}
    assert_fails(run(with_claim(case, provenance=provenance)), "provenance_exec")


def test_swapped_operands_fail() -> None:
    """The same fact ids in a different expression are a different claim."""
    case = vc.margin_case("Apple's FY2023 ratio was 335.3%.")
    provenance = {
        "type": "expr",
        "expr": f"f:{vc.REV23} / f:{vc.OPINC23}",
        "fact_ids": [vc.REV23, vc.OPINC23],
    }
    value = D("383285000000") / D("114301000000")
    claim = vc.edit(case.claim, provenance=provenance, value=value, display=None)
    assert_fails(run(case._replace(claim=claim)), "provenance_exec")


def test_numeric_claim_citing_a_chunk_fails_provenance() -> None:
    """A numeric claim's provenance must be an expression."""
    case = vc.revenue_case()
    provenance = {"type": "chunk", "chunk_id": "chunk-1", "quote": "revenue was 383,285"}
    assert_fails(run(with_claim(case, provenance=provenance)), "provenance_exec")


def test_cited_fact_absent_from_evidence_fails() -> None:
    """A fact not in the live evidence does not exist (D7: fail closed)."""
    case = vc.revenue_case()
    case = case._replace(evidence=vc.drop_fact(case.evidence, vc.REV23))
    assert_fails(run(case), "provenance_exec")


def test_value_is_re_executed_from_live_facts_not_copied_from_the_result() -> None:
    """v* comes from the facts: if the live row differs from what was compiled, the claim that
    copied the compiled value fails."""
    case = vc.revenue_case()
    case = case._replace(evidence=vc.replace_fact(case.evidence, vc.REV23, value=D("300000000000")))
    assert_fails(run(case), "provenance_exec")


def test_zero_divisor_in_live_facts_fails_without_raising() -> None:
    """A live denominator of zero must fail the claim closed, not crash the answer."""
    case = vc.margin_case()
    case = case._replace(evidence=vc.replace_fact(case.evidence, vc.REV23, value=D(0)))
    assert_fails(run(case), "provenance_exec")


# --- derived Q4 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("parent", [vc.Q4_FY, vc.Q4_9M])
def test_derived_q4_with_a_missing_parent_fails(parent: int) -> None:
    """A derived value that cannot be recomputed is not verified (D7: fail closed)."""
    case = vc.derived_q4_case()
    case = case._replace(evidence=vc.drop_fact(case.evidence, parent))
    assert_fails(run(case), "provenance_exec")


def test_derived_q4_that_does_not_equal_fy_minus_nine_months_fails() -> None:
    """The stored Q4 must equal first parent minus second parent."""
    case = vc.derived_q4_case()
    case = case._replace(evidence=vc.replace_fact(case.evidence, vc.Q4_9M, value=D("69000000000")))
    assert_fails(run(case), "provenance_exec")


def test_derived_q4_with_parents_in_reverse_order_fails() -> None:
    """``derived_from`` is [FY, 9M]; reversed it recomputes to the negative."""
    case = vc.derived_q4_case()
    case = case._replace(
        evidence=vc.replace_fact(case.evidence, vc.Q4, derived_from=[vc.Q4_9M, vc.Q4_FY])
    )
    assert_fails(run(case), "provenance_exec")


# --- restated latest values ---------------------------------------------------------------------


def test_latest_value_claim_that_states_the_original_value_fails() -> None:
    """A claim citing the restated fact is held to that fact's value."""
    case = vc.restated_latest_case(
        "Apple's FY2023 revenue was later restated to $383.3 billion.", value="383285000000"
    )
    assert_fails(run(case), "provenance_exec")


def test_latest_fact_without_a_restated_note_fails() -> None:
    """``f:<latest>`` is legal only because a compiled result's note names it."""
    case = vc.restated_latest_case()
    ctx = vc.context([vc.result("383285000000", [vc.REV23])])
    assert_fails(run(case._replace(ctx=ctx)), "provenance_exec")


def test_latest_value_of_a_note_on_a_ratio_result_is_not_citable() -> None:
    """On a ratio result the restated figure is an input in another unit, so D2 excludes it."""
    margin = vc.margin_case()
    res = margin.ctx.results[0].model_copy(update={"notes": [vc.restated_note()]})
    latest = vc.restated_latest_case()
    ev = vc.evidence(*margin.evidence.facts.values(), latest.evidence.facts[vc.REV23_LATEST])
    ctx = vc.context([res], margin.ctx.plan)
    assert_fails(run(vc.Case(latest.claim, ev, ctx)), "provenance_exec")


# --- tags, taxonomy and units -------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"tag": "NetIncomeLoss"},
        {"taxonomy": "aapl"},
        {"uom": "shares"},
        {"uom": "USD/shares"},
    ],
    ids=["wrong_tag", "custom_taxonomy", "shares_uom", "per_share_uom"],
)
def test_fact_that_does_not_fit_the_metric_fails_metadata(change: dict[str, str]) -> None:
    """The cited row must be a us-gaap fact with one of the metric's tags and a fitting unit."""
    case = vc.revenue_case()
    case = case._replace(evidence=vc.replace_fact(case.evidence, vc.REV23, **change))
    outcome = run(case)
    assert_fails(outcome, "metadata_match", others_pass=("provenance_exec", "number_in_text"))


def test_ratio_input_with_an_unrelated_tag_fails() -> None:
    """A ratio's inputs are limited to the tags of its operands."""
    case = vc.margin_case()
    case = case._replace(evidence=vc.replace_fact(case.evidence, vc.OPINC23, tag="Assets"))
    assert_fails(run(case), "metadata_match")


def test_ratio_input_with_the_wrong_unit_fails() -> None:
    """A ratio's inputs use their own metrics' units, here USD."""
    case = vc.margin_case()
    case = case._replace(evidence=vc.replace_fact(case.evidence, vc.OPINC23, uom="shares"))
    assert_fails(run(case), "metadata_match")


@pytest.mark.parametrize("uom", ["USD", "USD/shares"])
def test_eps_accepts_either_per_share_unit(uom: str) -> None:
    """FSDS records per-share amounts as USD (05 4.5): demanding "USD/shares" would drop every
    EPS claim."""
    assert_passes(run(vc.eps_case(uom=uom)))


def test_eps_fact_in_shares_fails() -> None:
    """The per-share alias does not admit a share count."""
    assert_fails(run(vc.eps_case(uom="shares")), "metadata_match")


def test_share_count_metric_requires_shares() -> None:
    """A shares metric over a USD row fails."""
    res = vc.result("15550061000", [vc.REV23], metric="shares_outstanding", unit="shares")
    claim = vc.numeric_claim("Apple had 15.6 billion shares outstanding in fiscal 2023.", res)
    ctx = vc.context([res], vc.plan(metrics=["shares_outstanding"]))
    good = vc.fact(
        vc.REV23, "15550061000", tag="CommonStockSharesOutstanding", uom="shares", qtrs=0,
        period_end=date(2023, 9, 30),
    )  # fmt: skip
    assert_passes(run(vc.Case(claim, vc.evidence(good), ctx)))
    bad = good.model_copy(update={"uom": "USD"})
    assert_fails(run(vc.Case(claim, vc.evidence(bad), ctx)), "metadata_match")
