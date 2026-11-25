"""QueryPlan and the compiler's result types (05_data_contracts.md 4.1 and 4.5, ADR-046)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import pytest
from pydantic import ValidationError

from app.engine.answer import ExprProvenance
from app.engine.plan.catalog import load_catalog
from app.engine.plan.schema import (
    METRIC_NAMES,
    CompanyRef,
    CompileOutcome,
    Note,
    NotFound,
    NumericResult,
    PeriodRef,
    QueryPlan,
    UnsupportedPlan,
)

NEG = -6651281536542086215


# --- the plan ----------------------------------------------------------------------------------


def test_metric_enum_is_the_catalog_keys() -> None:
    assert METRIC_NAMES == tuple(load_catalog())
    schema = QueryPlan.model_json_schema()
    items = schema["properties"]["metrics"]["items"]
    assert items == {"type": "string", "enum": list(METRIC_NAMES)}


def test_the_model_cannot_invent_a_metric() -> None:
    QueryPlan(intent="numeric", metrics=["net_income", "operating_margin"])
    with pytest.raises(ValidationError, match="unknown metric"):
        QueryPlan(intent="numeric", metrics=["ebitda"])
    with pytest.raises(ValidationError):
        QueryPlan.model_validate_json('{"intent": "numeric", "metrics": ["Net Income"]}')


def test_plan_defaults_and_strictness() -> None:
    plan = QueryPlan(intent="narrative")
    assert plan.companies == [] and plan.periods == [] and plan.metrics == []
    assert plan.operation is None and plan.needs_clarification is None
    with pytest.raises(ValidationError):
        QueryPlan.model_validate({"intent": "numeric", "extra": 1})
    with pytest.raises(ValidationError):
        QueryPlan(intent="chitchat")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PeriodRef(fiscal_year=2023, fiscal_period="H1")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        QueryPlan(intent="narrative", section_hints=["9"])  # type: ignore[list-item]


def test_company_cik_is_optional_and_filled_by_code() -> None:
    ref = CompanyRef(mention="Apple")
    assert ref.cik is None
    assert ref.model_copy(update={"cik": 320193}).cik == 320193
    assert PeriodRef(fiscal_year=2023).fiscal_period == "FY"


def test_plan_schema_is_accepted_by_the_structured_output_transform() -> None:
    """The planner sends `anthropic.transform_schema(QueryPlan schema)`; it must not raise and the
    metric enum must survive."""
    transformed: dict[str, Any] = anthropic.transform_schema(QueryPlan.model_json_schema())
    assert "net_income" in json.dumps(transformed)
    assert json.dumps(transformed).count('"enum"') >= 1


def test_catalog_path_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert tuple(load_catalog()) == METRIC_NAMES


# --- NumericResult -----------------------------------------------------------------------------


def _result(**kw: Any) -> NumericResult:
    base: dict[str, Any] = {
        "metric": "net_income",
        "cik": 320193,
        "fiscal_year": 2023,
        "fiscal_period": "FY",
        "operation": "value",
        "value": Decimal("96995000000"),
        "unit": "USD",
        "expr": "f:111",
        "fact_ids": [111],
        "kind": "reported",
    }
    base.update(kw)
    return NumericResult(**base)


def test_reported_result_and_its_provenance() -> None:
    r = _result()
    prov = r.provenance()
    assert isinstance(prov, ExprProvenance)
    assert (prov.expr, prov.fact_ids) == ("f:111", [111])
    assert r.value == Decimal("96995000000") and isinstance(r.value, Decimal)


def test_negative_fact_ids_are_citable() -> None:
    r = _result(expr=f"f:{NEG}", fact_ids=[NEG])
    assert r.provenance().fact_ids == [NEG]


def test_derived_growth_and_difference() -> None:
    g = _result(
        operation="growth",
        value=Decimal("-0.4166666667"),
        unit="ratio",
        expr=f"(f:{NEG} - f:5) / f:5",
        fact_ids=[NEG, 5],
        kind="derived",
    )
    assert g.provenance().expr == f"(f:{NEG} - f:5) / f:5"
    d = _result(
        operation="difference",
        compare_cik=1286681,
        expr="f:1 - f:2",
        fact_ids=[1, 2],
        kind="derived",
    )
    assert d.compare_cik == 1286681


@pytest.mark.parametrize(
    "bad",
    [
        {"expr": "f:111 + f:222"},  # tokens not in fact_ids
        {"expr": "f:1", "fact_ids": [111]},  # expr/fact_ids mismatch
        {"expr": "abs(f:111)"},  # outside the grammar
        {"kind": "reported", "expr": "f:111 * 2"},  # reported is exactly one fact
        {"kind": "reported", "operation": "growth", "unit": "ratio"},
        {"operation": "difference"},  # needs compare_cik
        {"compare_cik": 5},  # compare_cik only on difference
        {"operation": "growth", "kind": "derived", "expr": "f:1 / f:2", "fact_ids": [1, 2]},
        {"rank": 0},
        {"fiscal_period": "H1"},
        {"unit": "EUR"},
        {"unexpected": 1},
    ],
)
def test_invalid_results(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _result(**bad)


def test_value_is_decimal_not_float() -> None:
    r = NumericResult.model_validate_json(
        '{"metric": "net_income", "cik": 1, "fiscal_year": 2023, "fiscal_period": "FY",'
        ' "operation": "value", "value": "0.1", "unit": "USD", "expr": "f:1", "fact_ids": [1],'
        ' "kind": "reported"}'
    )
    assert r.value == Decimal("0.1")
    assert Decimal("0.1") + Decimal("0.2") == Decimal(
        "0.3"
    )  # the arithmetic the compiler relies on


# --- Note, NotFound, outcome -------------------------------------------------------------------


def test_restated_note_needs_the_latest_value_and_fact() -> None:
    n = Note(code="restated", text="restated", latest_value=Decimal("1"), latest_fact_id=NEG)
    assert n.must_mention is True
    with pytest.raises(ValidationError):
        Note(code="restated", text="restated")
    with pytest.raises(ValidationError):
        Note(code="q4_derived", text="x", latest_value=Decimal("1"), latest_fact_id=1)
    Note(code="derived_fallback", text="gross profit = revenue - cost of revenue")


def test_not_found_reasons_are_closed() -> None:
    nf = NotFound(
        metric="gross_margin",
        cik=1,
        fiscal_year=2023,
        fiscal_period="FY",
        reason="missing_input",
        missing_inputs=["revenue"],
    )
    assert nf.missing_inputs == ["revenue"]
    with pytest.raises(ValidationError):
        NotFound(metric="x", cik=1, fiscal_year=2023, fiscal_period="FY", reason="who knows")  # type: ignore[arg-type]


def test_outcome_round_trips_and_unsupported_plan_carries_a_reason() -> None:
    out = CompileOutcome(
        results=[_result()],
        not_found=[
            NotFound(
                metric="net_income",
                cik=2,
                fiscal_year=2023,
                fiscal_period="FY",
                reason="no_fact",
            )
        ],
    )
    assert CompileOutcome.model_validate_json(out.model_dump_json()) == out
    err = UnsupportedPlan("no operation on a numeric plan")
    assert err.reason == "no operation on a numeric plan" and str(err) == err.reason
