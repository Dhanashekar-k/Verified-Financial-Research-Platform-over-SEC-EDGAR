"""Verifier orchestration (ADR-049). **Orchestration tests:** the four owned decisions are
injected fakes, so these say nothing about what the real checks accept."""

from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.engine.answer import Answer, ChunkProvenance, Claim, ExprProvenance, Refusal
from app.engine.entities.resolve_company import CompanyRecord
from app.engine.plan.schema import CompanyRef, Note, NumericResult, PeriodRef, QueryPlan
from app.engine.verify import numbers, numeric, policy, quotes
from app.engine.verify.evidence import load_evidence
from app.engine.verify.models import (
    WITHHELD_REASON,
    CheckOutcome,
    Checks,
    Evidence,
    PolicyDecision,
    VerifierResult,
    VerifyContext,
    allowed_ciks,
    company_names,
    without_composer_wording,
)
from app.engine.verify.pipeline import (
    VerifierContractError,
    VerifierNotImplemented,
    require_implemented,
    verify_answer,
)
from app.engine.verify.render import compiler_notes, period_line, render
from tests.support.fake_verifier import FakeVerifier


def numeric_claim(cid: str, text: str, expr: str = "f:1", ids: list[int] | None = None) -> Claim:
    return Claim(
        id=cid,
        kind="numeric",
        text=text,
        value=Decimal(5),
        provenance=ExprProvenance(type="expr", expr=expr, fact_ids=ids or [1]),
    )


def text_claim(cid: str, text: str) -> Claim:
    return Claim(
        id=cid,
        kind="text",
        text=text,
        provenance=ChunkProvenance(type="chunk", chunk_id="a#1#0001", quote="q"),
    )


def result(**over: Any) -> NumericResult:
    base: dict[str, Any] = {
        "metric": "net_income",
        "cik": 1,
        "fiscal_year": 2023,
        "fiscal_period": "FY",
        "operation": "value",
        "value": Decimal(5),
        "unit": "USD",
        "expr": "f:1",
        "fact_ids": [1],
        "kind": "reported",
    }
    return NumericResult.model_validate(base | over)


def context(**over: Any) -> VerifyContext:
    plan = over.pop("plan", QueryPlan(intent="hybrid", periods=[PeriodRef(fiscal_year=2023)]))
    return VerifyContext(plan=plan, **over)


TWO = Answer(
    answer_markdown="Composer prose with a dangling sentence about 99 [1][2].",
    claims=[numeric_claim("c1", "Net income was $5."), text_claim("c2", "Costs rose.")],
)


def test_numeric_claims_go_to_the_numeric_check_and_text_claims_to_the_quote_check() -> None:
    fake = FakeVerifier()
    verify_answer(TWO, Evidence(), context(), verifier=fake.verifier())
    assert fake.numeric_calls == ["c1"] and fake.text_calls == ["c2"]


def test_the_composer_prose_is_never_shown_and_a_dropped_claim_leaves_nothing() -> None:
    fake = FakeVerifier(fail_ids={"c2"})
    answer, report = verify_answer(TWO, Evidence(), context(), verifier=fake.verifier())
    assert answer.answer_markdown == "Net income was $5. [1]"
    assert [c.id for c in answer.claims] == ["c1"]
    assert (report.status, report.claims_total, report.claims_verified, report.claims_dropped) == (
        "partially_verified",
        2,
        1,
        1,
    )
    assert report.dropped_by_check == {"quote_substring": 1}


def test_all_dropped_becomes_a_refusal_with_no_claim_text() -> None:
    fake = FakeVerifier(fail_ids={"c1", "c2"})
    answer, report = verify_answer(TWO, Evidence(), context(), verifier=fake.verifier())
    assert answer.refusal is not None and answer.claims == []
    assert "Costs rose" not in answer.model_dump_json()
    assert "Net income" not in answer.model_dump_json()
    assert report.status == "refused"
    assert report.dropped_by_check == {"provenance_exec": 1, "quote_substring": 1}


