"""The engine orchestration (M05 section 3.8), with every collaborator injected.

Real: the planner, the resolver, the composer's inputs, the grounding check, the refusal templates
and the stage logic. Doubles: the LLM boundary, the compiler (Shekar's stub is not implemented),
the retrieval call and the database connection (never touched by the doubles).
"""

from __future__ import annotations

import asyncio
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.engine.answer import Answer, ChunkProvenance, Claim, ExprProvenance
from app.engine.compose.composer import COMPOSER_SYSTEM
from app.engine.engine import (
    OUT_OF_SCOPE_REASON,
    EngineSystem,
    describe_missing,
    load_tracked_ciks,
)
from app.engine.entities.resolve_company import CompanyIndex
from app.engine.plan.planner import build_system_prompt
from app.engine.plan.schema import (
    CompanyRef,
    CompileOutcome,
    NotFound,
    NumericResult,
    PeriodRef,
    QueryPlan,
    UnsupportedPlan,
)
from app.engine.retrieval.query import RankedChunk, RetrievalResult
from app.llm.client import BatchPending
from app.llm.types import Usage
from eval.systems import SYSTEMS, EvalCfg, reset_retrieval, retrieval_sink

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "entities" / "companies.csv"
APPLE = 320193


class Conn:
    """A database connection that answers the one query the engine runs itself."""

    closed = False

    def __init__(self, latest_fy: int | None = 2024) -> None:
        self.latest_fy = latest_fy
        self.queries: list[str] = []

    def execute(self, sql: str, params: Any = None) -> Conn:
        self.queries.append(sql)
        return self

    def fetchone(self) -> tuple[int | None]:
        return (self.latest_fy,)

    def close(self) -> None:
        self.closed = True


class ScriptedLLM:
    """Plans and answers on cue. A stand-in for the model boundary only."""

    def __init__(
        self, plan: QueryPlan, answer: Answer | None = None, pending: str | None = None
    ) -> None:
        self.plan, self.answer, self.pending = plan, answer, pending
        self.calls: list[dict[str, Any]] = []
        self.ledger: list[Usage] = []

    def structured(
        self,
        role: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        *,
        max_tokens: int = 2048,
    ) -> tuple[Any, Usage]:
        self.calls.append({"role": role, "system": system, "messages": messages, "schema": schema})
        if self.pending == role:
            raise BatchPending("key")
        usage = Usage.model_construct(
            model="m",
            input_tokens=1,
            cache_write_tokens=0,
            cache_read_tokens=0,
            output_tokens=1,
            cost_usd=Decimal("0.001"),
            latency_ms=1,
        )
        self.ledger.append(usage)
        if schema is QueryPlan:
            return self.plan, usage
        assert schema is Answer and self.answer is not None, "the composer was called unexpectedly"
        return self.answer, usage


def cfg(llm: ScriptedLLM) -> EvalCfg:
    return EvalCfg(llm=llm, dsn="postgres://x", top_k=8, ef_search=800)  # type: ignore[arg-type]


def result(value: str = "96995000000") -> NumericResult:
    return NumericResult(
        metric="net_income",
        cik=APPLE,
        fiscal_year=2024,
        fiscal_period="FY",
        operation="value",
        value=Decimal(value),
        unit="USD",
        expr="f:111",
        fact_ids=[111],
        kind="reported",
    )


def numeric_plan(**kw: Any) -> QueryPlan:
    base: dict[str, Any] = {
        "intent": "numeric",
        "companies": [CompanyRef(mention="Apple")],
        "metrics": ["net_income"],
        "operation": "value",
    }
    base.update(kw)
    return QueryPlan(**base)


def numeric_answer() -> Answer:
    return Answer(
        answer_markdown="Apple's FY2024 net income was $97.0 billion [1].",
        claims=[
            Claim(
                id="c1",
                kind="numeric",
                text="Net income was $97.0 billion.",
                value=Decimal("96995000000"),
                unit="USD",
                display="$97.0 billion",
                primary=True,
                provenance=ExprProvenance(type="expr", expr="f:111", fact_ids=[111]),
            )
        ],
    )


