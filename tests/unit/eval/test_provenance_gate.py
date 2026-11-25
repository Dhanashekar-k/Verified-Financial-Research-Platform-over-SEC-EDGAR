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
