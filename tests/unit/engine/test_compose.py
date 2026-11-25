"""Composer inputs, display formatting and the deterministic grounding checks (M05 section 3.7)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from app.engine.answer import Answer, ChunkProvenance, Claim, ExprProvenance
from app.engine.compose.composer import (
    COMPOSER_ROLE,
    COMPOSER_SYSTEM,
    build_messages,
    compose,
    result_view,
)
from app.engine.compose.format import format_value
from app.engine.compose.grounding import check_grounding, check_markdown, numbers_in
from app.engine.plan.schema import Note, NotFound, NumericResult, PeriodRef, QueryPlan
from app.engine.retrieval.query import RankedChunk
from app.llm.types import Usage

D = Decimal
NAMES = {320193: "Apple Inc.", 1286681: "Domino's"}


def res(
    value: str = "96995000000", expr: str = "f:111", ids: list[int] | None = None, **kw: Any
) -> NumericResult:
    base: dict[str, Any] = {
        "metric": "net_income",
        "cik": 320193,
        "fiscal_year": 2023,
        "fiscal_period": "FY",
        "operation": "value",
        "value": D(value),
        "unit": "USD",
        "expr": expr,
        "fact_ids": ids or [111],
        "kind": "reported",
    }
    base.update(kw)
    return NumericResult(**base)


# --- format_value ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "unit", "shown"),
    [
        ("383285000000", "USD", "$383.3 billion"),
        ("11800000", "USD", "$11.8 million"),
        ("1234", "USD", "$1,234"),
        ("-1200000", "USD", "-$1.2 million"),
        ("999960000", "USD", "$1.0 billion"),  # 999.96 million rounds up, so it moves a scale
        ("2500000000000", "USD", "$2.5 trillion"),
        ("0", "USD", "$0"),
        ("15744231000", "shares", "15.7 billion shares"),
        ("6.13", "USD/shares", "$6.13 per share"),
        ("-0.456", "USD/shares", "-$0.46 per share"),
        ("0.2982", "ratio", "29.8%"),
        ("-0.4166666667", "ratio", "-41.7%"),
        ("1.5", "ratio", "150.0%"),
    ],
)
def test_format_value(value: str, unit: str, shown: str) -> None:
    assert format_value(D(value), unit) == shown


def test_format_value_rejects_an_unknown_unit() -> None:
    with pytest.raises(ValueError, match="unknown unit"):
        format_value(D(1), "EUR")


def test_rounding_is_half_up_on_the_decimal_not_a_float() -> None:
    """The reader's rule (a half rounds up), applied to the exact Decimal."""
    assert format_value(D("0.0125"), "ratio") == "1.3%"
    assert format_value(D("0.0135"), "ratio") == "1.4%"
    assert format_value(D("1250000"), "USD") == "$1.3 million"
    assert format_value(D("383250000000"), "USD") == "$383.3 billion"
    assert format_value(D("-1250000"), "USD") == "-$1.3 million"
    assert format_value(D("0.005"), "USD/shares") == "$0.01 per share"


def test_a_value_that_rounds_to_zero_has_no_minus_sign() -> None:
    assert format_value(D("-0.0004"), "ratio") == "0.0%"
    assert format_value(D("-0.4"), "USD") == "$0"
    assert format_value(D("-0.004"), "USD/shares") == "$0.00 per share"
    assert format_value(D("-0.0006"), "ratio") == "-0.1%"


# --- inputs ------------------------------------------------------------------------------------


def test_result_view_carries_the_provenance_and_display_verbatim() -> None:
    v = result_view(res(), NAMES, "r1")
    assert v["provenance"] == {"type": "expr", "expr": "f:111", "fact_ids": [111]}
    assert v["value"] == "96995000000" and v["display"] == "$97.0 billion"
    assert v["company"] == "Apple Inc." and v["period"] == "FY2023" and v["kind"] == "numeric"