class Spy:
    """Records compile/retrieve calls (with the thread each ran on)."""

    def __init__(
        self,
        outcome: CompileOutcome | Exception | None = None,
        retrieved: RetrievalResult | None = None,
    ) -> None:
        self.outcome, self.retrieved = outcome, retrieved
        self.compile_calls: list[tuple[QueryPlan, Any, Any]] = []
        self.retrieve_calls: list[tuple[QueryPlan, str, Any]] = []
        self.threads: dict[str, int] = {}
        self.retrieve_conns: list[Any] = []
        self.barrier: threading.Barrier | None = None

    def compile(self, plan: QueryPlan, conn: Any, *, universe: Any = None) -> CompileOutcome:
        self.threads["compile"] = threading.get_ident()
        if self.barrier is not None:
            self.barrier.wait(timeout=5)  # only passes if retrieve() runs at the same time
        self.compile_calls.append((plan, conn, universe))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        assert self.outcome is not None
        return self.outcome

    def retrieve(self, conn: Any, plan: QueryPlan, question: str, **kw: Any) -> RetrievalResult:
        self.threads["retrieve"] = threading.get_ident()
        self.retrieve_conns.append(conn)
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        self.retrieve_calls.append((plan, question, kw["cfg"]))
        assert self.retrieved is not None
        return self.retrieved


def engine(spy: Spy, conns: list[Conn] | None = None) -> EngineSystem:
    made: list[Conn] = conns if conns is not None else []

    def connect(dsn: str) -> Conn:
        c = Conn()
        made.append(c)
        return c

    return EngineSystem(
        index=CompanyIndex.from_csv(FIXTURE),
        compile_fn=spy.compile,
        retrieve_fn=spy.retrieve,
        connect=connect,
        embedder=object(),
        reranker=object(),
        tracked_ciks=[APPLE],  # type: ignore[arg-type]
    )


def chunk(
    cid: str = "0000320193-24-000123#7#0001", text: str = "Services revenue grew."
) -> RankedChunk:
    return RankedChunk(cid, 1, 1.0, "7", text)


def retrieved(*chunks: RankedChunk) -> RetrievalResult:
    return RetrievalResult(
        chunks=list(chunks), queries=["q"], filters={"ciks": [], "fiscal_years": []}
    )


# --- the happy numeric path --------------------------------------------------------------------


def test_numeric_question_compiles_composes_and_grounds() -> None:
    llm = ScriptedLLM(numeric_plan(periods=[PeriodRef(fiscal_year=2024)]), numeric_answer())
    spy = Spy(CompileOutcome(results=[result()]))
    eng = engine(spy)
    ans = eng.answer("How much net income did Apple report for FY2024?", cfg(llm))
    assert ans.refusal is None and ans.claims[0].display == "$97.0 billion"
    assert [c["role"] for c in llm.calls] == ["planner", "composer"]
    assert (
        llm.calls[0]["system"] == build_system_prompt()
        and llm.calls[1]["system"] == COMPOSER_SYSTEM
    )
    (plan, _, universe) = spy.compile_calls[0]
    assert [c.cik for c in plan.companies] == [APPLE], "CIKs are attached by code before compiling"
    assert universe is None, "only a rank ranges over the tracked universe"
    assert not spy.retrieve_calls, "a numeric plan retrieves nothing"
    diag = eng.diagnostics["How much net income did Apple report for FY2024?"]
    assert diag.composed and diag.violations == [] and diag.plan is plan or diag.plan == plan


def test_a_composer_that_computes_its_own_number_is_flagged_not_hidden() -> None:
    """The M05 pitfall: a 'helpful' ratio. Before M6 it is reported, not silently dropped."""
    bad = Answer(
        answer_markdown="Margin was 29.8%.",
        claims=[
            Claim(
                id="c1",
                kind="numeric_derived",
                text="Margin was 29.8%.",
                value=Decimal("0.298"),
                provenance=ExprProvenance(type="expr", expr="f:111 / f:222", fact_ids=[111, 222]),
            )
        ],
    )
    eng = engine(Spy(CompileOutcome(results=[result()])))
    llm = ScriptedLLM(numeric_plan(periods=[PeriodRef(fiscal_year=2024)]), bad)
    ans = eng.answer("q", cfg(llm))
    assert ans.claims == bad.claims
    assert [v.rule for v in eng.diagnostics["q"].violations] == ["not_a_provided_result"]


