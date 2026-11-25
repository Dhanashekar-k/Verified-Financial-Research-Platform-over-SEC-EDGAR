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
   restated value only through the note's own latest_provenance.
6. If the material does not answer the question, or answers only part of it, say what is
   missing: set `refusal` {"reason": one sentence, "found": [what the material does support],
   "missing": [what would have been needed]}. List a NOT FOUND result under "missing". Never
   fill a gap from memory or guess. You may keep supported claims in a partial answer.
7. `answer_markdown` cites claims with [n] markers in the order of `claims`. Mark the claim that
   answers the question directly with "primary": true (one claim).
8. State a period the user did not name only when the PERIOD NOTE says it was assumed: then say
   which fiscal year the figures are for.
"""


def result_view(r: NumericResult, names: Mapping[int, str], rid: str) -> dict[str, Any]:
    prov = r.provenance()
    view: dict[str, Any] = {
        "id": rid,
        "metric": r.metric,
        "company": names.get(r.cik, f"CIK {r.cik}"),
        "period": f"FY{r.fiscal_year}"
        if r.fiscal_period == "FY"
        else f"{r.fiscal_period} FY{r.fiscal_year}",
        "operation": r.operation,
        "value": str(r.value),
        "unit": r.unit,
        "display": format_value(r.value, r.unit),
        "kind": "numeric" if r.kind == "reported" else "numeric_derived",
        "provenance": {"type": prov.type, "expr": prov.expr, "fact_ids": prov.fact_ids},
    }
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
                    if n.latest_value is not None and n.latest_fact_id is not None
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
) -> list[dict[str, Any]]:
    parts = [f"QUESTION\n{question}"]
    if assumed_period:
        parts.append(f"PERIOD NOTE\n{assumed_period}")
    if results:
        views = [result_view(r, names, f"r{i}") for i, r in enumerate(results, start=1)]
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
    role: str = COMPOSER_ROLE,
) -> tuple[Answer, Usage]:
    answer, usage = llm.structured(
        role,
        COMPOSER_SYSTEM,
        build_messages(
            question, plan, results, not_found, chunks, names, assumed_period, unsupported
        ),
        Answer,
        max_tokens=MAX_ANSWER_TOKENS,
    )
    if not isinstance(answer, Answer):
        raise TypeError(f"composer returned {type(answer).__name__}")
    return answer, usage