def test_result_view_for_derived_difference_restated_and_quarter() -> None:
    d = res(
        operation="difference",
        compare_cik=1286681,
        expr="f:1 - f:2",
        ids=[1, 2],
        kind="derived",
        fiscal_period="Q2",
    )
    v = result_view(d, NAMES, "r2")
    assert v["kind"] == "numeric_derived" and v["minus_company"] == "Domino's"
    assert v["period"] == "Q2 FY2023"
    r = res(notes=[Note(code="restated", text="restated", latest_value=D("5"), latest_fact_id=-9)])
    note = result_view(r, NAMES, "r3")["notes"][0]
    assert note["must_mention"] is True and note["latest_display"] == "$5"
    assert note["latest_provenance"] == {"type": "expr", "expr": "f:-9", "fact_ids": [-9]}


def test_messages_hold_question_numbers_not_found_and_passages() -> None:
    chunk = RankedChunk("a#7#0001", 1, 0.9, "7", "Revenue rose because of services.")
    nf = NotFound(
        metric="r_and_d", cik=320193, fiscal_year=2023, fiscal_period="FY", reason="no_fact"
    )
    (msg,) = build_messages(
        "What was net income?",
        QueryPlan(intent="hybrid"),
        [res()],
        [nf],
        [chunk],
        NAMES,
        assumed_period="No period was given; the latest fiscal year (FY2023) is used.",
    )
    text = msg["content"]
    assert msg["role"] == "user"
    for part in (
        "QUESTION\nWhat was net income?",
        "PERIOD NOTE",
        "NUMERIC RESULTS",
        "NOT FOUND",
        "PASSAGES",
        "[chunk_id: a#7#0001 | section: 7]",
        "Revenue rose",
    ):
        assert part in text
    payload = text.split("NUMERIC RESULTS\n")[1].split("\n\nNOT FOUND")[0]
    assert json.loads(payload)[0]["provenance"]["fact_ids"] == [111]


def test_empty_sections_are_left_out() -> None:
    (msg,) = build_messages("q", QueryPlan(intent="narrative"), [], [], [], NAMES)
    assert msg["content"] == "QUESTION\nq"


def test_an_unsupported_numeric_part_reaches_the_composer_as_not_found() -> None:
    """A hybrid question whose numeric half could not be computed must say so to the model, or it
    answers from the passages alone with no hint that a number is missing."""
    chunk = RankedChunk("a#7#0001", 1, 0.9, "7", "Risk text.")
    (msg,) = build_messages(
        "q",
        QueryPlan(intent="hybrid"),
        [],
        [],
        [chunk],
        NAMES,
        unsupported="no operation on a numeric plan",
    )
    text = msg["content"]
    assert (
        "NOT FOUND" in text
        and "unsupported_plan" in text
        and "no operation on a numeric plan" in text
    )
    (msg2,) = build_messages("q", QueryPlan(intent="hybrid"), [], [], [chunk], NAMES)
    assert "NOT FOUND" not in msg2["content"]


def test_the_prompt_forbids_arithmetic_and_demands_provenance() -> None:
    flat = " ".join(COMPOSER_SYSTEM.split())
    for needle in (
        "DO NOT DO ARITHMETIC",
        "exactly one provenance",
        "contiguous substring",
        "`refusal`",
        "latest_provenance",
        "PERIOD NOTE",
    ):
        assert needle in flat, needle


class FakeLLM:
    def __init__(self, answer: Answer) -> None:
        self.answer, self.calls = answer, []  # type: ignore[var-annotated]

    def structured(
        self,
        role: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        *,
        max_tokens: int = 2048,
    ) -> tuple[Any, Usage]:
        self.calls.append((role, system, messages, schema, max_tokens))
        return self.answer, Usage.model_construct()


