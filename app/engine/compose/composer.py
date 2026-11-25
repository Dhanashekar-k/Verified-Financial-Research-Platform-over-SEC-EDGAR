"""Composer: question + typed numbers + passages -> Answer (M05 section 3.7).

The model writes prose and copies provenance; it never computes and never writes SQL. Numbers
arrive as ``NumericResult``s with a ready-made display string and provenance, text arrives as
passages with chunk ids. The system prompt is cached by the LLM client. The output is the same
``Answer`` schema the baseline writes, so one set of metrics scores both.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.engine.answer import Answer
from app.engine.compose.format import format_value
from app.engine.plan.planner import LLM
from app.engine.plan.schema import NotFound, NumericResult, QueryPlan
from app.engine.retrieval.query import RankedChunk
from app.llm.types import Usage

COMPOSER_ROLE = "composer"
MAX_ANSWER_TOKENS = 2048
TEXT_ONLY_NOTE = (
    "The question's measure is not one that is computed from XBRL facts, so there are no NUMERIC "
    "RESULTS. Rule 9 applies."
)

COMPOSER_SYSTEM = """\
You answer a question about public companies from two kinds of material only: NUMERIC RESULTS
(figures computed from XBRL facts) and PASSAGES (text from SEC filings). Nothing else is evidence.

Rules, all of them binding:
1. Every claim has exactly one provenance and every sentence of the answer rests on a claim.
2. A numeric claim (kind "numeric", or "numeric_derived" for a computed one) may state only a
   value from NUMERIC RESULTS. Copy that result's `provenance` object verbatim (type, expr,
   fact_ids), copy its `value` exactly, and use its `display` string as the claim's `display`.
   DO NOT DO ARITHMETIC: no sums, differences, ratios, percentages, rounding, unit changes or
   growth rates of your own. If a number you would like to state is not in NUMERIC RESULTS, do
   not state it.
3. A number that appears only in a passage may be stated only in a claim of kind "text" whose
   `quote` contains that number, and the number must appear in the claim text exactly as it
   appears in the quote.
4. A text claim's provenance is {"type": "chunk", "chunk_id": <a passage id>, "quote": <an exact,
   contiguous substring of that passage>}. Copy the quote character for character.
5. A result with a note marked must_mention (restated, derived fallback, derived Q4) must be
   mentioned in the answer. A restated value is as originally reported; state the latest
   restated value only through the note's own latest_provenance, and when a note has none, say
   that it was restated without giving a number. For a growth result, say which period it is
   measured from (`growth_from`).
6. If the material does not answer the question, or answers only part of it, say what is
   missing: set `refusal` {"reason": one sentence, "found": [what the material does support],
   "missing": [what would have been needed]}. List a NOT FOUND result under "missing". Never
   fill a gap from memory or guess. You may keep supported claims in a partial answer.
7. `answer_markdown` cites claims with [n] markers in the order of `claims`. Mark the claim that
   answers the question directly with "primary": true (one claim).
8. State a period the user did not name only when a PERIOD NOTE says how it was chosen: then say
   which fiscal period the figures are for.
9. When TEXT ONLY is present, no computed figure exists for what was asked. Answer from PASSAGES
   with claims of kind "text" and verbatim quotes (rules 3 and 4); write no numeric claim. Set
   `refusal` only if the passages do not answer the question.
"""


def _shows_latest(r: NumericResult) -> bool:
    """Whether a restated note's latest value and fact id go to the composer. On a ratio or growth
    result the restated figure is an INPUT in a unit the note does not carry: showing it in the
    result's unit would be wrong."""
    return r.unit != "ratio"


def citable_fact_ids(results: Sequence[NumericResult]) -> set[int]:
    """Every fact id the composer is handed: each result's ``fact_ids``, and the id of a restated
    value it is shown. The harness rejects a claim citing any other id."""
    ids = {i for r in results for i in r.fact_ids}
    ids |= {
        n.latest_fact_id
        for r in results
        if _shows_latest(r)
        for n in r.notes
        if n.latest_fact_id is not None
    }
    return ids


def _label(fiscal_year: int, fiscal_period: str) -> str:
    return f"FY{fiscal_year}" if fiscal_period == "FY" else f"{fiscal_period} FY{fiscal_year}"


def growth_base(r: NumericResult, plan: QueryPlan) -> str:
    """The period a growth rate is measured from, as the compiler chose it (05 4.5): the earlier
    of two named periods, else the same period a year before."""
    years = sorted({p.fiscal_year for p in plan.periods})
    year = years[0] if len(years) == 2 else r.fiscal_year - 1
    return _label(year, r.fiscal_period)


