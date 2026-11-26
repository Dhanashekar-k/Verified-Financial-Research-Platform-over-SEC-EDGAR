"""Typed claims, evidence and contexts for the verifier tests (M06, ADR-049).

Nothing here calls a verifier core (``numbers``, ``numeric``, ``quotes``, ``policy``): these are
inputs only, checked on their own by ``test_verify_fixtures.py``. Synthetic cases use round,
hand-checkable figures; ``dev_case`` loads a real claim from the saved Gate 0 dev trace.

The figures below are test inputs, not measurements and not a statement about any filing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from app.engine.answer import ChunkProvenance, Claim, ExprProvenance
from app.engine.compose.format import format_value
from app.engine.plan.catalog import MetricDef, load_catalog
from app.engine.plan.schema import CompanyRef, Note, NumericResult, PeriodRef, QueryPlan
from app.engine.verify.models import (
    ChunkRow,
    CompanyName,
    Evidence,
    FactRow,
    VerifyContext,
)
from eval.verifier_inputs import (
    Reference,
    context_for,
    evidence_for,
    load_reference,
    load_trace,
    row_by_id,
)
from eval.verifier_trace import Trace, TraceRow

ROOT = Path(__file__).resolve().parents[4]

APPLE = 320193
MICROSOFT = 789019
IBM = 51143
ALPHABET = 1652044
JPMORGAN = 19617
WALMART = 104169

REVENUE_TAG = "RevenueFromContractWithCustomerExcludingAssessedTax"

# Each company carries a short alias so that the "wrong company" tests do not depend on how a
# legal suffix ("Corp", "Inc.") is matched; that question is left open (see the report).
COMPANIES: dict[int, CompanyName] = {
    APPLE: CompanyName(cik=APPLE, name="Apple Inc.", ticker="AAPL", aliases=["Apple"]),
    MICROSOFT: CompanyName(cik=MICROSOFT, name="Microsoft Corp", ticker="MSFT",
                           aliases=["Microsoft"]),
    IBM: CompanyName(cik=IBM, name="International Business Machines Corp", ticker="IBM",
                     aliases=["Big Blue"]),
    ALPHABET: CompanyName(cik=ALPHABET, name="Alphabet Inc.", ticker="GOOGL", tickers=["GOOG"],
                          aliases=["Google"]),
    JPMORGAN: CompanyName(cik=JPMORGAN, name="JPMorgan Chase & Co", ticker="JPM",
                          aliases=["Chase", "JPMorgan"]),
    WALMART: CompanyName(cik=WALMART, name="Walmart Inc.", ticker="WMT", aliases=["Walmart"]),
}  # fmt: skip

# Fact ids. REV23 is negative on purpose elsewhere (see NEGATIVE_ID).
REV23, REV22, REV21 = 101, 102, 103
OPINC23, COGS23 = 111, 121
MSFT_REV23 = 201
Q4_FY, Q4_9M, Q4 = 301, 302, 303
EPS23 = 401
REV23_LATEST = 501
ASSETS21 = 601
WMT_REV23 = 701
NEGATIVE_ID = -6651281536542086215


@lru_cache(maxsize=1)
def catalog() -> dict[str, MetricDef]:
    return load_catalog()


class Case(NamedTuple):
    claim: Claim
    evidence: Evidence
    ctx: VerifyContext


# --- builders ------------------------------------------------------------------------------------


def fact(
    fact_id: int,
    value: Decimal | int | str,
    *,
    cik: int = APPLE,
    tag: str = REVENUE_TAG,
    taxonomy: str = "us-gaap",
    uom: str = "USD",
    qtrs: int = 4,
    fiscal_year: int = 2023,
    fiscal_period: str = "FY",
    period_end: date | None = None,
    form: str = "10-K",
    filed: date | None = None,
    is_latest: bool = True,
    is_original: bool = True,
    is_restated: bool = False,
    derived_from: list[int] | None = None,
) -> FactRow:
    end = period_end or date(fiscal_year, 9, 30)
    return FactRow(
        fact_id=fact_id,
        cik=cik,
        tag=tag,
        taxonomy=taxonomy,
        uom=uom,
        qtrs=qtrs,
        period_end=end,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        value=Decimal(value),
        form=form,
        filed=filed or date(end.year, 11, 3),
        is_latest=is_latest,
        is_original=is_original,
        is_restated=is_restated,
        derived_from=derived_from,
    )


def result(
    value: Decimal | int | str,
    fact_ids: list[int],
    *,
    metric: str = "revenue",
    cik: int = APPLE,
    fiscal_year: int = 2023,
    fiscal_period: str = "FY",
    operation: str = "value",
    unit: str = "USD",
    expr: str | None = None,
    kind: str | None = None,
    compare_cik: int | None = None,
    rank: int | None = None,
    notes: Sequence[Note] = (),
) -> NumericResult:
    expr = expr if expr is not None else f"f:{fact_ids[0]}"
    if kind is None:
        kind = "reported" if operation == "value" and expr == f"f:{fact_ids[0]}" else "derived"
    return NumericResult(
        metric=metric,
        cik=cik,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        operation=operation,
        value=Decimal(value),
        unit=unit,
        expr=expr,
        fact_ids=fact_ids,
        kind=kind,
        compare_cik=compare_cik,
        rank=rank,
        notes=list(notes),
    )


def numeric_claim(
    text: str,
    res: NumericResult,
    *,
    claim_id: str = "c1",
    value: Decimal | int | str | None = None,
    display: str | None = None,
    primary: bool = False,
) -> Claim:
    """The claim the composer would write for ``res``: its provenance and value copied, its
    display formatted by the composer's own formatter unless one is given."""
    return Claim(
        id=claim_id,
        kind="numeric" if res.kind == "reported" else "numeric_derived",
        text=text,
        value=res.value if value is None else Decimal(value),
        unit=res.unit,
        display=display if display is not None else format_value(res.value, res.unit),
        primary=primary,
        provenance=res.provenance(),
    )


