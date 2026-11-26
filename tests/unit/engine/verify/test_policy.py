"""``verify/policy.py``: keep or drop, sufficiency (D5), refusals (ADR-041; ADR-049 decisions
H0, H, I and J, which replace D6's "keep a clean composer reason") and status. Written before
the implementation."""

from __future__ import annotations

from collections.abc import Collection, Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.engine.answer import Answer, Claim, Refusal
from app.engine.plan.schema import NotFound, NumericResult
from app.engine.verify import numbers, policy
from app.engine.verify.models import (
    DETERMINISTIC_CHECKS,
    CheckOutcome,
    Checks,
    Evidence,
    PolicyDecision,
    VerifierResult,
    VerifyConfig,
    VerifyContext,
)
from app.engine.verify.pipeline import Verifier, verify_answer
from app.engine.verify.policy import apply, decide
from tests.unit.engine.verify import verify_cases as vc

pytestmark = pytest.mark.xfail(
    condition=not (policy.IMPLEMENTED and numbers.IMPLEMENTED),
    raises=NotImplementedError,
    strict=True,
    reason="Shekar-owned verify/policy.py and verify/numbers.py are not implemented yet",
)

CFG = VerifyConfig()


# --- helpers ------------------------------------------------------------------------------------


def verdict(claim: Claim, *, keep: bool) -> VerifierResult:
    """A per-claim result as the checks would produce it, without calling ``decide``."""
    check = "pass" if keep else "fail"
    checks = (
        Checks(quote_substring=check) if claim.kind == "text" else Checks(provenance_exec=check)
    )
    return VerifierResult(
        claim_id=claim.id,
        status="supported" if keep else "unsupported",
        checks=checks,
        action="keep" if keep else "drop",
        reason="" if keep else "A check failed.",
    )


def run(
    claims: Sequence[Claim],
    ctx: VerifyContext,
    *,
    drop: Collection[int] = (),
    refusal: Refusal | None = None,
) -> PolicyDecision:
    """``apply`` over ``claims``, dropping those at the positions in ``drop``."""
    answer = Answer(answer_markdown="composer prose", claims=list(claims), refusal=refusal)
    results = [verdict(c, keep=i not in drop) for i, c in enumerate(claims)]
    return apply(answer, results, ctx, CFG)


def assert_refused(decision: PolicyDecision) -> Refusal:
    assert decision.status == "refused"
    assert decision.refusal is not None
    reason = decision.refusal.reason
    assert reason.strip() and "\n" not in reason
    assert decision.refusal.found == [c.text for c in decision.kept]
    return decision.refusal


def assert_answered(decision: PolicyDecision, status: str) -> None:
    assert decision.status == status
    assert decision.refusal is None
    assert decision.kept


def trend_results() -> list[NumericResult]:
    return [
        vc.result("383285000000", [vc.REV23]),
        vc.result("394328000000", [vc.REV22], fiscal_year=2022),
        vc.result("365817000000", [vc.REV21], fiscal_year=2021),
    ]


def trend_claims(*, primary: bool = True) -> list[Claim]:
    texts = [
        "Apple's FY2023 revenue was $383.3 billion.",
        "Apple's FY2022 revenue was $394.3 billion.",
        "Apple's FY2021 revenue was $365.8 billion.",
    ]
    return [
        vc.numeric_claim(t, r, claim_id=f"c{i}", primary=primary and i == 1)
        for i, (t, r) in enumerate(zip(texts, trend_results(), strict=True), start=1)
    ]


def trend_ctx() -> VerifyContext:
    return vc.context(trend_results(), vc.plan(years=[2021, 2022, 2023], operation="trend"))


def rank_results() -> list[NumericResult]:
    rows = [(vc.WALMART, 701, "648125000000"), (vc.APPLE, 101, "383285000000")]
    rows.append((vc.ALPHABET, 801, "307394000000"))
    return [vc.result(v, [i], cik=cik, rank=n) for n, (cik, i, v) in enumerate(rows, start=1)]