def test_no_period_uses_the_latest_filed_fiscal_year_and_says_so() -> None:
    llm = ScriptedLLM(numeric_plan(), numeric_answer())
    spy = Spy(CompileOutcome(results=[result()]))
    eng = engine(spy)
    eng.answer("What was Apple's net income?", cfg(llm))
    plan = spy.compile_calls[0][0]
    assert [(p.fiscal_year, p.fiscal_period) for p in plan.periods] == [(2024, "FY")]
    assert "PERIOD NOTE" in llm.calls[1]["messages"][0]["content"]
    assert eng.diagnostics["What was Apple's net income?"].assumed_period is not None


def test_a_named_period_is_never_overridden() -> None:
    llm = ScriptedLLM(numeric_plan(periods=[PeriodRef(fiscal_year=2021)]), numeric_answer())
    spy = Spy(CompileOutcome(results=[result()]))
    engine(spy).answer("q", cfg(llm))
    assert spy.compile_calls[0][0].periods == [PeriodRef(fiscal_year=2021)]
    assert "PERIOD NOTE" not in llm.calls[1]["messages"][0]["content"]


# --- refusals without a composer call ----------------------------------------------------------


def test_out_of_scope_is_a_templated_refusal_with_no_composer_and_no_lookup() -> None:
    llm = ScriptedLLM(QueryPlan(intent="out_of_scope"))
    spy = Spy()
    ans = engine(spy).answer("Predict Costco's operating income.", cfg(llm))
    assert ans.refusal is not None and ans.refusal.reason == OUT_OF_SCOPE_REASON
    assert ans.refusal.found == [] and ans.refusal.missing == [] and ans.claims == []
    assert [c["role"] for c in llm.calls] == ["planner"]
    assert not spy.compile_calls and not spy.retrieve_calls


def test_an_unresolved_company_asks_instead_of_answering() -> None:
    plan = numeric_plan(
        companies=[CompanyRef(mention="Zzyzx Quantum")], periods=[PeriodRef(fiscal_year=2024)]
    )
    llm = ScriptedLLM(plan)
    spy = Spy()
    ans = engine(spy).answer("q", cfg(llm))
    assert ans.refusal is not None and "Zzyzx Quantum" in ans.refusal.reason
    assert not spy.compile_calls and [c["role"] for c in llm.calls] == ["planner"]


def test_not_found_everywhere_refuses_and_lists_what_is_missing() -> None:
    nf = NotFound(
        metric="net_income", cik=APPLE, fiscal_year=2024, fiscal_period="FY", reason="no_fact"
    )
    llm = ScriptedLLM(numeric_plan(periods=[PeriodRef(fiscal_year=2024)]))
    ans = engine(Spy(CompileOutcome(not_found=[nf]))).answer("q", cfg(llm))
    assert ans.refusal is not None and ans.claims == []
    assert ans.refusal.missing == ["net_income for Apple Inc., FY2024 (no_fact)"]
    assert [c["role"] for c in llm.calls] == ["planner"], "nothing to compose: no composer spend"


def test_an_unsupported_plan_refuses_and_says_why() -> None:
    llm = ScriptedLLM(numeric_plan(operation=None, periods=[PeriodRef(fiscal_year=2024)]))
    eng = engine(Spy(UnsupportedPlan("no operation on a numeric plan")))
    ans = eng.answer("q", cfg(llm))
    assert ans.refusal is not None and "no operation on a numeric plan" in ans.refusal.missing[0]
    assert eng.diagnostics["q"].unsupported == "no operation on a numeric plan"


# --- narrative and hybrid ----------------------------------------------------------------------