def test_compose_calls_the_composer_role_with_the_answer_schema() -> None:
    llm = FakeLLM(Answer(answer_markdown="x"))
    got, _ = compose("q", QueryPlan(intent="numeric"), [res()], [], [], NAMES, llm)
    role, system, _, schema, _ = llm.calls[0]
    assert (role, system, schema) == (
        COMPOSER_ROLE,
        COMPOSER_SYSTEM,
        Answer,
    ) and got.answer_markdown == "x"


# --- grounding ---------------------------------------------------------------------------------

TEXT = "Net sales rose 8% in fiscal 2023, driven by  Services\nrevenue of $85,200 million."


def claim(kind: str, prov: Any, **kw: Any) -> Claim:
    return Claim(id="c1", kind=kind, text=kw.pop("text", "x"), provenance=prov, **kw)  # type: ignore[arg-type]


def expr(e: str, ids: list[int]) -> ExprProvenance:
    return ExprProvenance(type="expr", expr=e, fact_ids=ids)


def rules(vs: list[Any]) -> list[str]:
    return [v.rule for v in vs]


def test_a_copied_numeric_claim_is_grounded() -> None:
    a = Answer(
        answer_markdown="m", claims=[claim("numeric", expr("f:111", [111]), value=D("96995000000"))]
    )
    assert check_grounding(a, [res()], {}) == []


def test_an_ids_order_does_not_matter_but_the_expression_does() -> None:
    r = res(operation="difference", compare_cik=1, expr="f:1 - f:2", ids=[1, 2], kind="derived")
    ok = claim("numeric_derived", expr("f:1 - f:2", [2, 1]), value=r.value)
    assert check_grounding(Answer(answer_markdown="m", claims=[ok]), [r], {}) == []
    swapped = claim("numeric_derived", expr("f:2 - f:1", [1, 2]), value=r.value)
    assert rules(check_grounding(Answer(answer_markdown="m", claims=[swapped]), [r], {})) == [
        "not_a_provided_result"
    ]


def test_a_number_the_composer_computed_is_flagged() -> None:
    """The pitfall M05 section 7 names: a 'helpful' ratio the model worked out itself."""
    computed = claim("numeric_derived", expr("f:111 / f:222", [111, 222]), value=D("0.2982"))
    assert rules(check_grounding(Answer(answer_markdown="m", claims=[computed]), [res()], {})) == [
        "not_a_provided_result"
    ]
    changed = claim("numeric", expr("f:111", [111]), value=D("97000000000"))  # rounded by the model
    assert rules(check_grounding(Answer(answer_markdown="m", claims=[changed]), [res()], {})) == [
        "value_changed"
    ]
    novalue = claim("numeric", expr("f:111", [111]))
    assert rules(check_grounding(Answer(answer_markdown="m", claims=[novalue]), [res()], {})) == [
        "value_changed"
    ]


def test_the_restated_latest_value_is_citable_through_its_note() -> None:
    r = res(notes=[Note(code="restated", text="t", latest_value=D("5"), latest_fact_id=-9)])
    ok = claim("numeric", expr("f:-9", [-9]), value=D("5"))
    assert check_grounding(Answer(answer_markdown="m", claims=[ok]), [r], {}) == []
    wrong = claim("numeric", expr("f:-9", [-9]), value=D("6"))
    assert rules(check_grounding(Answer(answer_markdown="m", claims=[wrong]), [r], {})) == [
        "value_changed"
    ]


def test_kind_and_provenance_type_must_agree() -> None:
    num_with_chunk = claim(
        "numeric", ChunkProvenance(type="chunk", chunk_id="c", quote="q"), value=D(1)
    )
    text_with_expr = claim("text", expr("f:111", [111]))
    out = check_grounding(
        Answer(answer_markdown="m", claims=[num_with_chunk, text_with_expr]), [res()], {"c": "q"}
    )
    assert rules(out) == ["numeric_needs_expr", "text_needs_chunk"]


