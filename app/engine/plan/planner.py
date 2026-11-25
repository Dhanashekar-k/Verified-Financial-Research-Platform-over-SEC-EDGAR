"""Planner: question -> QueryPlan with Haiku structured output (M05 section 3.3).

The system prompt is built from the metric catalog (key, label, synonyms), the period conventions,
the intent definitions and the few-shot file, and is sent with ``cache_control`` by the LLM
client. The ``metrics`` field is an enum of the catalog keys, so the model cannot invent a metric.
Companies come back as mentions only; ``attach_ciks`` fills CIKs in code (never the LLM).

The planner is the flexible front door (ADR-048): it reads loose wording, but it never picks a
date or a number. A relative period comes back as ``period_text`` with ``periods`` empty, and
``periods.resolve_period`` anchors it on what the company has filed. A measure the catalog does
not have is left unmapped, never swapped for a near one.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel

from app.engine.entities.resolve_company import CompanyIndex, clarification_for, resolve_company
from app.engine.plan.catalog import CATALOG_PATH, MetricDef, load_catalog
from app.engine.plan.schema import CompanyRef, QueryPlan
from app.llm.types import Usage

PLANNER_ROLE = "planner"
FEWSHOTS_PATH = Path(__file__).resolve().parent / "planner_fewshots.yaml"
MAX_PLAN_TOKENS = 1024

INSTRUCTIONS = """\
You turn one user question about public companies' SEC filings into a typed query plan. You never
answer the question, never compute anything and never write SQL.

Users write loosely: lower case, typos, slang, tickers, abbreviations, half sentences. Read what
they mean. Being flexible about wording is your job; guessing is not.

INTENT
- numeric: the answer is a number that a catalog metric gives.
- narrative: the answer is in the text of a filing (risk factors, MD&A, business). Also use it when
  the user names a specific measure the catalog does not have (EBITDA, free cash flow, headcount,
  sales of one product or segment): the filing text is searched and quoted.
- hybrid: both a catalog number and an explanation are asked for.
- out_of_scope: not answerable from filings of listed companies: a forecast or prediction of a
  future value ("what will X be in 2028"), stock prices, investment advice, opinions, or a topic
  that is not about a company. A question about what a company said or disclosed is NOT
  out_of_scope.

METRICS
- Use only keys from the catalog below, and only when a key means the same thing as what the user
  asked for. Everyday wording is fine when the meaning is exact: "bottom line" or "profit" is
  net_income, "top line" or "sales" is revenue, "R&D spend" is r_and_d, "cash on hand" is cash.
- Never swap in a related metric. "free cash flow" is not operating_cash_flow, "EBITDA" is not
  operating_income, "total debt" is not long_term_debt, "iPhone sales" is not revenue.
- If no key fits a measure the user named, leave metrics empty, set intent to narrative and put the
  user's measure in narrative_topics.
- If you cannot tell which measure the user wants ("how is X doing?", "how much did X make?", which
  could be revenue or profit), set needs_clarification.

PERIODS
- periods: only fiscal periods the user names: "fiscal 2023", "FY2023", "FY23", "'23", "Q3 2022",
  "2022". A two-digit year means 20xx. A bare year means the full fiscal year (fiscal_period FY).
  For growth, list the two periods earliest first.