def test_narrative_retrieves_and_composes_over_the_passages() -> None:
    plan = QueryPlan(
        intent="narrative",
        companies=[CompanyRef(mention="Apple")],
        periods=[PeriodRef(fiscal_year=2024)],
        narrative_topics=["services"],
    )
    c = chunk()
    answer = Answer(
        answer_markdown="Services grew [1].",
        claims=[
            Claim(
                id="c1",
                kind="text",
                text="Services revenue grew.",
                primary=True,
                provenance=ChunkProvenance(
                    type="chunk", chunk_id=c.chunk_id, quote="Services revenue grew."
                ),
            )
        ],
    )
    llm = ScriptedLLM(plan, answer)
    spy = Spy(retrieved=retrieved(c))
    eng = engine(spy)
    sink, token = retrieval_sink()
    try:
        out = eng.answer("What drove services?", cfg(llm))
    finally:
        reset_retrieval(token)
    assert out.claims == answer.claims and not spy.compile_calls
    assert sink == [c.chunk_id], "the harness sees the retrieved ids in rank order"
    assert spy.retrieve_calls[0][2].top_k == 8 and spy.retrieve_calls[0][2].ef_search == 800
    assert eng.diagnostics["What drove services?"].violations == []


def test_hybrid_runs_both_branches_concurrently() -> None:
    plan = numeric_plan(
        intent="hybrid", periods=[PeriodRef(fiscal_year=2024)], narrative_topics=["margins"]
    )
    spy = Spy(CompileOutcome(results=[result()]), retrieved(chunk()))
    spy.barrier = threading.Barrier(
        2
    )  # each branch waits for the other: a serial run would time out
    llm = ScriptedLLM(plan, numeric_answer())
    engine(spy).answer("q", cfg(llm))
    assert spy.compile_calls and spy.retrieve_calls and not spy.barrier.broken
    content = llm.calls[1]["messages"][0]["content"]
    assert "NUMERIC RESULTS" in content and "PASSAGES" in content


def test_the_two_branches_use_two_connections() -> None:
    plan = numeric_plan(intent="hybrid", periods=[PeriodRef(fiscal_year=2024)])
    spy = Spy(CompileOutcome(results=[result()]), retrieved(chunk()))
    engine(spy).answer("q", cfg(ScriptedLLM(plan, numeric_answer())))
    assert spy.compile_calls[0][1] is not spy.retrieve_conns[0], "a connection is not thread-safe"


def test_hybrid_with_only_passages_still_answers_and_lists_what_is_not_found() -> None:
    nf = NotFound(
        metric="net_income", cik=APPLE, fiscal_year=2024, fiscal_period="FY", reason="no_fact"
    )
    plan = numeric_plan(intent="hybrid", periods=[PeriodRef(fiscal_year=2024)])
    llm = ScriptedLLM(plan, Answer(answer_markdown="Partial.", claims=[]))
    engine(Spy(CompileOutcome(not_found=[nf]), retrieved(chunk()))).answer("q", cfg(llm))
    content = llm.calls[1]["messages"][0]["content"]
    assert "NOT FOUND" in content and "PASSAGES" in content


# --- batch re-entrancy, wiring, helpers --------------------------------------------------------


def test_batch_pending_at_the_planner_propagates_for_the_harness_to_rerun() -> None:
    llm = ScriptedLLM(numeric_plan(), numeric_answer(), pending="planner")
    with pytest.raises(BatchPending):
        engine(Spy(CompileOutcome())).answer("q", cfg(llm))


def test_batch_pending_at_the_composer_propagates_after_the_lookups() -> None:
    llm = ScriptedLLM(
        numeric_plan(periods=[PeriodRef(fiscal_year=2024)]), numeric_answer(), pending="composer"
    )
    spy = Spy(CompileOutcome(results=[result()]))
    with pytest.raises(BatchPending):
        engine(spy).answer("q", cfg(llm))
    assert spy.compile_calls, (
        "round 1 plans; round 2 composes: the lookups are redone, they are cheap"
    )


def test_the_engine_is_registered_with_the_harness() -> None:
    system = SYSTEMS["engine"]()
    assert system.name == "engine" and getattr(system, "reads_corpus", False) is True
    assert system.version == "0.5.0"  # type: ignore[attr-defined]