def expr_claim(
    text: str,
    expr: str,
    fact_ids: list[int],
    value: Decimal | int | str | None,
    *,
    claim_id: str = "c1",
    display: str | None = None,
    unit: str = "USD",
) -> Claim:
    """A claim with any provenance expression, for expressions the compiler did not build."""
    return Claim(
        id=claim_id,
        kind="numeric_derived",
        text=text,
        value=None if value is None else Decimal(value),
        unit=unit,
        display=display,
        provenance=ExprProvenance(type="expr", expr=expr, fact_ids=fact_ids),
    )


def text_claim(
    text: str, quote: str, chunk_id: str = "chunk-1", *, claim_id: str = "t1", primary: bool = False
) -> Claim:
    return Claim(
        id=claim_id,
        kind="text",
        text=text,
        primary=primary,
        provenance=ChunkProvenance(type="chunk", chunk_id=chunk_id, quote=quote),
    )


def edit(claim: Claim, **changes: Any) -> Claim:
    """``claim`` with some fields replaced, re-validated."""
    return Claim.model_validate({**claim.model_dump(), **changes})


def plan(
    *,
    intent: str = "numeric",
    ciks: Sequence[int] = (APPLE,),
    years: Sequence[int] = (2023,),
    fiscal_period: str = "FY",
    metrics: Sequence[str] = ("revenue",),
    operation: str | None = "value",
) -> QueryPlan:
    return QueryPlan(
        intent=intent,
        companies=[CompanyRef(mention=COMPANIES[c].name, cik=c) for c in ciks],
        periods=[PeriodRef(fiscal_year=y, fiscal_period=fiscal_period) for y in years],
        metrics=list(metrics),
        operation=operation,
    )


def context(
    results: Sequence[NumericResult],
    query: QueryPlan | None = None,
    *,
    allowed: frozenset[int] | None = None,
    **extra: Any,
) -> VerifyContext:
    query = query if query is not None else plan()
    if allowed is None:
        allowed = frozenset(c.cik for c in query.companies if c.cik is not None)
    return VerifyContext(
        plan=query,
        results=list(results),
        allowed_ciks=allowed,
        companies=COMPANIES,
        catalog=catalog(),
        **extra,
    )


def evidence(*facts: FactRow, chunks: Sequence[ChunkRow] = ()) -> Evidence:
    return Evidence(facts={f.fact_id: f for f in facts}, chunks={c.chunk_id: c for c in chunks})