def rank_claims() -> list[Claim]:
    texts = [
        "Walmart ranked first with FY2023 revenue of $648.1 billion.",
        "Apple ranked second with FY2023 revenue of $383.3 billion.",
        "Alphabet ranked third with FY2023 revenue of $307.4 billion.",
    ]
    return [
        vc.numeric_claim(t, r, claim_id=f"c{i}", primary=i == 1)
        for i, (t, r) in enumerate(zip(texts, rank_results(), strict=True), start=1)
    ]


def rank_ctx() -> VerifyContext:
    """A rank over the whole universe: the plan names no company."""
    query = vc.plan(ciks=[], operation="rank")
    universe = frozenset({vc.WALMART, vc.APPLE, vc.ALPHABET, vc.MICROSOFT})
    return vc.context(rank_results(), query, allowed=universe)


def compare_claims(*, primary: int | None = None) -> list[Claim]:
    """[Apple value, Microsoft value, difference]."""
    texts = [
        "Apple's FY2023 revenue was $383.3 billion.",
        "Microsoft's FY2023 revenue was $211.9 billion.",
        "Apple's FY2023 revenue was $171.4 billion higher than Microsoft's.",
    ]
    return [
        vc.numeric_claim(t, r, claim_id=f"c{i + 1}", primary=primary == i)
        for i, (t, r) in enumerate(zip(texts, vc.compare_results(), strict=True))
    ]


def not_found(year: int = 2022) -> NotFound:
    return NotFound(
        metric="revenue", cik=vc.APPLE, fiscal_year=year, fiscal_period="FY", reason="no_fact"
    )


NARRATIVE_A = "Apple cites supply concentration as a risk."
NARRATIVE_B = "Apple cites currency movements as a risk."


def narrative_claims(*, primary: int | None = None) -> list[Claim]:
    return [
        vc.text_claim(t, "a quote of words", claim_id=f"t{i + 1}", primary=primary == i)
        for i, t in enumerate([NARRATIVE_A, NARRATIVE_B])
    ]


def narrative_ctx(**extra: object) -> VerifyContext:
    return vc.context([], vc.plan(intent="narrative", metrics=[], operation=None), **extra)


def hybrid_ctx() -> VerifyContext:
    return vc.context([vc.result("383285000000", [vc.REV23])], vc.plan(intent="hybrid"))


def value_claim(*, primary: bool = True) -> Claim:
    return vc.numeric_claim(
        "Apple's FY2023 revenue was $383.3 billion.",
        vc.result("383285000000", [vc.REV23]),
        primary=primary,
    )


def value_ctx(**extra: object) -> VerifyContext:
    return vc.context([vc.result("383285000000", [vc.REV23])], **extra)


# --- decide -------------------------------------------------------------------------------------


def test_claim_with_no_failed_check_is_kept() -> None:
    """No failed check means supported and kept; ``n/a`` is not a failure. Checks and claim id
    are carried over."""
    claim = value_claim()
    checks = Checks(provenance_exec="pass", metadata_match="pass", number_in_text="pass")
    got = decide(claim, CheckOutcome(checks=checks), CFG)
    assert (got.claim_id, got.status, got.action) == (claim.id, "supported", "keep")
    assert got.checks == checks


@pytest.mark.parametrize("check", DETERMINISTIC_CHECKS)
def test_any_failed_check_drops_the_claim(check: str) -> None:
    """One failure among passes is enough, and the outcome's checks and reason are kept for
    the report."""
    checks = Checks(**{**dict.fromkeys(DETERMINISTIC_CHECKS, "pass"), check: "fail"})
    got = decide(value_claim(), CheckOutcome(checks=checks, reason="It does not match."), CFG)
    assert (got.status, got.action) == ("unsupported", "drop")
    assert got.checks == checks
    assert got.reason == "It does not match."