def test_connections_are_opened_lazily_reused_and_closed() -> None:
    made: list[Conn] = []
    spy = Spy(CompileOutcome(results=[result()]))
    eng = engine(spy, made)
    plan = numeric_plan(periods=[PeriodRef(fiscal_year=2024)])
    eng.answer("a", cfg(ScriptedLLM(plan, numeric_answer())))
    eng.answer("b", cfg(ScriptedLLM(plan, numeric_answer())))
    assert len(made) == 1, "one 'numeric' connection, reused across questions"
    eng.close()
    assert all(c.closed for c in made)


def test_a_missing_dsn_is_a_clear_error() -> None:
    eng = engine(Spy(CompileOutcome(results=[result()])))
    llm = ScriptedLLM(numeric_plan(periods=[PeriodRef(fiscal_year=2024)]), numeric_answer())
    config = cfg(llm)
    config.dsn = None
    with pytest.raises(RuntimeError, match="serving DSN"):
        eng.answer("q", config)


def test_load_tracked_ciks(tmp_path: Path) -> None:
    p = tmp_path / "t.csv"
    p.write_text("cik,ticker,name\n2,B,Bee\n1,A,Ay\n2,B,Bee\n")
    assert load_tracked_ciks(p) == [1, 2]


def test_describe_missing_names_company_and_period() -> None:
    nfs = [
        NotFound(metric="capex", cik=APPLE, fiscal_year=2023, fiscal_period="Q2", reason="no_fact"),
        NotFound(
            metric="capex",
            cik=5,
            fiscal_year=2023,
            fiscal_period="FY",
            reason="growth_base_missing",
        ),
    ]
    assert describe_missing(nfs, {APPLE: "Apple Inc."}) == [
        "capex for Apple Inc., Q2 FY2023 (no_fact)",
        "capex for CIK 5, FY2023 (growth_base_missing)",
    ]


def test_the_async_entry_point_runs_without_asyncio_run() -> None:
    llm = ScriptedLLM(numeric_plan(periods=[PeriodRef(fiscal_year=2024)]), numeric_answer())
    ans = asyncio.run(engine(Spy(CompileOutcome(results=[result()]))).aanswer("q", cfg(llm)))
    assert ans.claims


def test_a_rank_ranges_over_the_tracked_ciks_even_without_an_explicit_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.engine.engine as engine_module

    monkeypatch.setattr(engine_module, "load_tracked_ciks", lambda: [7, 3, 5])
    plan = numeric_plan(operation="rank", periods=[PeriodRef(fiscal_year=2024)])
    spy = Spy(CompileOutcome(results=[result()]))
    eng = EngineSystem(
        index=CompanyIndex.from_csv(FIXTURE),
        compile_fn=spy.compile,
        retrieve_fn=spy.retrieve,
        connect=lambda dsn: Conn(),
    )  # default: no list given
    eng.answer("q", cfg(ScriptedLLM(plan, numeric_answer())))
    assert spy.compile_calls[0][2] == [3, 5, 7]


def test_an_unsupported_numeric_part_is_told_to_the_composer_on_a_hybrid_question() -> None:
    plan = numeric_plan(intent="hybrid", periods=[PeriodRef(fiscal_year=2024)])
    llm = ScriptedLLM(plan, Answer(answer_markdown="Only the text part.", claims=[]))
    spy = Spy(UnsupportedPlan("no operation on a numeric plan"), retrieved(chunk()))
    engine(spy).answer("q", cfg(llm))
    content = llm.calls[1]["messages"][0]["content"]
    assert "unsupported_plan" in content and "no operation on a numeric plan" in content


def test_with_no_company_named_the_period_defaults_to_the_latest_widely_filed_year() -> None:
    plan = numeric_plan(companies=[], operation="rank")
    llm = ScriptedLLM(plan, numeric_answer())
    spy = Spy(CompileOutcome(results=[result()]))
    eng = EngineSystem(
        index=CompanyIndex.from_csv(FIXTURE),
        compile_fn=spy.compile,
        retrieve_fn=spy.retrieve,
        connect=lambda dsn: Conn(2023),
        tracked_ciks=[APPLE],
    )
    eng.answer("q", cfg(llm))
    assert [p.fiscal_year for p in spy.compile_calls[0][0].periods] == [2023]
    assert "most companies" in llm.calls[1]["messages"][0]["content"]
