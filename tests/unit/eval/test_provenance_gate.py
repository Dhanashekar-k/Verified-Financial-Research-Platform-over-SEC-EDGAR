"""The harness asserts every claim carries provenance (DoD M5): empty provenance fails the run."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.engine.answer import Answer, ChunkProvenance, Claim, ExprProvenance
from eval.run import provenance_breaches, provenance_gaps


def expr_claim(ids: list[int], cid: str = "c1") -> Claim:
    # model_construct: the validators would refuse an empty fact_ids list for a real expr, which
    # is exactly the shape the gate must catch if anything ever slips past them.
    prov = ExprProvenance.model_construct(type="expr", expr="1", fact_ids=ids)
    return Claim.model_construct(
        id=cid,
        kind="numeric",
        text="t",
        value=Decimal(1),
        unit=None,
        display=None,
        primary=False,
        provenance=prov,
    )


def chunk_claim(chunk_id: str, quote: str, cid: str = "c2") -> Claim:
    prov = ChunkProvenance(type="chunk", chunk_id=chunk_id, quote=quote)
    return Claim(id=cid, kind="text", text="t", provenance=prov)


def test_well_formed_claims_have_no_gaps() -> None:
    good = Answer(
        answer_markdown="m",
        claims=[
            Claim(
                id="c0",
                kind="numeric",
                text="t",
                value=Decimal(1),
                provenance=ExprProvenance(type="expr", expr="f:1", fact_ids=[1]),
            ),
            chunk_claim("a#1#0", "quote"),
        ],
    )
    assert provenance_gaps(good) == []
    assert provenance_gaps(Answer(answer_markdown="refusal only")) == []


def test_empty_provenance_is_a_gap() -> None:
    a = Answer.model_construct(
        answer_markdown="m",
        claims=[expr_claim([]), chunk_claim("", "q", "c3"), chunk_claim("a#1#0", "   ", "c4")],
        refusal=None,
    )
    assert provenance_gaps(a) == [
        "c1: expr provenance without fact ids",
        "c3: chunk provenance without a chunk id or quote",
        "c4: chunk provenance without a chunk id or quote",
    ]


def test_breaches_are_keyed_by_question_and_skip_unanswered() -> None:
    def outcome(qid: str, answer: Answer | None) -> Any:
        return SimpleNamespace(row=SimpleNamespace(id=qid), answer=answer)

    bad = Answer.model_construct(answer_markdown="m", claims=[expr_claim([])], refusal=None)
    ok = Answer(answer_markdown="m")
    out = provenance_breaches([outcome("q1", bad), outcome("q2", ok), outcome("q3", None)])
    assert list(out) == ["q1"]


def test_strict_mode_flags_prose_with_no_claim_and_no_refusal() -> None:
    from app.engine.answer import Refusal

    prose = Answer(answer_markdown="Apple earned a lot.")
    assert provenance_gaps(prose) == [], "the baselines are not held to the engine's contract"
    assert provenance_gaps(prose, strict=True) == ["answer has prose but no claim and no refusal"]
    refused = Answer(answer_markdown="No.", refusal=Refusal(reason="r"))
    assert provenance_gaps(refused, strict=True) == []
    assert provenance_gaps(Answer(answer_markdown=""), strict=True) == []


def test_breaches_are_strict_only_when_asked() -> None:
    o = SimpleNamespace(row=SimpleNamespace(id="q1"), answer=Answer(answer_markdown="prose"))
    assert provenance_breaches([o]) == {}  # type: ignore[list-item]
    assert list(provenance_breaches([o], strict=True)) == ["q1"]  # type: ignore[list-item]


# --- numeric claims must cite what the compiler returned ---------------------------------------

REAL, OTHER, LATEST, INVENTED = -8138443950304199831, 222, -9, 123456789


def numeric(ids: list[int], cid: str = "c1", expr: str | None = None) -> Claim:
    expr = expr or " - ".join(f"f:{i}" for i in ids)
    return Claim(
        id=cid,
        kind="numeric",
        text="t",
        value=Decimal(1),
        provenance=ExprProvenance(type="expr", expr=expr, fact_ids=ids),
    )


def gaps(claim: Claim, compiled: set[int]) -> list[str]:
    return provenance_gaps(Answer(answer_markdown="m", claims=[claim]), compiled=compiled)


def test_an_invented_fact_id_is_rejected() -> None:
    assert gaps(numeric([INVENTED]), {REAL}) == [
        f"c1: cites fact ids the compiler did not return: [{INVENTED}]"
    ]


def test_one_invented_id_among_real_ones_is_enough() -> None:
    (gap,) = gaps(numeric([REAL, INVENTED]), {REAL, OTHER})
    assert str(INVENTED) in gap and str(REAL) not in gap


def test_ids_the_compiler_returned_pass() -> None:
    assert gaps(numeric([REAL, OTHER]), {REAL, OTHER, LATEST}) == []


def test_with_nothing_compiled_every_numeric_claim_is_rejected() -> None:
    assert gaps(numeric([REAL]), set())


def test_a_text_claim_cannot_smuggle_an_invented_id_through_expr_provenance() -> None:
    claim = Claim(
        id="c1",
        kind="text",
        text="t",
        provenance=ExprProvenance(type="expr", expr=f"f:{INVENTED}", fact_ids=[INVENTED]),
    )
    assert gaps(claim, {REAL})


def test_a_numeric_claim_resting_on_a_passage_is_rejected() -> None:
    """It has no fact ids, so a bare subset test would pass it."""
    claim = Claim(
        id="c1",
        kind="numeric",
        text="t",
        value=Decimal(1),
        provenance=ChunkProvenance(type="chunk", chunk_id="a#1#0", quote="q"),
    )
    assert gaps(claim, {REAL}) == ["c1: a numeric claim without fact-id provenance"]


def test_without_a_compiled_set_the_check_does_not_apply() -> None:
    assert provenance_gaps(Answer(answer_markdown="m", claims=[numeric([INVENTED])])) == []


def test_citable_ids_are_the_results_ids_and_the_restated_ids_the_composer_was_shown() -> None:
    from app.engine.compose.composer import citable_fact_ids
    from app.engine.plan.schema import Note, NumericResult

    def result(unit: str, ids: list[int], latest: int) -> NumericResult:
        return NumericResult(
            metric="net_income" if unit == "USD" else "net_margin",
            cik=1,
            fiscal_year=2023,
            fiscal_period="FY",
            operation="value",
            value=Decimal(1),
            unit=unit,  # type: ignore[arg-type]
            expr=" / ".join(f"f:{i}" for i in ids),
            fact_ids=ids,
            kind="reported" if len(ids) == 1 else "derived",
            notes=[Note(code="restated", text="t", latest_value=Decimal(2), latest_fact_id=latest)],
        )

    assert citable_fact_ids([result("USD", [REAL], LATEST)]) == {REAL, LATEST}
    # on a ratio the composer is not shown the restated id (no unit to show it in)
    assert citable_fact_ids([result("ratio", [REAL, OTHER], LATEST)]) == {REAL, OTHER}
    assert citable_fact_ids([]) == set()


class _Cites:
    """An engine-like system: reports what it compiled, answers citing ``cited``."""

    name = "engine"
    retrieves = False

    def __init__(self, cited: int, compiled: list[int] | None) -> None:
        self.cited, self.compiled = cited, compiled

    def answer(self, question: str, cfg: Any) -> Answer:
        from eval.systems import record_compiled_facts

        if self.compiled is not None:
            record_compiled_facts(self.compiled)
        return Answer(answer_markdown="m", claims=[numeric([self.cited])])


def run_one(system: _Cites) -> Any:
    from app.llm.client import LLMClient
    from app.llm.types import LLMMode
    from eval import run
    from eval.schemas import UnanswerableRow
    from eval.systems import AccountingLLM, EvalCfg

    row = UnanswerableRow.model_construct(id="u-1", question="q")
    client = LLMClient(mode=LLMMode.stub)
    cfg = EvalCfg(llm=AccountingLLM(client))
    outcomes, _ = run.run_questions(system, cfg, None, [("unanswerable", row, "dev")], {}, client)
    return outcomes


def test_the_harness_collects_the_compiled_ids_per_answer_and_rejects_an_invented_one() -> None:
    bad = run_one(_Cites(INVENTED, [REAL]))
    assert bad[0].compiled_facts == {REAL}
    assert list(provenance_breaches(bad, strict=True)) == ["u-1"]
    assert provenance_breaches(run_one(_Cites(REAL, [REAL])), strict=True) == {}


def test_a_system_that_reports_nothing_fails_closed_under_the_strict_gate() -> None:
    assert list(provenance_breaches(run_one(_Cites(REAL, None)), strict=True)) == ["u-1"]


def test_ids_do_not_leak_from_one_question_to_the_next() -> None:
    first, second = run_one(_Cites(REAL, [REAL])), run_one(_Cites(REAL, None))
    assert first[0].compiled_facts == {REAL} and second[0].compiled_facts == set()


def test_the_results_file_does_not_gain_a_field() -> None:
    from eval import run

    outcomes = run_one(_Cites(REAL, [REAL]))
    per = run.PerQuestion.model_fields
    assert "compiled_facts" not in per
    assert "compiled_facts" not in outcomes[0].scores