@given(st.tuples(*[st.sampled_from(["pass", "fail", "n/a"])] * len(DETERMINISTIC_CHECKS)))
def test_decide_is_keep_exactly_when_nothing_failed(states: tuple[str, ...]) -> None:
    """Over every combination of check states there are only two verdicts: ``partial``,
    ``error`` and ``rewrite`` need an entailment stage that does not exist."""
    checks = Checks(**dict(zip(DETERMINISTIC_CHECKS, states, strict=True)))
    got = decide(value_claim(), CheckOutcome(checks=checks), CFG)
    expected = ("unsupported", "drop") if "fail" in states else ("supported", "keep")
    assert (got.status, got.action) == expected
    assert got.checks.entailment == "n/a"


# --- kept claims and status ---------------------------------------------------------------------


def test_everything_kept_is_verified() -> None:
    """Nothing dropped and sufficient: ``verified``, no refusal, all claims."""
    claims = trend_claims()
    decision = run(claims, trend_ctx())
    assert_answered(decision, "verified")
    assert decision.kept == claims


def test_dropping_an_unneeded_claim_is_partially_verified() -> None:
    """A dropped extra claim leaves the question answered. Survivors are unchanged and in
    composer order."""
    claims = [*trend_claims(), vc.text_claim("An unsupported aside.", "a quote of words")]
    claims.insert(1, vc.text_claim("A supported aside.", "a quote of words", claim_id="t2"))
    decision = run(claims, trend_ctx(), drop={4})
    assert_answered(decision, "partially_verified")
    assert decision.kept == claims[:4]


def test_apply_does_not_change_the_answer_it_was_given() -> None:
    """``apply`` decides; it does not edit the composer's answer in place."""
    answer = Answer(answer_markdown="composer prose", claims=trend_claims())
    before = answer.model_copy(deep=True)
    results = [verdict(c, keep=i != 2) for i, c in enumerate(answer.claims)]
    decision = apply(answer, results, trend_ctx(), CFG)
    assert decision.kept == before.claims[:2]  # it did decide: the dropped claim is gone
    assert answer == before


def test_verdicts_are_aligned_by_position_not_by_claim_id() -> None:
    """``results`` follows the claim order. The composer is a model and may repeat an id, which
    must not let a dropped claim survive under its twin's verdict."""
    good = value_claim()
    bad = vc.text_claim("An unsupported aside.", "a quote of words", claim_id=good.id)
    decision = run([good, bad], value_ctx(), drop={1})
    assert decision.kept == [good]
    assert decision.status == "partially_verified"


# --- sufficiency: value, ratio, trend, rank -----------------------------------------------------


def test_value_slot_dropped_refuses() -> None:
    """The one requested figure is gone, so the answer is a refusal."""
    decision = run([value_claim()], value_ctx(), drop={0})
    refusal = assert_refused(decision)
    assert decision.kept == []
    assert len(refusal.missing) >= 1


def test_ratio_slot_met_and_unmet() -> None:
    """A ratio result is a slot like a value."""
    case = vc.margin_case()
    claim = vc.edit(case.claim, primary=True)
    assert_answered(run([claim], case.ctx), "verified")
    assert_refused(run([claim], case.ctx, drop={0}))


def test_trend_needs_every_period() -> None:
    """A trend missing a year is not an answer, even with the primary claim kept. ``missing``
    has one entry per unmet slot, naming its period."""
    refusal = assert_refused(run(trend_claims(), trend_ctx(), drop={1, 2}))
    assert len(refusal.missing) == 2
    for year in ("2022", "2021"):
        assert sum(year in entry for entry in refusal.missing) == 1, refusal.missing


def test_slot_without_any_claim_is_unmet() -> None:
    """Sufficiency is read from ``ctx.results``: a figure the composer simply left out is as
    missing as a dropped one."""
    refusal = assert_refused(run(trend_claims()[:2], trend_ctx()))
    assert len(refusal.missing) == 1 and "2021" in refusal.missing[0]


def test_universe_rank_with_every_row_kept_is_verified() -> None:
    """A rank with ``plan.companies == []`` is answered when each result row is claimed."""
    assert rank_ctx().plan.companies == []
    assert_answered(run(rank_claims(), rank_ctx()), "verified")