def test_a_refusal_lists_only_what_survives() -> None:
    answer = TWO.model_copy(update={"refusal": Refusal(reason="Only part is known.")})
    out, _ = verify_answer(answer, Evidence(), context(), verifier=FakeVerifier().verifier())
    assert out.answer_markdown.startswith("fake refusal")  # the policy's own reason
    assert "Only part is known." not in out.model_dump_json()
    assert "- Net income was $5. [1]" in out.answer_markdown
    assert out.refusal and out.refusal.found == ["Net income was $5.", "Costs rose."]


# --- the contract the policy is held to ------------------------------------------------------


def breach(apply: Any = None, decide: Any = None, answer: Answer = TWO, fail: str = "") -> None:
    fake = FakeVerifier(fail_ids={fail} if fail else ())
    v = fake.verifier()
    v = type(v)(v.check_numeric, v.check_text, decide or v.decide, apply or v.apply)
    verify_answer(answer, Evidence(), context(), verifier=v)


def keep_everything(claim: Claim, outcome: CheckOutcome, cfg: Any) -> VerifierResult:
    return VerifierResult(
        claim_id=claim.id, status="supported", checks=outcome.checks, action="keep"
    )


def test_a_failed_check_can_never_be_kept() -> None:
    with pytest.raises(VerifierContractError, match="failed a check"):
        breach(decide=keep_everything, fail="c1")


@pytest.mark.parametrize(
    "decision",
    [
        PolicyDecision(kept=[], status="verified"),  # kept does not match the results
        PolicyDecision(kept=list(reversed(TWO.claims)), status="verified"),  # reordered
        PolicyDecision(
            kept=[TWO.claims[0].model_copy(update={"text": "rewritten"}), TWO.claims[1]],
            status="verified",
        ),
        PolicyDecision(
            kept=list(TWO.claims),
            refusal=Refusal(reason="r", found=["something else"]),
            status="refused",
        ),
        PolicyDecision(kept=list(TWO.claims), status="refused"),
        PolicyDecision(kept=list(TWO.claims), status="partially_verified"),
    ],
)
def test_policy_decisions_outside_the_contract_raise(decision: PolicyDecision) -> None:
    with pytest.raises(VerifierContractError):
        breach(apply=lambda *_: decision)


def test_a_composer_refusal_cannot_be_turned_into_an_answer() -> None:
    refused = TWO.model_copy(update={"refusal": Refusal(reason="no")})
    with pytest.raises(VerifierContractError, match="composer refusal"):
        breach(
            apply=lambda *_: PolicyDecision(kept=list(TWO.claims), status="verified"),
            answer=refused,
        )


def test_an_empty_answer_without_a_refusal_is_a_breach() -> None:
    empty = Answer(answer_markdown="prose", claims=[])
    with pytest.raises(VerifierContractError, match="no kept claim"):
        breach(apply=lambda *_: PolicyDecision(kept=[], status="verified"), answer=empty)


def test_the_verifier_cannot_call_a_model() -> None:
    for path in Path("app/engine/verify").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            assert not any(n.startswith(("app.llm", "anthropic")) for n in names), path