def result_view(
    r: NumericResult, names: Mapping[int, str], rid: str, compared_with: str | None = None
) -> dict[str, Any]:
    prov = r.provenance()
    view: dict[str, Any] = {
        "id": rid,
        "metric": r.metric,
        "company": names.get(r.cik, f"CIK {r.cik}"),
        "period": _label(r.fiscal_year, r.fiscal_period),
        "operation": r.operation,
        "value": str(r.value),
        "unit": r.unit,
        "display": format_value(r.value, r.unit),
        "kind": "numeric" if r.kind == "reported" else "numeric_derived",
        "provenance": {"type": prov.type, "expr": prov.expr, "fact_ids": prov.fact_ids},
    }
    if compared_with is not None:
        view["growth_from"] = compared_with
    if r.compare_cik is not None:
        view["minus_company"] = names.get(r.compare_cik, f"CIK {r.compare_cik}")
    if r.rank is not None:
        view["rank"] = r.rank
    if r.notes:
        view["notes"] = [
            {
                "code": n.code,
                "text": n.text,
                "must_mention": n.must_mention,
                **(
                    {
                        "latest_value": str(n.latest_value),
                        "latest_display": format_value(n.latest_value, r.unit),
                        "latest_provenance": {
                            "type": "expr",
                            "expr": f"f:{n.latest_fact_id}",
                            "fact_ids": [n.latest_fact_id],
                        },
                    }
                    if n.latest_value is not None
                    and n.latest_fact_id is not None
                    and _shows_latest(r)
                    else {}
                ),
            }
            for n in r.notes
        ]
    return view


def build_messages(
    question: str,
    plan: QueryPlan,
    results: Sequence[NumericResult],
    not_found: Sequence[NotFound],
    chunks: Sequence[RankedChunk],
    names: Mapping[int, str],
    assumed_period: str | None = None,
    unsupported: str | None = None,
    text_only: bool = False,
) -> list[dict[str, Any]]:
    parts = [f"QUESTION\n{question}"]
    if text_only:
        parts.append(f"TEXT ONLY\n{TEXT_ONLY_NOTE}")
    if assumed_period:
        parts.append(f"PERIOD NOTE\n{assumed_period}")
    if results:
        views = [
            result_view(
                r,
                names,
                f"r{i}",
                growth_base(r, plan) if r.operation == "growth" else None,
            )
            for i, r in enumerate(results, start=1)
        ]
        parts.append("NUMERIC RESULTS\n" + json.dumps(views, indent=1, ensure_ascii=False))
    if not_found or unsupported:
        nf = [
            {
                "metric": n.metric,
                "company": names.get(n.cik, f"CIK {n.cik}"),
                "period": f"FY{n.fiscal_year}"
                if n.fiscal_period == "FY"
                else f"{n.fiscal_period} FY{n.fiscal_year}",
                "reason": n.reason,
                "detail": n.detail,
            }
            for n in not_found
        ]
        if unsupported:  # the whole numeric part could not be computed: say so, never skip it
            nf.append(
                {
                    "metric": "(the numeric part of the question)",
                    "company": "-",
                    "period": "-",
                    "reason": "unsupported_plan",
                    "detail": unsupported,
                }
            )
        parts.append("NOT FOUND\n" + json.dumps(nf, indent=1, ensure_ascii=False))
    if chunks:
        passages = [
            f"[chunk_id: {c.chunk_id} | section: {c.section or '-'}]\n{c.text}" for c in chunks
        ]
        parts.append("PASSAGES\n" + "\n\n".join(passages))
    return [{"role": "user", "content": "\n\n".join(parts)}]


def compose(
    question: str,
    plan: QueryPlan,
    results: Sequence[NumericResult],
    not_found: Sequence[NotFound],
    chunks: Sequence[RankedChunk],
    names: Mapping[int, str],
    llm: LLM,
    *,
    assumed_period: str | None = None,
    unsupported: str | None = None,
    text_only: bool = False,
    role: str = COMPOSER_ROLE,
) -> tuple[Answer, Usage]:
    answer, usage = llm.structured(
        role,
        COMPOSER_SYSTEM,
        build_messages(
            question,
            plan,
            results,
            not_found,
            chunks,
            names,
            assumed_period,
            unsupported,
            text_only,
        ),
        Answer,
        max_tokens=MAX_ANSWER_TOKENS,
    )
    if not isinstance(answer, Answer):
        raise TypeError(f"composer returned {type(answer).__name__}")
    return answer, usage