@pytest.mark.parametrize("position", [1, 2])
def test_universe_rank_with_a_dropped_row_refuses(position: int) -> None:
    """An empty company list must not make sufficiency pass by itself."""
    refusal = assert_refused(run(rank_claims(), rank_ctx(), drop={position}))
    assert len(refusal.missing) == 1


def test_universe_rank_with_an_unclaimed_row_refuses() -> None:
    """The same for a ranked row the composer never claimed."""
    assert_refused(run(rank_claims()[:2], rank_ctx()))


def test_slot_is_matched_on_fact_ids_as_a_set() -> None:
    """The slot key is (expr, set(fact_ids)), so id order does not matter."""
    case = vc.margin_case()
    provenance = case.claim.provenance.model_dump()
    provenance["fact_ids"] = list(reversed(provenance["fact_ids"]))
    claim = vc.edit(case.claim, provenance=provenance)
    assert_answered(run([claim], case.ctx), "verified")


def test_duplicate_claim_for_a_slot_may_replace_a_dropped_one() -> None:
    """The slot needs one kept claim; a dropped non-primary duplicate does not unmeet it."""
    claims = [value_claim(), vc.edit(value_claim(primary=False), id="c2", text="Another wording.")]
    assert_answered(run(claims, value_ctx(), drop={1}), "partially_verified")


def test_restated_latest_value_does_not_meet_the_slot() -> None:
    """The latest restated value is extra context; the requested slot is the compiled
    (as-originally-reported) result."""
    latest = vc.restated_latest_case().claim
    ctx = vc.context([vc.restated_result()])
    original = vc.numeric_claim(
        "Apple's FY2023 revenue was originally reported as $383.3 billion.", vc.restated_result()
    )
    assert_answered(run([original, latest], ctx), "verified")
    assert_refused(run([latest], ctx))


# --- sufficiency: not_found, unsupported, growth, compare ---------------------------------------


def test_not_found_means_a_requested_figure_is_unmet() -> None:
    """Even with every compiled result claimed, a NotFound entry refuses and is listed as
    missing; the surviving claim is reported as found."""
    ctx = value_ctx(not_found=[not_found(2022)])
    decision = run([value_claim()], ctx)
    refusal = assert_refused(decision)
    assert decision.kept == [value_claim()]
    assert len(refusal.missing) == 1 and "2022" in refusal.missing[0]


def test_unsupported_numeric_part_refuses() -> None:
    """``ctx.unsupported`` says the numeric part could not be compiled: never verified."""
    ctx = narrative_ctx(unsupported="no catalog metric for this measure")
    refusal = assert_refused(run(narrative_claims(), ctx))
    assert refusal.missing


def test_growth_needs_only_the_growth_claim() -> None:
    """No separate claim for the base year is required."""
    case = vc.growth_case()
    assert_answered(run([vc.edit(case.claim, primary=True)], case.ctx), "verified")


def test_growth_claim_dropped_refuses_even_with_a_value_kept() -> None:
    """An input value does not answer a growth question."""
    case = vc.growth_case()
    value = vc.expr_claim(
        "Apple's FY2023 revenue was $633.0 million.", f"f:{vc.REV23}", [vc.REV23], "633000000"
    )
    claims = [case.claim, vc.edit(value, id="c2", primary=True)]
    assert_refused(run(claims, case.ctx, drop={0}))


def test_compare_is_met_by_the_difference_alone() -> None:
    """The difference answers a comparison."""
    claim = compare_claims(primary=2)[2]
    assert_answered(run([claim], vc.compare_context()), "verified")


def test_compare_is_met_by_both_values_without_the_difference() -> None:
    """Both values also answer it, even when the non-primary difference claim is dropped."""
    decision = run(compare_claims(primary=0), vc.compare_context(), drop={2})
    assert_answered(decision, "partially_verified")


def test_compare_with_one_value_only_refuses() -> None:
    """One side of a comparison is not a comparison."""
    claims = compare_claims(primary=0)
    assert_refused(run(claims, vc.compare_context(), drop={1, 2}))