def test_preflight_names_every_stubbed_core(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (numbers, numeric, quotes, policy):
        monkeypatch.setattr(module, "IMPLEMENTED", False)
    monkeypatch.setattr(numeric, "IMPLEMENTED", True)
    with pytest.raises(VerifierNotImplemented, match="numbers, quotes, policy"):
        require_implemented()
    for module in (numbers, quotes, policy):
        monkeypatch.setattr(module, "IMPLEMENTED", True)
    require_implemented()


# --- H0: the composer's refusal wording ------------------------------------------------------

COMPOSER = Refusal(
    reason="Weak Chinese demand hurt margins, but the forecast is not disclosed.",
    found=["Margins fell on softness in China."],
    missing=["why demand in China softened"],
)


def test_the_policy_never_sees_the_composers_refusal_wording() -> None:
    seen: list[Answer] = []
    fake = FakeVerifier()
    v = fake.verifier()

    def spy(answer: Answer, *args: Any) -> PolicyDecision:
        seen.append(answer)
        return fake.apply(answer, *args)

    prose = "The material does not say how weak Chinese demand was [1]."
    refused = TWO.model_copy(update={"refusal": COMPOSER, "answer_markdown": prose})
    out, _ = verify_answer(
        refused, Evidence(), context(), verifier=type(v)(v.check_numeric, v.check_text,
        v.decide, spy))  # fmt: skip
    [handed] = seen
    assert handed.refusal == Refusal(reason=WITHHELD_REASON)  # still a refusal, no wording
    assert handed.claims == refused.claims
    assert handed.answer_markdown == ""  # the composer words its refusals here too
    assert refused.refusal == COMPOSER  # the composer's answer is not edited in place
    assert "Chin" not in out.model_dump_json() and "material does not" not in out.answer_markdown


def test_an_unrefused_answer_loses_its_prose_and_gains_no_refusal() -> None:
    handed = without_composer_wording(TWO)
    assert (handed.answer_markdown, handed.refusal, handed.claims) == ("", None, TWO.claims)
    assert TWO.answer_markdown  # the original is untouched


@pytest.mark.parametrize("where", ["reason", "missing"])
def test_passing_the_composers_refusal_through_breaks_the_contract(where: str) -> None:
    """A policy that "keeps the composer's reason" (the old D6) echoes what it was handed."""

    def pass_through(answer: Answer, *_: Any) -> PolicyDecision:
        assert answer.refusal is not None
        text = answer.refusal.reason
        refusal = Refusal(
            reason=text if where == "reason" else "A template.",
            missing=[f"also: {text}"] if where == "missing" else [],
            found=[c.text for c in answer.claims],
        )
        return PolicyDecision(kept=list(answer.claims), refusal=refusal, status="refused")

    with pytest.raises(VerifierContractError, match="H0"):
        breach(apply=pass_through, answer=TWO.model_copy(update={"refusal": COMPOSER}))


# --- render ----------------------------------------------------------------------------------


def restated() -> NumericResult:
    note = Note(code="restated", text="Later restated.", latest_fact_id=9, latest_value=Decimal(6))
    quiet = Note(code="q4_derived", text="Quiet.", must_mention=False)
    return result(notes=[note, quiet])


def test_notes_follow_the_kept_figures_only_and_appear_once() -> None:
    results = [
        restated(),
        result(
            expr="f:2", fact_ids=[2], notes=[Note(code="q4_derived", text="Dropped figure's note.")]
        ),
    ]
    original = numeric_claim("c1", "A.")
    latest = numeric_claim("c2", "B.", expr="f:9", ids=[9])
    assert compiler_notes([original, latest], results) == ["Later restated."]
    assert compiler_notes([text_claim("c3", "T.")], results) == []


def test_the_period_line_is_for_the_reader_and_only_with_a_kept_claim() -> None:
    plan = QueryPlan(intent="numeric", periods=[PeriodRef(fiscal_year=2024)])
    ctx = context(plan=plan, assumed_period=True)
    line = period_line(ctx)
    assert line and "FY2024" in line and "Say" not in line
    worded = context(plan=plan.model_copy(update={"period_text": "last year"}), assumed_period=True)
    assert '"last year" was read as FY2024' in (period_line(worded) or "")
    assert period_line(context(plan=plan)) is None
    nothing = PolicyDecision(kept=[], refusal=Refusal(reason="No."), status="refused")
    assert render(nothing, ctx) == "No."
    some = PolicyDecision(kept=[numeric_claim("c1", "A.")], status="verified")
    assert render(some, ctx).endswith(line)


def test_markers_number_the_kept_claims_in_order() -> None:
    decision = PolicyDecision(
        kept=[text_claim("c2", "B."), text_claim("c7", "C.")], status="partially_verified"
    )
    assert render(decision, context()) == "B. [1] C. [2]"


# --- context helpers -------------------------------------------------------------------------


def test_allowed_companies() -> None:
    named = QueryPlan(intent="numeric", companies=[CompanyRef(mention="A", cik=7)])
    assert allowed_ciks(named, [1, 2]) == {7}
    assert allowed_ciks(QueryPlan(intent="numeric", operation="rank"), [1, 2]) == {1, 2}
    assert allowed_ciks(QueryPlan(intent="narrative"), [1, 2]) == frozenset()


def test_company_names_carry_tickers_and_aliases() -> None:
    names = company_names(
        [CompanyRecord(cik=1, name="Alphabet Inc.", ticker="GOOGL", tickers=["GOOG"])],
        [("Google", 1), ("Nobody", 2)],
    )
    assert names[1].aliases == ["Google"] and names[1].tickers == ["GOOG"] and list(names) == [1]


# --- evidence --------------------------------------------------------------------------------


class Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class EvidenceConn:
    def __init__(self) -> None:
        self.asked: list[tuple[str, list[Any]]] = []

    def execute(self, sql: str, params: list[Any]) -> Rows:
        table = "facts" if " FROM facts " in sql else "chunks"
        self.asked.append((table, params[0]))
        columns = sql.split("SELECT ")[1].split(" FROM")[0].split(", ")
        out = []
        for key in params[0]:
            if table == "facts" and key == 404:
                continue
            row: dict[str, Any] = dict.fromkeys(columns)
            if table == "facts":
                row |= {
                    "fact_id": key, "cik": 1, "tag": "T", "taxonomy": "us-gaap", "uom": "USD",
                    "qtrs": 4, "period_end": date(2023, 12, 31), "fiscal_year": 2023,
                    "fiscal_period": "FY", "value": Decimal(1), "form": "10-K",
                    "filed": date(2024, 1, 1), "is_latest": True, "is_original": True,
                    "is_restated": False, "derived_from": [20, 21] if key == 2 else None,
                }  # fmt: skip
            else:
                row |= {"chunk_id": key, "cik": 1, "text": "body"}
            out.append(tuple(row[c] for c in columns))
        return Rows(out)


def test_evidence_loads_cited_and_compiled_facts_parents_and_only_shown_chunks() -> None:
    answer = Answer(
        answer_markdown="m",
        claims=[
            numeric_claim("c1", "t", expr="f:2 + f:404", ids=[2, 404]),
            text_claim("c2", "shown"),
            Claim(
                id="c3",
                kind="text",
                text="hidden",
                provenance=ChunkProvenance(type="chunk", chunk_id="never#shown", quote="q"),
            ),
        ],
    )
    conn = EvidenceConn()
    evidence = load_evidence(conn, answer, [restated()], ["a#1#0001", "other"])
    assert sorted(evidence.facts) == [1, 2, 9, 20, 21]  # 404 does not exist: simply absent
    assert list(evidence.chunks) == ["a#1#0001"]
    assert conn.asked == [
        ("facts", [1, 2, 9, 404]),
        ("facts", [20, 21]),
        ("chunks", ["a#1#0001"]),
    ]


def test_checks_object_rejects_an_unknown_key() -> None:
    with pytest.raises(ValueError):
        Checks.model_validate({"vibes": "pass"})


@pytest.mark.parametrize(("cid", "missing"), [("c1", "provenance_exec"), ("c2", "quote_substring")])
def test_a_claim_is_never_kept_on_checks_that_were_not_made(cid: str, missing: str) -> None:
    """A check that returns early, leaving everything "n/a", must not publish the claim."""
    fake = FakeVerifier()
    answer = TWO.model_copy(update={"claims": [c for c in TWO.claims if c.id == cid]})

    def early_return(claim: Claim, *_: Any) -> CheckOutcome:
        return CheckOutcome(checks=Checks())

    v = fake.verifier()
    v = type(v)(early_return, early_return, v.decide, v.apply)
    with pytest.raises(VerifierContractError, match=missing):
        verify_answer(answer, Evidence(), context(), verifier=v)