def replace_fact(ev: Evidence, fact_id: int, **changes: Any) -> Evidence:
    """``ev`` with one fact row altered: what a live read would see if the row differed."""
    row = FactRow.model_validate({**ev.facts[fact_id].model_dump(), **changes})
    return Evidence(facts={**ev.facts, fact_id: row}, chunks=ev.chunks)


def drop_fact(ev: Evidence, fact_id: int) -> Evidence:
    return Evidence(
        facts={i: f for i, f in ev.facts.items() if i != fact_id}, chunks=dict(ev.chunks)
    )


# --- numeric scenarios ---------------------------------------------------------------------------


def revenue_case(
    text: str = "Apple's revenue in fiscal 2023 was $383.3 billion.",
    *,
    value: Decimal | int | str | None = None,
    display: str | None = None,
) -> Case:
    """One reported fact: 383,285,000,000 USD, FY2023, period end 2023-09-30."""
    res = result("383285000000", [REV23])
    return Case(
        numeric_claim(text, res, value=value, display=display),
        evidence(fact(REV23, "383285000000")),
        context([res]),
    )


def margin_case(text: str = "Apple's FY2023 operating margin was 29.8%.") -> Case:
    """A ratio over two USD facts."""
    value = Decimal("114301000000") / Decimal("383285000000")
    res = result(
        value,
        [OPINC23, REV23],
        metric="operating_margin",
        unit="ratio",
        expr=f"f:{OPINC23} / f:{REV23}",
    )
    return Case(
        numeric_claim(text, res),
        evidence(
            fact(OPINC23, "114301000000", tag="OperatingIncomeLoss"),
            fact(REV23, "383285000000"),
        ),
        context([res], plan(metrics=["operating_margin"], operation="ratio")),
    )


def fallback_gross_profit_case() -> Case:
    """gross_profit computed through its fallback formula: the cited tags are its operands'."""
    res = result(
        "169148000000",
        [REV23, COGS23],
        metric="gross_profit",
        expr=f"f:{REV23} - f:{COGS23}",
    )
    return Case(
        numeric_claim("Apple's FY2023 gross profit was $169.1 billion.", res),
        evidence(
            fact(REV23, "383285000000"),
            fact(COGS23, "214137000000", tag="CostOfGoodsAndServicesSold"),
        ),
        context([res], plan(metrics=["gross_profit"])),
    )


def growth_case(
    text: str = "Apple's revenue decreased 36.7% from fiscal 2022 to fiscal 2023.",
) -> Case:
    """633,000,000 over a base of 1,000,000,000: growth is exactly -0.367."""
    res = result(
        "-0.367",
        [REV23, REV22],
        operation="growth",
        unit="ratio",
        expr=f"(f:{REV23} - f:{REV22}) / f:{REV22}",
    )
    return Case(
        numeric_claim(text, res),
        evidence(fact(REV23, "633000000"), fact(REV22, "1000000000", fiscal_year=2022)),
        context([res], plan(operation="growth")),
    )


def two_year_growth_case(
    text: str = "Apple's revenue grew 4.8% from fiscal 2021 to fiscal 2023.",
    *,
    base_year: int = 2021,
) -> Case:
    """The plan names 2021 and 2023, so the base is not t-1. ``base_year`` sets the year on the
    cited base fact."""
    value = (Decimal("383285000000") - Decimal("365817000000")) / Decimal("365817000000")
    res = result(
        value,
        [REV23, REV21],
        operation="growth",
        unit="ratio",
        expr=f"(f:{REV23} - f:{REV21}) / f:{REV21}",
    )
    return Case(
        numeric_claim(text, res),
        evidence(
            fact(REV23, "383285000000"),
            fact(REV21, "365817000000", fiscal_year=base_year),
        ),
        context([res], plan(years=[2021, 2023], operation="growth")),
    )


def compare_results() -> tuple[NumericResult, NumericResult, NumericResult]:
    """(Apple value, Microsoft value, Apple minus Microsoft)."""
    apple = result("383285000000", [REV23])
    msft = result("211915000000", [MSFT_REV23], cik=MICROSOFT)
    diff = result(
        "171370000000",
        [REV23, MSFT_REV23],
        operation="difference",
        compare_cik=MICROSOFT,
        expr=f"f:{REV23} - f:{MSFT_REV23}",
    )
    return apple, msft, diff


