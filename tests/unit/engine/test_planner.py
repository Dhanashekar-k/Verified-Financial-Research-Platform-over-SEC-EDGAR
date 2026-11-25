"""Planner (M05 section 3.3): catalog-built cached prompt, few-shots, CIKs attached by code."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from rapidfuzz import fuzz

from app.engine.entities.resolve_company import CompanyIndex
from app.engine.plan.catalog import load_catalog
from app.engine.plan.planner import (
    FEWSHOTS_PATH,
    MAX_PLAN_TOKENS,
    PLANNER_ROLE,
    attach_ciks,
    build_system_prompt,
    load_fewshots,
    plan_question,
)
from app.engine.plan.schema import METRIC_NAMES, CompanyRef, QueryPlan
from app.llm.client import LLMClient
from app.llm.types import Usage

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "entities" / "companies.csv"


class FakeLLM:
    """The LLM boundary, recorded. (The unit under test, the planner, is real.)"""

    def __init__(self, plan: QueryPlan) -> None:
        self.plan = plan
        self.calls: list[dict[str, Any]] = []

    def structured(
        self,
        role_or_model: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        *,
        max_tokens: int = 2048,
    ) -> tuple[Any, Usage]:
        self.calls.append(
            {"role": role_or_model, "system": system, "messages": messages, "schema": schema}
            | {"max_tokens": max_tokens}
        )
        usage = Usage.model_construct()  # not read by the planner
        return self.plan, usage


@pytest.fixture(scope="module")
def index() -> CompanyIndex:
    return CompanyIndex.from_csv(FIXTURE)


# --- few-shots ---------------------------------------------------------------------------------


def test_there_are_eight_fewshots_and_all_validate() -> None:
    shots = load_fewshots()
    assert len(shots) == 8
    assert FEWSHOTS_PATH.name == "planner_fewshots.yaml"
    for s in shots:
        assert all(m in METRIC_NAMES for m in s.plan.metrics)
        assert all(c.cik is None for c in s.plan.companies), "the LLM never outputs a CIK"


def test_fewshots_cover_every_intent_and_the_shapes_that_matter() -> None:
    plans = [s.plan for s in load_fewshots()]
    assert {p.intent for p in plans} == {"numeric", "narrative", "hybrid", "out_of_scope"}
    assert {p.operation for p in plans} >= {"value", "growth", "compare"}
    assert any(p.needs_clarification for p in plans)
    assert any(p.intent == "numeric" and not p.periods for p in plans), "relative period example"
    assert any(len(p.periods) == 2 for p in plans), "growth lists two periods"
    assert any(p.section_hints for p in plans) and any(p.narrative_topics for p in plans)


# --- the system prompt -------------------------------------------------------------------------


def test_prompt_lists_every_catalog_metric_with_its_synonyms() -> None:
    prompt = build_system_prompt()
    catalog = load_catalog()
    for key, m in catalog.items():
        assert f"- {key}:" in prompt
        for syn in m.synonyms:
            assert syn in prompt
    assert prompt.count("\n- ") >= len(METRIC_NAMES)


def test_prompt_is_deterministic_and_names_the_rules() -> None:
    a, b = build_system_prompt(), build_system_prompt()
    assert a == b
    flat = " ".join(a.lower().split())
    for needle in ("out_of_scope", "Never output a CIK", "never compute", "leave periods empty"):
        assert needle.lower() in flat, needle
    assert "Example 8" in a and "Example 9" not in a


def test_the_cassette_key_is_stable_for_the_same_question() -> None:
    client = LLMClient()
    msgs = [{"role": "user", "content": "How much net income did Apple report for FY2023?"}]
    k1 = client.request_key(PLANNER_ROLE, build_system_prompt(), msgs, QueryPlan)
    k2 = client.request_key(PLANNER_ROLE, build_system_prompt(), msgs, QueryPlan)
    assert k1 == k2
    changed = build_system_prompt(shots=load_fewshots()[:-1])
    assert client.request_key(PLANNER_ROLE, changed, msgs, QueryPlan) != k1


# --- plan_question -----------------------------------------------------------------------------


def test_plan_question_makes_one_haiku_call_with_the_question_only() -> None:
    plan = QueryPlan(intent="numeric", metrics=["net_income"], operation="value")
    llm = FakeLLM(plan)
    got, _ = plan_question("How much net income did Apple report for FY2023?", llm)
    assert got == plan
    (call,) = llm.calls
    assert call["role"] == PLANNER_ROLE == "planner"
    assert call["schema"] is QueryPlan and call["max_tokens"] == MAX_PLAN_TOKENS
    assert call["messages"] == [
        {"role": "user", "content": "How much net income did Apple report for FY2023?"}
    ]
    assert call["system"] == build_system_prompt()


def test_plan_question_uses_a_prebuilt_system_prompt() -> None:
    llm = FakeLLM(QueryPlan(intent="narrative"))
    plan_question("q", llm, system="SYS")
    assert llm.calls[0]["system"] == "SYS"


def test_a_misbehaving_client_fails_loudly() -> None:
    class Bad:
        def structured(self, *a: Any, **k: Any) -> tuple[Any, Usage]:
            return {"intent": "numeric"}, Usage.model_construct()

    with pytest.raises(TypeError, match="planner returned dict"):
        plan_question("q", Bad())


def test_the_planner_role_is_haiku() -> None:
    assert LLMClient().resolve(PLANNER_ROLE) == "claude-haiku-5-5"


# --- attach_ciks -------------------------------------------------------------------------------


def test_attach_ciks_fills_resolved_companies_in_order(index: CompanyIndex) -> None:
    plan = QueryPlan(
        intent="numeric",
        companies=[CompanyRef(mention="Apple"), CompanyRef(mention="Applied Materials")],
    )
    out = attach_ciks(plan, index)
    assert [c.cik for c in out.companies] == [320193, 6951]
    assert [c.mention for c in out.companies] == ["Apple", "Applied Materials"]
    assert out.needs_clarification is None
    assert plan.companies[0].cik is None, "the input plan is not mutated"


def test_an_ambiguous_mention_asks_instead_of_guessing(index: CompanyIndex) -> None:
    out = attach_ciks(QueryPlan(intent="numeric", companies=[CompanyRef(mention="Johnson")]), index)
    assert out.companies[0].cik is None
    assert out.needs_clarification is not None and "Which one" in out.needs_clarification


def test_an_unknown_mention_asks_and_keeps_the_resolved_ones(index: CompanyIndex) -> None:
    plan = QueryPlan(
        intent="numeric",
        companies=[CompanyRef(mention="Apple"), CompanyRef(mention="Zzyzx Quantum")],
    )
    out = attach_ciks(plan, index)
    assert [c.cik for c in out.companies] == [320193, None]
    assert out.needs_clarification is not None and "Zzyzx Quantum" in out.needs_clarification


def test_attach_ciks_overwrites_a_cik_the_model_wrote_and_keeps_a_clarification(
    index: CompanyIndex,
) -> None:
    plan = QueryPlan(
        intent="numeric",
        companies=[CompanyRef(mention="Apple", cik=789019), CompanyRef(mention="Zzyzx Quantum")],
        needs_clarification="Which period?",
    )
    out = attach_ciks(plan, index)
    assert out.companies[0].cik == 320193, "Apple is Apple whatever CIK the model wrote"
    assert out.companies[1].cik is None
    assert out.needs_clarification == "Which period?"
    wrong = QueryPlan(intent="numeric", companies=[CompanyRef(mention="Zzyzx Quantum", cik=5)])
    assert attach_ciks(wrong, index).companies[0].cik is None, "an unknown mention keeps no CIK"


# --- the few-shots must not be eval questions --------------------------------------------------


def _gold_questions() -> list[str]:
    sets = Path(__file__).resolve().parents[3] / "eval" / "sets"
    out: list[str] = []
    for name in ("numeric_auto", "financebench", "narrative", "unanswerable"):
        for line in (sets / f"{name}.jsonl").read_text().splitlines():
            out.append(json.loads(line)["question"])
    return out


def test_no_fewshot_is_or_nearly_is_an_eval_question() -> None:
    """A few-shot copied from a gold set leaks it into the prompt and inflates the intent and
    plan-field scores of exactly the rows it copies (found in review: 3 of 8 were)."""
    gold = [q.casefold() for q in _gold_questions()]
    for shot in load_fewshots():
        q = shot.question.casefold()
        best = max(fuzz.ratio(q, g) for g in gold)
        assert best < 80, (shot.question, best)


def test_no_fewshot_asks_for_a_gold_numeric_fact(index: CompanyIndex) -> None:
    """Same company, metric and period as a numeric gold row is the same question reworded."""
    sets = Path(__file__).resolve().parents[3] / "eval" / "sets" / "numeric_auto.jsonl"
    rows = [json.loads(line) for line in sets.read_text().splitlines()]
    gold = {(r["cik"], r["metric"], r["fiscal_year"], r["fiscal_period"]) for r in rows}
    gold |= {
        (r["compare_cik"], r["metric"], r["fiscal_year"], r["fiscal_period"])
        for r in rows
        if r["compare_cik"]
    }
    for shot in load_fewshots():
        plan = attach_ciks(shot.plan, index)
        for c in plan.companies:
            for m in plan.metrics:
                for p in plan.periods:
                    assert (c.cik, m, p.fiscal_year, p.fiscal_period) not in gold, shot.question


def test_a_cik_written_by_the_model_is_discarded() -> None:
    wrote = QueryPlan(intent="numeric", companies=[CompanyRef(mention="Apple", cik=789019)])
    got, _ = plan_question("q", FakeLLM(wrote))
    assert [c.cik for c in got.companies] == [None]


# --- the front door (ADR-048) ------------------------------------------------------------------


def test_fewshots_teach_relative_wording_unmapped_measures_and_vague_questions() -> None:
    shots = load_fewshots()
    relative = [s.plan for s in shots if s.plan.period_text]
    assert relative and all(not p.periods for p in relative), "wording never becomes a year"
    assert any(p.period_text == "most recent quarter" for p in relative)
    unmapped = [s for s in shots if s.plan.intent == "narrative" and "ebitda" in s.question.lower()]
    assert unmapped and not unmapped[0].plan.metrics, "a near metric is never swapped in"
    assert any(
        p.plan.needs_clarification and not p.plan.metrics and p.plan.companies for p in shots
    ), "a vague measure asks a specific question"


def test_every_fewshot_wording_resolves_or_asks() -> None:
    from app.engine.plan.periods import LatestFiled, resolve_period
    from app.engine.plan.schema import PeriodRef

    latest = LatestFiled(fiscal_year=2024, latest=PeriodRef(fiscal_year=2025, fiscal_period="Q2"))
    for s in load_fewshots():
        if s.plan.period_text:
            assert resolve_period(s.plan.period_text, latest).clarification is None, s.question


def test_the_canonical_wordings_in_the_prompt_are_ones_periods_py_understands() -> None:
    from app.engine.plan.periods import LatestFiled, resolve_period
    from app.engine.plan.schema import PeriodRef

    latest = LatestFiled(fiscal_year=2024, latest=PeriodRef(fiscal_year=2025, fiscal_period="Q2"))
    prompt = build_system_prompt()
    for wording in ("last year", "prior year", "most recent quarter"):
        assert f'"{wording}"' in prompt
        assert resolve_period(wording, latest).periods, wording


def test_period_text_survives_the_structured_output_transform() -> None:
    assert "period_text" in QueryPlan.model_json_schema()["properties"]