- period_text: wording that points at a period without naming it. Leave periods empty and write:
  "last year" for the latest annual period ("latest year", "most recent annual report", "latest
  10-K"); "prior year" for the year before that; "most recent quarter" for the latest quarter or
  latest 10-Q. For any other wording ("trailing twelve months", "year to date", "this year", "the
  past few years") copy the user's words. NEVER turn such wording into a year: you do not know what
  has been filed.
- No period mentioned: leave both empty.

OTHER FIELDS
- companies: the company names or tickers as the user wrote them, in order. Fix only an obvious
  typo. Never output a CIK.
- operation: value (one number), growth (change in an amount between two periods, or year over
  year with one), compare (one metric across companies), trend (several periods), rank (the best
  companies on one metric). A margin is a value. To ask how a margin changed, use trend with both
  periods (growth of a ratio is not defined). Every plan with a metric needs an operation. Leave it
  empty for narrative intent.
- narrative_topics: short topic phrases for retrieval. section_hints: filing items likely to
  contain the answer (1 business, 1A risk factors, 1C cybersecurity, 7 MD&A, 7A market risk,
  8 financial statements); a hint, not a filter.
- needs_clarification: one short, specific question that says what is missing and, where it helps,
  offers the likely choices ("Do you mean Apple's revenue or its net income?"). Use it only when you
  cannot go on: no company, an unclear company or an unclear measure.

Return only the plan.
"""


class FewShot(BaseModel):
    question: str
    plan: QueryPlan


class LLM(Protocol):
    def structured(
        self,
        role_or_model: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        *,
        max_tokens: int = 2048,
    ) -> tuple[Any, Usage]: ...


def load_fewshots(path: Path = FEWSHOTS_PATH) -> list[FewShot]:
    raw = yaml.safe_load(path.read_text())
    return [FewShot.model_validate(item) for item in raw]


def render_catalog(catalog: Mapping[str, MetricDef]) -> str:
    lines = []
    for key, m in catalog.items():
        extra = f" (also: {', '.join(m.synonyms)})" if m.synonyms else ""
        lines.append(f"- {key}: {m.label or key}, {m.unit}{extra}")
    return "\n".join(lines)


def render_fewshots(shots: list[FewShot]) -> str:
    blocks = []
    for i, shot in enumerate(shots, start=1):
        plan_json = shot.plan.model_dump_json(exclude_defaults=True)
        blocks.append(f"Example {i}\nQuestion: {shot.question}\nPlan: {plan_json}")
    return "\n\n".join(blocks)


def build_system_prompt(
    catalog: Mapping[str, MetricDef] | None = None, shots: list[FewShot] | None = None
) -> str:
    """Deterministic: the same catalog and examples give byte-identical text (cassette keys)."""
    catalog = load_catalog(CATALOG_PATH) if catalog is None else catalog
    shots = load_fewshots() if shots is None else shots
    return (
        f"{INSTRUCTIONS}\nMETRIC CATALOG\n{render_catalog(catalog)}\n\n"
        f"EXAMPLES\n{render_fewshots(shots)}\n"
    )


def plan_question(question: str, llm: LLM, *, system: str | None = None) -> tuple[QueryPlan, Usage]:
    """One Haiku call. ``system`` defaults to ``build_system_prompt()``; pass it to avoid
    rebuilding the prompt per question."""
    plan, usage = llm.structured(
        PLANNER_ROLE,
        system if system is not None else build_system_prompt(),
        [{"role": "user", "content": question}],
        QueryPlan,
        max_tokens=MAX_PLAN_TOKENS,
    )
    if not isinstance(plan, QueryPlan):  # a mis-wired client; fail loudly
        raise TypeError(f"planner returned {type(plan).__name__}")
    # A CIK is code's, never the model's (05 4.1): drop whatever it wrote before anything reads it.
    blank = [c.model_copy(update={"cik": None}) for c in plan.companies]
    return plan.model_copy(update={"companies": blank}), usage


def attach_ciks(plan: QueryPlan, index: CompanyIndex) -> QueryPlan:
    """Set every ``CompanyRef.cik`` by code from its mention; a CIK the model wrote is discarded
    (05 4.1: ``cik`` is never the LLM's). A mention that resolves to one company gets its CIK; an
    unknown or ambiguous one leaves ``cik`` None and sets ``needs_clarification`` (an existing
    clarification is kept, else the first problem wins)."""
    companies: list[CompanyRef] = []
    clarification = plan.needs_clarification
    for ref in plan.companies:
        matches = resolve_company(ref.mention, index)
        if len(matches) == 1:
            companies.append(ref.model_copy(update={"cik": matches[0].cik}))
        else:
            companies.append(ref.model_copy(update={"cik": None}))
            options = matches or index.suggest(ref.mention)
            clarification = clarification or clarification_for(ref.mention, options)
    return plan.model_copy(update={"companies": companies, "needs_clarification": clarification})