def compare_evidence() -> Evidence:
    return evidence(
        fact(REV23, "383285000000"),
        fact(MSFT_REV23, "211915000000", cik=MICROSOFT, period_end=date(2023, 6, 30)),
    )


def compare_context() -> VerifyContext:
    return context(compare_results(), plan(ciks=[APPLE, MICROSOFT], operation="compare"))


def compare_case(which: int, text: str) -> Case:
    """``which``: 0 Apple's value, 1 Microsoft's value, 2 the difference."""
    return Case(
        numeric_claim(text, compare_results()[which]), compare_evidence(), compare_context()
    )


def reverse_compare_case(text: str) -> Case:
    """Microsoft minus Apple: a negative difference."""
    diff = result(
        "-171370000000",
        [MSFT_REV23, REV23],
        cik=MICROSOFT,
        operation="difference",
        compare_cik=APPLE,
        expr=f"f:{MSFT_REV23} - f:{REV23}",
    )
    return Case(
        numeric_claim(text, diff),
        compare_evidence(),
        context([diff], plan(ciks=[MICROSOFT, APPLE], operation="compare")),
    )


def derived_q4_case(text: str = "Apple's Q4 FY2023 revenue was $30.0 billion.") -> Case:
    """A derived Q4 row: FY 100bn minus nine months 70bn."""
    res = result(
        "30000000000",
        [Q4],
        fiscal_period="Q4",
        notes=[Note(code="q4_derived", text="Q4 is FY minus nine months.")],
    )
    return Case(
        numeric_claim(text, res),
        evidence(
            fact(Q4, "30000000000", qtrs=1, fiscal_period="Q4", derived_from=[Q4_FY, Q4_9M]),
            fact(Q4_FY, "100000000000"),
            fact(Q4_9M, "70000000000", qtrs=3, fiscal_period="9M", period_end=date(2023, 6, 30),
                 form="10-Q"),
        ),
        context([res], plan(fiscal_period="Q4")),
    )  # fmt: skip


def quarter_case(text: str = "Apple's Q2 FY2023 revenue was $94.8 billion.") -> Case:
    res = result("94836000000", [REV23], fiscal_period="Q2")
    return Case(
        numeric_claim(text, res),
        evidence(
            fact(REV23, "94836000000", qtrs=1, fiscal_period="Q2",
                 period_end=date(2023, 4, 1), form="10-Q")
        ),
        context([res], plan(fiscal_period="Q2")),
    )  # fmt: skip


def eps_case(*, uom: str = "USD") -> Case:
    """Diluted EPS: the lake stores per-share amounts with uom "USD"."""
    res = result("6.13", [EPS23], metric="eps_diluted", unit="USD/shares")
    return Case(
        numeric_claim("Apple's FY2023 diluted EPS was $6.13 per share.", res),
        evidence(fact(EPS23, "6.13", tag="EarningsPerShareDiluted", uom=uom)),
        context([res], plan(metrics=["eps_diluted"])),
    )


def restated_note() -> Note:
    return Note(
        code="restated",
        text="Revenue was later restated.",
        latest_value=Decimal("390000000000"),
        latest_fact_id=REV23_LATEST,
    )


def restated_result() -> NumericResult:
    return result("383285000000", [REV23], notes=[restated_note()])


def restated_evidence() -> Evidence:
    return evidence(
        fact(REV23, "383285000000", is_latest=False, is_restated=True),
        fact(REV23_LATEST, "390000000000", is_original=False, is_restated=True,
             filed=date(2024, 11, 1)),
    )  # fmt: skip


def restated_latest_case(
    text: str = "Apple's FY2023 revenue was later restated to $390.0 billion.",
    *,
    value: str = "390000000000",
) -> Case:
    """The latest restated value, cited as the composer is told to: expr f:<latest_fact_id>."""
    claim = expr_claim(
        text,
        f"f:{REV23_LATEST}",
        [REV23_LATEST],
        value,
        claim_id="c2",
        display="$390.0 billion",
    )
    return Case(claim, restated_evidence(), context([restated_result()]))