def test_text_claims_need_a_real_chunk_and_an_exact_quote() -> None:
    passages = {"a#7#0001": TEXT}
    ok = claim(
        "text",
        ChunkProvenance(type="chunk", chunk_id="a#7#0001", quote="driven by Services revenue"),
        text="Growth was driven by Services revenue.",
    )
    assert (
        check_grounding(Answer(answer_markdown="m", claims=[ok]), [], passages) == []
    )  # whitespace-normalized
    bad = [
        claim("text", ChunkProvenance(type="chunk", chunk_id="zzz", quote="x")),
        claim("text", ChunkProvenance(type="chunk", chunk_id="a#7#0001", quote="sales fell")),
        claim("text", ChunkProvenance(type="chunk", chunk_id="a#7#0001", quote="  ")),
    ]
    assert rules(check_grounding(Answer(answer_markdown="m", claims=bad), [], passages)) == [
        "unknown_chunk",
        "quote_not_in_chunk",
        "quote_not_in_chunk",
    ]


def test_a_number_in_a_text_claim_must_be_in_its_quote() -> None:
    p = {"a#7#0001": TEXT}
    quote = "Net sales rose 8% in fiscal 2023"
    ok = claim(
        "text",
        ChunkProvenance(type="chunk", chunk_id="a#7#0001", quote=quote),
        text="Sales rose 8% in fiscal 2023.",
    )
    assert check_grounding(Answer(answer_markdown="m", claims=[ok]), [], p) == []
    stray = claim(
        "text",
        ChunkProvenance(type="chunk", chunk_id="a#7#0001", quote=quote),
        text="Sales rose 9% in fiscal 2023.",
    )
    (v,) = check_grounding(Answer(answer_markdown="m", claims=[stray]), [], p)
    assert v.rule == "number_not_in_quote" and v.detail == "9"
    thousands = claim(
        "text",
        ChunkProvenance(type="chunk", chunk_id="a#7#0001", quote="revenue of $85,200 million"),
        text="Services revenue was $85200 million.",
    )
    assert check_grounding(Answer(answer_markdown="m", claims=[thousands]), [], p) == []


def test_numbers_in() -> None:
    assert numbers_in("rose 8% to $1,234.5 million; see Item 7.") == {"8", "1234.5", "7"}
    assert numbers_in("no digits") == set()


# --- grounding: the wording of a numeric claim, and the prose ----------------------------------


def numeric_claim(text: str, display: str | None = "$97.0 billion", **kw: Any) -> Claim:
    return Claim(
        id="c1",
        kind="numeric",
        text=text,
        value=D("96995000000"),
        unit="USD",
        display=display,
        provenance=expr("f:111", [111]),
        **kw,
    )


def test_a_growth_rate_tucked_into_a_numeric_claims_wording_is_caught() -> None:
    """Right expr and value, but the text computes a percentage the model worked out itself."""
    bad = numeric_claim("Net income grew 7.8% to $97.0 billion in fiscal 2023.")
    (v,) = check_grounding(Answer(answer_markdown="m", claims=[bad]), [res()], {})
    assert v.rule == "number_not_in_result" and v.detail == "7.8"
    ok = numeric_claim("Net income was $97.0 billion in fiscal 2023.")
    assert check_grounding(Answer(answer_markdown="m", claims=[ok]), [res()], {}) == []


def test_the_display_string_must_be_the_results_own() -> None:
    bad = numeric_claim("Net income was about $97 billion.", display="$97 billion")
    # the re-rounded "97" is both a changed display and a number the result does not have
    assert rules(check_grounding(Answer(answer_markdown="m", claims=[bad]), [res()], {})) == [
        "display_changed",
        "number_not_in_result",
    ]
    assert (
        check_grounding(
            Answer(answer_markdown="m", claims=[numeric_claim("x", display=None)]), [res()], {}
        )
        == []
    )