def test_compare_of_three_needs_all_three_values() -> None:
    """With no difference result, every value result is needed."""
    results = [*vc.compare_results()[:2], vc.result("307394000000", [801], cik=vc.ALPHABET)]
    ctx = vc.context(
        results, vc.plan(ciks=[vc.APPLE, vc.MICROSOFT, vc.ALPHABET], operation="compare")
    )
    third = vc.numeric_claim(
        "Alphabet's FY2023 revenue was $307.4 billion.", results[2], claim_id="c3"
    )
    claims = [*compare_claims(primary=0)[:2], third]
    assert_answered(run(claims, ctx), "verified")
    assert_refused(run(claims, ctx, drop={2}))


# --- sufficiency: the primary claim -------------------------------------------------------------


def test_dropped_primary_claim_refuses_even_if_slots_are_met() -> None:
    """Scoring reads the primary claim and must not fall back to another one."""
    decision = run(compare_claims(primary=2), vc.compare_context(), drop={2})
    assert_refused(decision)


def test_dropped_implicit_primary_claim_refuses() -> None:
    """With nothing marked, ``Answer.primary_claim()`` is the first numeric claim; dropping it
    refuses too."""
    apple, msft, diff = compare_claims()
    claims = [diff, apple, msft]
    assert Answer(answer_markdown="x", claims=claims).primary_claim() == diff
    assert_refused(run(claims, vc.compare_context(), drop={0}))


def test_dropped_primary_text_claim_refuses_despite_another_kept_one() -> None:
    """The primary rule applies to a marked text claim as well."""
    assert_refused(run(narrative_claims(primary=0), narrative_ctx(), drop={0}))


# --- sufficiency: text --------------------------------------------------------------------------


def test_narrative_needs_one_kept_text_claim() -> None:
    """One supported passage is enough (there is no per-topic field to check more)."""
    assert_answered(run(narrative_claims(), narrative_ctx(), drop={1}), "partially_verified")


def test_narrative_with_no_kept_text_claim_refuses() -> None:
    """Nothing supported: a refusal with one unmet need and an empty ``found``."""
    decision = run(narrative_claims(), narrative_ctx(), drop={0, 1})
    refusal = assert_refused(decision)
    assert decision.kept == [] and refusal.found == []
    assert len(refusal.missing) == 1


def test_hybrid_needs_both_the_number_and_a_text_claim() -> None:
    """A hybrid answer is refused when either half is gone."""
    claims = [value_claim(), narrative_claims()[0]]
    assert_answered(run(claims, hybrid_ctx()), "verified")
    assert_refused(run(claims, hybrid_ctx(), drop={1}))
    assert_refused(run(claims, hybrid_ctx(), drop={0}))


def test_hybrid_without_any_text_claim_refuses() -> None:
    """Text is wanted even if the composer wrote none."""
    assert_refused(run([value_claim()], hybrid_ctx()))


def test_text_only_numeric_plan_needs_a_kept_text_claim() -> None:
    """ADR-048: a numeric plan answered from text is sufficient only with a supported text
    claim."""
    ctx = vc.context([], vc.plan(metrics=[]), text_only=True)
    assert_answered(run(narrative_claims(), ctx), "verified")
    assert_refused(run(narrative_claims(), ctx, drop={0, 1}))


def test_plain_numeric_plan_wants_no_text_claim() -> None:
    """Text is not demanded of a numeric question."""
    assert_answered(run([value_claim()], value_ctx()), "verified")


# --- no claim, no answer ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ctx_factory",
    [narrative_ctx, value_ctx, rank_ctx, lambda: vc.context([])],
    ids=["narrative", "value", "universe_rank", "numeric_without_results"],
)
def test_answer_with_no_claims_and_no_composer_refusal_is_refused(ctx_factory) -> None:
    """Without a refusal at least one claim must be kept, so an answer that is only prose
    becomes a refusal."""
    decision = run([], ctx_factory())
    assert_refused(decision)
    assert decision.kept == []