def instant_assets_case(
    text: str = "Apple's total assets were $351.0 billion at December 31, 2021.",
) -> Case:
    res = result("351002000000", [ASSETS21], metric="total_assets", fiscal_year=2021)
    return Case(
        numeric_claim(text, res),
        evidence(
            fact(ASSETS21, "351002000000", tag="Assets", qtrs=0, fiscal_year=2021,
                 period_end=date(2021, 12, 31))
        ),
        context([res], plan(years=[2021], metrics=["total_assets"])),
    )  # fmt: skip


def january_fye_case(
    text: str = "Walmart's revenue for fiscal 2023, ended January 31, 2024, was $648.1 billion.",
) -> Case:
    """A fiscal 2023 whose period ends in January 2024."""
    res = result("648125000000", [WMT_REV23], cik=WALMART)
    return Case(
        numeric_claim(text, res),
        evidence(fact(WMT_REV23, "648125000000", cik=WALMART, period_end=date(2024, 1, 31))),
        context([res], plan(ciks=[WALMART])),
    )


# --- text scenarios ------------------------------------------------------------------------------

PASSAGE = (
    "Item 7. Management's Discussion and Analysis. Net sales decreased 3% during 2023 compared "
    "to 2022. Gross margin was 44.1% of net sales. The decrease was primarily driven by foreign "
    "currency headwinds.\n"
    "Products | $ | 298,085 | 316,199"
)


def chunk(text: str = PASSAGE, *, chunk_id: str = "chunk-1", cik: int | None = APPLE) -> ChunkRow:
    return ChunkRow(chunk_id=chunk_id, cik=cik, form="10-K", fiscal_year=2023, text=text)


def text_case(
    claim_text: str,
    quote: str,
    *,
    passage: str = PASSAGE,
    cik: int | None = APPLE,
    query: QueryPlan | None = None,
    allowed: frozenset[int] | None = None,
) -> Case:
    query = query if query is not None else plan(intent="narrative", metrics=[], operation=None)
    return Case(
        text_claim(claim_text, quote),
        evidence(chunks=[chunk(passage, cik=cik)]),
        context([], query, allowed=allowed),
    )


# --- the saved Gate 0 dev trace ------------------------------------------------------------------


@lru_cache(maxsize=1)
def dev_trace() -> Trace:
    return load_trace(ROOT / "eval/results/verifier/gate0_dev_trace.json")


@lru_cache(maxsize=1)
def dev_reference() -> Reference:
    return load_reference(
        ROOT / "eval/results/verifier/gate0_dev_companies.json",
        ROOT / "eval/results/verifier/gate0_dev_tracked_ciks.csv",
    )


def dev_row(question_id: str) -> TraceRow:
    return row_by_id(dev_trace(), question_id)


def dev_claim(question_id: str, claim_id: str) -> Claim:
    answer = dev_row(question_id).answer
    assert answer is not None, f"{question_id} has no answer in the trace"
    [claim] = [c for c in answer.claims if c.id == claim_id]
    return claim


def dev_case(question_id: str, claim_id: str) -> Case:
    """A real dev claim with exactly the evidence and context the engine would hand a check."""
    row = dev_row(question_id)
    return Case(
        dev_claim(question_id, claim_id),
        evidence_for(dev_trace(), row),
        context_for(row, dev_reference()),
    )


# --- real company records (ADR-049, decision A) ---------------------------------------------------


def with_real_companies(case: Case) -> Case:
    """``case`` with the tracked companies as the engine knows them: filed names such as
    "MICROSOFT CORP", no hand-made short aliases, and tickers that are also words."""
    return case._replace(ctx=case.ctx.model_copy(update={"companies": dev_reference().companies}))


def restated_difference_case(text: str) -> Case:
    """Apple's latest restated value cited under a compare: the note sits on the difference."""
    apple, msft, diff = compare_results()
    diff = diff.model_copy(update={"notes": [restated_note()]})
    claim = expr_claim(
        text,
        f"f:{REV23_LATEST}",
        [REV23_LATEST],
        "390000000000",
        claim_id="c9",
        display="$390.0 billion",
    )
    facts = [*compare_evidence().facts.values(), restated_evidence().facts[REV23_LATEST]]
    ctx = context([apple, msft, diff], plan(ciks=[APPLE, MICROSOFT], operation="compare"))
    return Case(claim, evidence(*facts), ctx)
