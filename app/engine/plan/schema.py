"""QueryPlan (planner output) and the plan compiler's result types (05_data_contracts.md 4.1, 4.5).

``MetricName`` is generated from ``metric_catalog.yaml`` at import time: the planner's JSON schema
carries the catalog keys as an enum, so the model cannot invent a metric, and a plan that names an
unknown metric fails validation. Companies are returned as ``mention`` only; code fills ``cik``.

``NumericResult`` carries ready-made provenance (``expr`` + ``fact_ids``, the 4.3 grammar) so the
composer copies it and never does arithmetic. Values are ``Decimal`` in base units.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, WithJsonSchema, model_validator

from app.engine.answer import ExprProvenance
from app.engine.plan.catalog import load_catalog

FiscalPeriodName = Literal["FY", "Q1", "Q2", "Q3", "Q4"]
Unit = Literal["USD", "ratio", "USD/shares", "shares"]

_CATALOG = load_catalog()
METRIC_NAMES: tuple[str, ...] = tuple(_CATALOG)


def _known_metric(name: str) -> str:
    if name not in _CATALOG:
        raise ValueError(f"unknown metric {name!r}; the catalog has {len(_CATALOG)} metrics")
    return name


# A str that must be a catalog key; the JSON schema is the key enum (for structured output).
MetricName = Annotated[
    str,
    AfterValidator(_known_metric),
    WithJsonSchema({"type": "string", "enum": list(METRIC_NAMES)}),
]


# --- 4.1 the plan ------------------------------------------------------------------------------


class CompanyRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mention: str  # as written by the user: "Apple", "AAPL"
    cik: int | None = None  # filled in by code (resolve_company), never by the LLM


class PeriodRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fiscal_year: int
    fiscal_period: FiscalPeriodName = "FY"


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["numeric", "narrative", "hybrid", "out_of_scope"]
    companies: list[CompanyRef] = []
    periods: list[PeriodRef] = []
    metrics: list[MetricName] = []
    operation: Literal["value", "growth", "ratio", "compare", "rank", "trend"] | None = None
    narrative_topics: list[str] = []  # "supply chain risk", "drivers of gross margin decline"
    section_hints: list[Literal["1", "1A", "1C", "7", "7A", "8", "PART1-2", "PART2-1A"]] = []
    needs_clarification: str | None = None


# --- 4.5 the compiler's results ----------------------------------------------------------------


class Note(BaseModel):
    """A fact about a result the composer must carry into the answer (when ``must_mention``)."""

    model_config = ConfigDict(extra="forbid")

    code: Literal["restated", "derived_fallback", "q4_derived"]
    text: str
    must_mention: bool = True
    latest_value: Decimal | None = None  # "restated": the latest restated value
    latest_fact_id: int | None = None  # "restated": cite it with expr "f:<latest_fact_id>"

    @model_validator(mode="after")
    def _restated_has_latest(self) -> Note:
        has_latest = self.latest_value is not None and self.latest_fact_id is not None
        if self.code == "restated" and not has_latest:
            raise ValueError("a 'restated' note needs latest_value and latest_fact_id")
        if self.code != "restated" and (
            self.latest_value is not None or self.latest_fact_id is not None
        ):
            raise ValueError("latest_value / latest_fact_id belong to 'restated' notes only")
        return self


class NumericResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str  # a catalog key
    cik: int
    fiscal_year: int  # facts.fiscal_year, never the filing's
    fiscal_period: FiscalPeriodName
    operation: Literal["value", "growth", "difference"]
    value: Decimal  # base units; ratios and growth are fractions
    unit: Unit
    expr: str  # 05 4.3 grammar over fact ids
    fact_ids: list[int]  # exactly the f: tokens of expr
    kind: Literal["reported", "derived"]  # derived = any arithmetic, incl. a fallback_formula
    resolution: (
        Literal["single_candidate", "candidates_agree", "reconciles_with_gross_profit"] | None
    ) = None
    compare_cik: int | None = None  # "difference" only: value(cik) - value(compare_cik)
    rank: int | None = None  # rank rows only, 1 = best
    notes: list[Note] = []

    @model_validator(mode="after")
    def _consistent(self) -> NumericResult:
        self.provenance()  # raises when expr is outside the 4.3 grammar or fact_ids differ
        if (
            self.kind == "reported"
            and self.expr != f"f:{self.fact_ids[0] if self.fact_ids else ''}"
        ):
            raise ValueError("a 'reported' result is exactly one fact: expr 'f:<id>'")
        if self.kind == "reported" and self.operation != "value":
            raise ValueError("only a 'value' result can be 'reported'")
        if (self.operation == "difference") != (self.compare_cik is not None):
            raise ValueError("compare_cik is set exactly for 'difference' results")
        if self.operation == "growth" and self.unit != "ratio":
            raise ValueError("a growth result is a ratio")
        if self.rank is not None and self.rank < 1:
            raise ValueError("rank starts at 1")
        return self

    def provenance(self) -> ExprProvenance:
        """The claim provenance the composer copies verbatim (validates the grammar)."""
        return ExprProvenance(type="expr", expr=self.expr, fact_ids=self.fact_ids)


NotFoundReason = Literal[
    "no_fact",
    "conflicting_candidates",
    "missing_input",
    "growth_base_missing",
    "growth_base_nonpositive",
    "division_by_zero",
    "fallback_not_allowed",
]


class NotFound(BaseModel):
    """A requested number the filings do not support. The composer turns it into a partial answer
    or a refusal; it never fills the gap."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    cik: int
    fiscal_year: int
    fiscal_period: FiscalPeriodName
    reason: NotFoundReason
    detail: str = ""
    missing_inputs: list[str] = []  # formula operands without a value ("missing_input")


class CompileOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[NumericResult] = []
    not_found: list[NotFound] = []


class UnsupportedPlan(Exception):
    """The plan cannot be compiled at all (no operation, no metric, a company without a CIK, ...).
    The engine refuses, or uses the guarded SQL fallback when ``ENGINE_SQL_FALLBACK=on``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