def test_every_claim_dropped_is_refused_even_with_no_slots() -> None:
    """No slots plus no survivors is still not an answer."""
    claim = vc.text_claim("An unsupported statement.", "a quote of words")
    assert_refused(run([claim], vc.context([]), drop={0}))


# --- composer refusals (H0: none of the composer's words survive) -------------------------------

GUIDANCE = Refusal(
    reason="No FY2027 guidance appears in Apple's filings.",
    found=["Apple expects FY2027 revenue of $500 billion."],
    missing=["revenue guidance for fiscal 2027"],
)


def test_composer_refusal_stays_a_refusal_when_every_claim_passes() -> None:
    """Passing claims do not turn the composer's refusal into an answer. None of the composer's
    own words survive (decision H0): the reason is a template, ``missing`` comes from slots and
    ``found`` is rebuilt from the kept claims."""
    claim = value_claim()
    decision = run([claim], value_ctx(), refusal=GUIDANCE)
    refusal = assert_refused(decision)
    assert decision.kept == [claim]
    assert refusal.found == [claim.text]
    assert refusal.reason != GUIDANCE.reason
    dumped = refusal.model_dump_json()
    for composer_text in (GUIDANCE.reason, *GUIDANCE.missing, *GUIDANCE.found, "500"):
        assert composer_text not in dumped


def test_composer_refusal_without_claims_is_templated_too() -> None:
    """Even a clean refusal of an unanswerable question is rewritten: the unmet text need is
    what is missing."""
    decision = run([], narrative_ctx(), refusal=GUIDANCE)
    refusal = assert_refused(decision)
    assert refusal.found == []
    assert refusal.reason != GUIDANCE.reason
    assert refusal.missing and not set(GUIDANCE.missing) & set(refusal.missing)


def test_the_reason_does_not_depend_on_what_the_composer_said() -> None:
    """The template is a function of what is unmet, never of composer prose: two different
    composer refusals over the same claims give the same refusal."""
    first = Refusal(reason="Alpha wording about the question.", missing=["alpha detail"])
    second = Refusal(reason="Entirely different beta wording.", missing=["beta detail"])
    one = run([value_claim()], value_ctx(), refusal=first).refusal
    two = run([value_claim()], value_ctx(), refusal=second).refusal
    assert one is not None and one == two


def test_a_composer_reason_paraphrasing_a_dropped_claim_does_not_reach_the_output() -> None:
    """Required by H0: no number and no copied sentence, so only "never surface the composer's
    reason" stops the dropped claim's content."""
    bad = vc.text_claim("Margins fell because China demand collapsed.", "a quote of words",
                        claim_id="t9")  # fmt: skip
    composer = Refusal(
        reason="Weak Chinese demand hurt margins, but the forecast is not disclosed.",
        missing=["why softness in China persisted"],
    )
    decision = run([narrative_claims()[0], bad], narrative_ctx(), drop={1}, refusal=composer)
    dumped = assert_refused(decision).model_dump_json()
    assert "Chin" not in dumped and "demand" not in dumped.lower()


def test_composer_reason_with_an_unsupported_number_is_replaced() -> None:
    """A refusal is user-facing text: it may not smuggle in a figure no claim supports."""
    reason = "Guidance of $500 billion could not be confirmed."
    refusal = assert_refused(run([value_claim()], value_ctx(), refusal=Refusal(reason=reason)))
    assert refusal.reason != reason
    assert "500" not in refusal.model_dump_json()


def test_composer_missing_entry_with_an_unsupported_number_is_replaced() -> None:
    """The same rule for ``missing`` entries."""
    composer = Refusal(
        reason="The guidance is not in the filings.",
        missing=["confirmation of the 12.5% growth target"],
    )
    refusal = assert_refused(run([value_claim()], value_ctx(), refusal=composer))
    assert "12.5" not in refusal.model_dump_json()


