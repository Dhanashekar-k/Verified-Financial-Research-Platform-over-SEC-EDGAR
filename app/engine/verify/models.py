"""The verifier's typed inputs and outputs (05_data_contracts.md 4.4, ADR-049).

The verifier reads an ``Answer`` and returns an ``Answer`` of the same schema plus a
``VerificationReport`` beside it. Evidence is passed in as rows, so every check runs the same on
live database rows (``evidence.py``) and on a saved trace.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.engine.answer import Claim, Refusal
from app.engine.entities.resolve_company import CompanyRecord
from app.engine.plan.catalog import MetricDef
from app.engine.plan.schema import NotFound, NumericResult, QueryPlan

CheckStatus = Literal["pass", "fail", "n/a"]
EntailmentStatus = Literal["supported", "partial", "not_supported", "error", "n/a"]
ClaimStatus = Literal["supported", "unsupported", "partial", "error"]
Action = Literal["keep", "drop", "rewrite"]
VerificationStatus = Literal["verified", "partially_verified", "refused"]

# The order in which a dropped claim is attributed to "the check that failed first".
DETERMINISTIC_CHECKS = ("provenance_exec", "quote_substring", "metadata_match", "number_in_text")
POLICY_DROP = "policy"


class Checks(BaseModel):
    """The §4.4 ``checks`` object. ``entailment`` stays ``n/a``: there is no entailment stage."""

    model_config = ConfigDict(extra="forbid")

    provenance_exec: CheckStatus = "n/a"
    metadata_match: CheckStatus = "n/a"
    number_in_text: CheckStatus = "n/a"
    quote_substring: CheckStatus = "n/a"
    entailment: EntailmentStatus = "n/a"

    def failed(self) -> list[str]:
        return [name for name in DETERMINISTIC_CHECKS if getattr(self, name) == "fail"]


class CheckOutcome(BaseModel):
    """What a check function (``numeric.py``, ``quotes.py``) found about one claim."""

    checks: Checks
    reason: str = ""  # one sentence naming what failed; empty when nothing did

    @property
    def passed(self) -> bool:
        return not self.checks.failed()


class VerifierResult(BaseModel):
    claim_id: str
    status: ClaimStatus
    checks: Checks
    action: Action
    reason: str = ""


class FactRow(BaseModel):
    """One live ``facts`` row (serving/sql/templates/facts.sql), the columns a check reads."""

    model_config = ConfigDict(extra="ignore")

    fact_id: int
    cik: int
    tag: str
    taxonomy: str
    uom: str  # per-share amounts are stored as "USD" (05 4.5)
    qtrs: int  # 0 instant, 1 a quarter, 3 nine months, 4 a year
    period_start: date | None = None
    period_end: date
    fiscal_year: int
    fiscal_period: str  # FY, Q1..Q4, and H1 / 9M on derived-Q4 parents
    value: Decimal
    form: str
    filed: date
    is_latest: bool
    is_original: bool
    is_restated: bool
    anomaly_score: float | None = None
    derived_from: list[int] | None = None  # derived Q4: [FY fact, 9M fact], value = first - second


class ChunkRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chunk_id: str
    cik: int | None = None
    form: str | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    section: str | None = None
    text: str


class Evidence(BaseModel):
    """What exists for one answer's claims. A fact or chunk absent here does not exist: ``facts``
    holds the cited and compiled facts found, with their ``derived_from`` parents; ``chunks``
    holds only cited passages **that the composer was shown**."""

    facts: dict[int, FactRow] = {}
    chunks: dict[str, ChunkRow] = {}


class CompanyName(BaseModel):
    """How one company may be named in claim text (for the "company named" wording check)."""

    cik: int
    name: str
    ticker: str | None = None
    tickers: list[str] = []  # other share classes
    aliases: list[str] = []  # brand and former names (app/engine/entities/aliases.csv)


class VerifyContext(BaseModel):
    plan: QueryPlan
    results: list[NumericResult] = []  # what the compiler built: the only legal expressions
    not_found: list[NotFound] = []
    unsupported: str | None = None  # why the numeric part could not be compiled at all
    text_only: bool = False  # a numeric plan with no catalog metric, answered from text (ADR-048)
    assumed_period: bool = False  # code chose the period; the rendered answer must say so
    allowed_ciks: frozenset[int] = frozenset()  # empty: no restriction (see ``allowed_ciks``)
    companies: dict[int, CompanyName] = {}  # tracked and plan companies
    catalog: dict[str, MetricDef] = {}


class VerifyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    rel_tolerance: Decimal = Decimal("0.005")  # D1: claim value and display only
    min_quote_words: int = 3  # D4


class PolicyDecision(BaseModel):
    """What ``policy.apply`` decides. ``kept`` holds the answer's own claims, unchanged and in
    order. Without a refusal at least one claim is kept."""

    kept: list[Claim] = []
    refusal: Refusal | None = None
    status: VerificationStatus


class VerificationReport(BaseModel):
    status: VerificationStatus
    claims_total: int
    claims_verified: int
    claims_dropped: int
    results: list[VerifierResult]
    dropped_by_check: dict[str, int] = {}


def allowed_ciks(plan: QueryPlan, universe: Collection[int] | None = None) -> frozenset[int]:
    """The companies a claim's evidence may belong to (D7): the plan's; for a ``rank`` that names
    none, the tracked universe; otherwise, with no company named, no restriction (empty)."""
    named = frozenset(c.cik for c in plan.companies if c.cik is not None)
    if named:
        return named
    return frozenset(universe or ()) if plan.operation == "rank" else frozenset()


def company_names(
    records: Iterable[CompanyRecord], aliases: Iterable[tuple[str, int]] = ()
) -> dict[int, CompanyName]:
    out = {
        r.cik: CompanyName(cik=r.cik, name=r.name, ticker=r.ticker, tickers=list(r.tickers))
        for r in records
    }
    for alias, cik in aliases:
        if cik in out:
            out[cik].aliases.append(alias)
    return out
