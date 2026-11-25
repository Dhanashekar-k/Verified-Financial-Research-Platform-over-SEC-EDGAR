"""eval/intent_accuracy.py: scoring of planner output, and the script end to end on a fake LLM."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.engine.plan.schema import CompanyRef, PeriodRef, QueryPlan
from app.llm.client import LLMClient
from app.llm.types import Usage
from eval import intent_accuracy as ia
from eval.schemas import NarrativeRow, NumericRow


def _numeric_row(**kw: Any) -> NumericRow:
    base: dict[str, Any] = {
        "id": "num-0001",
        "question": "What was Apple's net income in fiscal 2023?",
        "qtype": "single_value",
        "cik": 320193,
        "metric": "net_income",
        "fiscal_year": 2023,
        "fiscal_period": "FY",
        "gold_value": Decimal("1"),
        "unit": "USD",
        "gold_fact_ids": [1],
        "template_id": "single_v1",
        "paraphrased": False,
        "second_source_check": "match",
    }
    base.update(kw)
    return NumericRow(**base)


def _plan(**kw: Any) -> QueryPlan:
    base: dict[str, Any] = {
        "intent": "numeric",
        "companies": [CompanyRef(mention="Apple", cik=320193)],
        "periods": [PeriodRef(fiscal_year=2023)],
        "metrics": ["net_income"],
        "operation": "value",
    }
    base.update(kw)
    return QueryPlan(**base)


def test_numeric_plan_checks_all_right_and_each_way_to_be_wrong() -> None:
    row = _numeric_row()
    assert ia.numeric_plan_checks(row, _plan()) == {
        "metric": True,
        "company": True,
        "period": True,
        "operation": True,
    }
    assert not ia.numeric_plan_checks(row, _plan(metrics=["net_margin"]))["metric"]
    assert not ia.numeric_plan_checks(row, _plan(metrics=["net_income", "capex"]))["metric"]
    assert not ia.numeric_plan_checks(row, _plan(companies=[CompanyRef(mention="x")]))["company"]
    assert not ia.numeric_plan_checks(row, _plan(companies=[CompanyRef(mention="x", cik=1)]))[
        "company"
    ]
    assert not ia.numeric_plan_checks(row, _plan(periods=[PeriodRef(fiscal_year=2022)]))["period"]
    assert not ia.numeric_plan_checks(row, _plan(periods=[]))["period"]
    assert not ia.numeric_plan_checks(row, _plan(operation=None))["operation"]


def test_operation_must_match_the_question_type_and_compare_needs_both_companies() -> None:
    assert not ia.numeric_plan_checks(_numeric_row(qtype="growth"), _plan(operation="value"))[
        "operation"
    ]
    assert ia.numeric_plan_checks(_numeric_row(qtype="growth"), _plan(operation="growth"))[
        "operation"
    ]
    cmp_row = _numeric_row(qtype="compare", compare_cik=1286681)
    one = _plan(operation="compare")
    assert not ia.numeric_plan_checks(cmp_row, one)["company"]
    both = _plan(
        operation="compare",
        companies=[
            CompanyRef(mention="Apple", cik=320193),
            CompanyRef(mention="DPZ", cik=1286681),
        ],
    )
    checks = ia.numeric_plan_checks(cmp_row, both)
    assert checks["company"] and checks["operation"]


def test_growth_rows_list_both_periods_and_gold_is_the_later_one() -> None:
    row = _numeric_row(qtype="growth")
    plan = _plan(periods=[PeriodRef(fiscal_year=2022), PeriodRef(fiscal_year=2023)])
    assert ia.numeric_plan_checks(row, plan)["period"]


def test_rate_has_n_and_a_bootstrap_ci() -> None:
    r = ia.rate([True, True, False, True])
    assert r.n == 4 and r.rate == 0.75 and r.ci95 is not None and r.ci95[0] <= 0.75 <= r.ci95[1]
    assert ia.rate([]).rate is None


def test_score_plans_reports_labelled_sets_and_only_a_distribution_for_the_rest() -> None:
    num = _numeric_row()
    nar = NarrativeRow.model_construct(id="nar-001", question="Risks?")
    fb = NarrativeRow.model_construct(id="fb-1", question="?")
    items: list[tuple[str, BaseModel, str | None]] = [
        ("numeric", num, "dev"),
        ("narrative", nar, "dev"),
        ("financebench", fb, "dev"),
    ]
    plans = {
        "num-0001": _plan(),
        "nar-001": QueryPlan(intent="hybrid"),
        "fb-1": QueryPlan(intent="numeric", needs_clarification="Which company?"),
    }
    out = ia.score_plans(items, plans)
    assert out["intent_accuracy"]["numeric"]["rate"] == 1.0
    assert out["intent_accuracy"]["narrative"]["rate"] == 0.0
    assert "financebench" not in out["intent_accuracy"]  # no label exists for it
    assert out["predicted_intent_distribution"]["financebench"] == {"numeric": 1}
    assert out["needs_clarification"] == {"financebench": 1}
    assert out["confusion"]["narrative"] == {"hybrid": 1}
    assert out["numeric_plan_fields"]["metric"]["rate"] == 1.0
    by_id = {q["id"]: q for q in out["per_question"]}
    assert by_id["nar-001"]["correct"] is False and "label" not in by_id["fb-1"]


def test_main_end_to_end_on_a_fake_llm_writes_a_results_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LLM boundary is replaced; the script, planner, resolver and scoring are real."""
    calls: list[str] = []

    def fake_structured(
        self: LLMClient,
        role: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: Any,
        **kw: Any,
    ) -> tuple[Any, Usage]:
        calls.append(messages[0]["content"])
        usage = Usage(
            model="claude-haiku-5-5",
            input_tokens=10,
            cache_write_tokens=0,
            cache_read_tokens=0,
            output_tokens=5,
            cost_usd=Decimal("0.001"),
            latency_ms=1,
        )
        return QueryPlan(intent="narrative"), usage

    monkeypatch.setattr(LLMClient, "structured", fake_structured)
    code = ia.main(
        ["--split", "dev", "--sets", "narrative", "--mode", "replay", "--out", str(tmp_path)]
    )
    assert code == 0
    (path,) = tmp_path.glob("*.json")
    res = json.loads(path.read_text())
    n = len(calls)
    assert n > 0 and res["config"]["n_questions"] == n
    assert res["intent_accuracy"]["narrative"]["n"] == n
    assert res["intent_accuracy"]["narrative"]["rate"] == 1.0
    assert res["cost_usd"] == pytest.approx(0.001 * n)
    assert (
        res["config"]["planner"] == "claude-haiku-5-5"
        and len(res["config"]["system_prompt_sha256"]) == 64
    )
    assert (
        path.parent == tmp_path
    )  # report.py reads eval/results/*.json only: results/intent/ is safe


def test_stub_mode_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        ia.main(["--mode", "stub", "--out", str(tmp_path)])
    assert list(tmp_path.iterdir()) == []