def test_number_stated_only_by_a_dropped_claim_does_not_license_the_reason() -> None:
    """A dropped claim's figure is unsupported, so the composer's reason cannot repeat it."""
    reason = "Only the revenue of $383.3 billion could be located."
    decision = run([value_claim()], value_ctx(), drop={0}, refusal=Refusal(reason=reason))
    refusal = assert_refused(decision)
    assert "383.3" not in refusal.model_dump_json()


# --- nothing from a dropped claim ---------------------------------------------------------------


def test_dropped_claim_text_appears_nowhere_in_a_verifier_refusal() -> None:
    """The refusal is built from kept claims and templates only."""
    kept = value_claim()
    bad = vc.text_claim("Zebra demand collapsed by 91%.", "a quote of words", claim_id="t9")
    decision = run([kept, bad], hybrid_ctx(), drop={1})
    refusal = assert_refused(decision)
    dumped = refusal.model_dump_json()
    assert bad.text not in dumped
    assert "Zebra" not in dumped and "91%" not in dumped


def test_dropped_claim_text_is_removed_from_the_composers_refusal() -> None:
    """Even without a number, composer text that repeats a dropped claim must not survive in
    ``reason``, ``missing`` or ``found``."""
    bad = vc.text_claim("Zebra demand collapsed in China.", "a quote of words", claim_id="t9")
    composer = Refusal(reason=bad.text, found=[bad.text], missing=[bad.text])
    decision = run([narrative_claims()[0], bad], narrative_ctx(), drop={1}, refusal=composer)
    refusal = assert_refused(decision)
    assert bad.text not in refusal.model_dump_json()
    assert refusal.found == [NARRATIVE_A]


def test_found_lists_each_kept_claim_text_in_order() -> None:
    """``found`` is exactly the surviving claims' texts, text and numeric, in composer
    order."""
    claims = [narrative_claims()[0], value_claim(), narrative_claims()[1]]
    ctx = vc.context(
        [vc.result("383285000000", [vc.REV23])],
        vc.plan(intent="hybrid"),
        not_found=[not_found()],
    )
    decision = run(claims, ctx, drop={2})
    assert assert_refused(decision).found == [NARRATIVE_A, value_claim().text]
    assert decision.kept == claims[:2]


# --- invariant ----------------------------------------------------------------------------------


@given(
    st.sets(st.integers(0, 2)),
    st.booleans(),
    st.booleans(),
)
def test_status_kept_and_refusal_stay_consistent(
    drop: set[int], composer_refused: bool, primary: bool
) -> None:
    """For any pattern of drops over a trend: survivors are the kept verdicts in order; the
    answer is verified only with nothing dropped and no composer refusal; any dropped slot
    refuses; a refusal's ``found`` is the survivors' texts."""
    claims = trend_claims(primary=primary)
    refusal = Refusal(reason="The filings do not say why.") if composer_refused else None
    decision = run(claims, trend_ctx(), drop=drop, refusal=refusal)
    assert decision.kept == [c for i, c in enumerate(claims) if i not in drop]
    if drop or composer_refused:
        assert_refused(decision)
    else:
        assert_answered(decision, "verified")


# --- decision I: a numeric question with nothing computed ----------------------------------------


def test_numeric_plan_with_no_results_is_refused_despite_a_kept_text_claim() -> None:
    """Decision I: no compiled result, no NotFound, not a text-only fallback: nothing answers
    the numeric question, so a surviving passage does not make it verified."""
    claim = vc.text_claim("A supported aside.", "a quote of words")
    ctx = vc.context([], vc.plan(), text_only=False)
    decision = run([claim], ctx)
    refusal = assert_refused(decision)
    assert decision.kept == [claim] and refusal.missing


# --- decisions H and J ----------------------------------------------------------------------------


def test_a_dropped_claim_with_the_same_text_as_a_kept_one_leaves_found_intact() -> None:
    """Decision H: "no dropped text" means no text that only a dropped claim has."""
    kept = value_claim()
    twin = vc.expr_claim(kept.text, "f:999", [999], "383285000000", claim_id="c2")
    decision = run([kept, twin], value_ctx(not_found=[not_found()]), drop={1})
    assert assert_refused(decision).found == [kept.text]