def test_a_quarters_digit_and_the_period_are_allowed_in_the_wording() -> None:
    q = res(fiscal_period="Q2", fiscal_year=2024)
    claim_q = numeric_claim("Q2 net income in fiscal 2024 was $97.0 billion.")
    assert check_grounding(Answer(answer_markdown="m", claims=[claim_q]), [q], {}) == []


def test_numbers_in_the_prose_must_come_from_a_claim_or_a_result() -> None:
    c = numeric_claim("Net income was $97.0 billion.")
    ok = Answer(
        answer_markdown="Apple's FY2023 net income was $97.0 billion [1] (Item 7, 10-K).",
        claims=[c],
    )
    assert check_markdown(ok, [res()], {}) == []
    sneaky = Answer(
        answer_markdown="Net income was $97.0 billion [1], up 7.8% on the year.", claims=[c]
    )
    (v,) = check_markdown(sneaky, [res()], {})
    assert v.rule == "number_not_supported" and v.detail == "7.8"


def test_a_figure_quoted_from_a_passage_is_supported_in_the_prose() -> None:
    quote = "Services revenue of $85,200 million"
    c = Claim(
        id="c1",
        kind="text",
        text="Services revenue was $85,200 million.",
        provenance=ChunkProvenance(type="chunk", chunk_id="a#7#0001", quote=quote),
    )
    ans = Answer(answer_markdown="Services revenue was $85,200 million [1].", claims=[c])
    assert check_markdown(ans, [], {"a#7#0001": quote}) == []


# --- review findings, 2026-11-25 (ADR-047) -----------------------------------------------------

RESTATED_INPUT = Note(
    code="restated",
    text="Revenue for FY2023 was later restated; the figure used is as originally filed.",
    latest_value=D("800000000"),
    latest_fact_id=-9,
)


def _ratio_result() -> NumericResult:
    return res(
        "0.2",
        "f:1 / f:2",
        [1, 2],
        metric="net_margin",
        unit="ratio",
        kind="derived",
        notes=[RESTATED_INPUT],
    )


def test_a_restated_input_of_a_ratio_is_never_shown_in_the_ratios_unit() -> None:
    """800,000,000 dollars formatted as a ratio read '80000000000.0%'."""
    (note,) = result_view(_ratio_result(), NAMES, "r1")["notes"]
    assert note["must_mention"] is True and "Revenue" in note["text"]
    assert not {"latest_value", "latest_display", "latest_provenance"} & set(note)


def test_grounding_does_not_admit_a_ratios_restated_input() -> None:
    answer = Answer(
        answer_markdown="Margin was 20.0%, later 80000000000.0%.",
        claims=[
            Claim(
                id="c1",
                kind="numeric",
                text="Later restated to 800000000.",
                value=D("800000000"),
                provenance=ExprProvenance(type="expr", expr="f:-9", fact_ids=[-9]),
            )
        ],
    )
    result = _ratio_result()
    assert [v.rule for v in check_grounding(answer, [result], {})] == ["not_a_provided_result"]
    assert [v.rule for v in check_markdown(answer, [result], {})] == ["number_not_supported"]


@pytest.mark.parametrize(
    ("years", "base"), [([2023], "FY2022"), ([2019, 2023], "FY2019"), ([2023, 2019], "FY2019")]
)
def test_a_growth_result_says_what_it_is_measured_from(years: list[int], base: str) -> None:
    """Without it, four-year growth labelled 'FY2023' reads as year over year."""
    growth = res("2", "(f:1 - f:2) / f:2", [1, 2], operation="growth", unit="ratio", kind="derived")
    plan = QueryPlan(
        intent="numeric",
        metrics=["net_income"],
        operation="growth",
        periods=[PeriodRef(fiscal_year=y) for y in years],
    )
    content = build_messages("q", plan, [growth], [], [], NAMES)[0]["content"]
    assert f'"growth_from": "{base}"' in content
    said = Answer(answer_markdown=f"Net income grew 200.0% from {base} to FY2023.", claims=[])
    assert check_markdown(said, [growth], {}, set(years)) == []