def test_a_refusal_for_the_primary_claim_alone_names_its_metric_and_period() -> None:
    """Decision J: every slot is met (both values), but the dropped primary was the
    difference."""
    decision = run(compare_claims(primary=2), vc.compare_context(), drop={2})
    refusal = assert_refused(decision)
    entries = " | ".join(refusal.missing).lower()
    assert refusal.missing and "revenue" in entries and "2023" in entries


# --- the real policy through the pipeline (H0 and the pipeline's contract) ------------------------


def through_pipeline(
    claims: Sequence[Claim],
    ctx: VerifyContext,
    *,
    drop: Collection[int] = (),
    refusal: Refusal | None = None,
) -> Answer:
    """The real ``decide`` and ``apply`` inside ``verify_answer``, with the claim checks replaced:
    the claim at each position in ``drop`` fails. A decision that breaks the pipeline's
    contract (H0 among it) raises ``VerifierContractError``."""
    answer = Answer(answer_markdown="composer prose", claims=list(claims), refusal=refusal)
    failing = {id(answer.claims[i]) for i in drop}

    def check(claim: Claim, *_: object) -> CheckOutcome:
        first = "fail" if id(claim) in failing else "pass"
        if claim.kind == "text":
            checks = Checks(quote_substring=first, metadata_match="pass", number_in_text="pass")
        else:
            checks = Checks(provenance_exec=first, metadata_match="pass", number_in_text="pass")
        return CheckOutcome(checks=checks, reason="" if first == "pass" else "A check failed.")

    verifier = Verifier(check_numeric=check, check_text=check, decide=decide, apply=apply)
    verified, _ = verify_answer(answer, Evidence(), ctx, CFG, verifier)
    return verified


def test_through_the_pipeline_no_composer_wording_reaches_the_answer() -> None:
    bad = vc.text_claim("Margins fell because China demand collapsed.", "a quote of words",
                        claim_id="t9")  # fmt: skip
    composer = Refusal(
        reason="Weak Chinese demand hurt margins, but the forecast is not disclosed.",
        found=["Softness in China persisted."],
        missing=["why demand in China softened"],
    )
    out = through_pipeline(
        [narrative_claims()[0], bad], narrative_ctx(), drop={1}, refusal=composer
    )
    dumped = out.model_dump_json()  # the markdown and the refusal
    assert out.refusal is not None
    assert "Chin" not in dumped and "demand" not in dumped.lower()
    assert "composer prose" not in dumped
    assert out.answer_markdown.startswith(out.refusal.reason)


@pytest.mark.parametrize(
    ("build", "drop", "composer_refused", "status"),
    [
        (lambda: (trend_claims(), trend_ctx()), set(), False, "answered"),
        (lambda: (trend_claims(), trend_ctx()), {2}, False, "refused"),
        (lambda: (trend_claims(), trend_ctx()), set(), True, "refused"),
        (lambda: (narrative_claims(), narrative_ctx()), {0, 1}, False, "refused"),
        (lambda: ([value_claim()], value_ctx(not_found=[not_found()])), set(), False,
         "refused"),
        (lambda: (compare_claims(primary=2), vc.compare_context()), {2}, False, "refused"),
        (lambda: (rank_claims(), rank_ctx()), set(), False, "answered"),
    ],
    ids=["verified", "slot_dropped", "composer_refusal", "no_text_left", "not_found",
         "primary_dropped", "universe_rank"],
)  # fmt: skip
def test_the_policy_satisfies_the_pipelines_contract(
    build, drop: set[int], composer_refused: bool, status: str
) -> None:
    claims, ctx = build()
    composer = Refusal(reason="The filings do not say.") if composer_refused else None
    out = through_pipeline(claims, ctx, drop=drop, refusal=composer)
    assert (out.refusal is None) is (status == "answered")
    assert out.claims == [c for i, c in enumerate(claims) if i not in drop]
