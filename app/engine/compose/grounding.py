"""Deterministic grounding checks on a composed Answer (the slot M6's verifier fills).

Nothing here changes the answer: it reports which claims break the composer's rules, so a run
measures how often the composer strays (computes a number, quotes loosely) before the verifier
exists, and the verifier later has its work cut out in numbers rather than guesses. The checks:

- a numeric claim cites exactly one provided NumericResult (same ``expr`` and fact ids), carries
  its value unchanged and the result's own display string, and every number in its text is that
  value (in any spelling the display uses) or the period: a growth rate the model worked out and
  tucked into the wording is caught here, not only a wrong ``expr``;
- a text claim cites a provided chunk, its ``quote`` is a substring of that chunk (whitespace
  normalized), and every number in the claim text appears in the quote (rule 3).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.engine.answer import Answer, ChunkProvenance, ExprProvenance
from app.engine.compose.format import format_value
from app.engine.plan.schema import NumericResult

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class Violation:
    claim_id: str
    rule: str
    detail: str


def squash(text: str) -> str:
    """Whitespace-normalized text (the quote rule of 05 4.3)."""
    return " ".join(text.split())


def numbers_in(text: str) -> set[str]:
    """Number tokens of ``text`` with thousands separators removed and a trailing '.' dropped."""
    return {m.group(0).replace(",", "").rstrip(".") for m in _NUMBER.finditer(text)}


def _allowed_numbers(r: NumericResult) -> set[str]:
    """Numbers a claim about ``r`` may say: its value, its display, its fiscal year and quarter."""
    out = numbers_in(format_value(r.value, r.unit)) | numbers_in(str(r.value))
    out.add(str(r.fiscal_year))
    if r.operation == "growth":
        out.add(str(r.fiscal_year - 1))  # year over year; a named base year arrives via `years`
    if r.fiscal_period != "FY":
        out.add(r.fiscal_period[1:])
    for n in r.notes:
        if n.latest_value is not None and r.unit != "ratio":
            out |= numbers_in(format_value(n.latest_value, r.unit)) | numbers_in(
                str(n.latest_value)
            )
    return out


def check_grounding(
    answer: Answer,
    results: Sequence[NumericResult],
    passages: Mapping[str, str],
    years: Collection[int] = (),
) -> list[Violation]:
    by_prov = {(r.expr, tuple(sorted(r.fact_ids))): r for r in results}
    # a restated note's latest value is citable too: expr "f:<latest_fact_id>"
    latest: dict[tuple[str, tuple[int, ...]], Decimal] = {}
    for r in results:
        for n in r.notes:
            if n.latest_fact_id is not None and n.latest_value is not None and r.unit != "ratio":
                latest[(f"f:{n.latest_fact_id}", (n.latest_fact_id,))] = n.latest_value
    out: list[Violation] = []
    for c in answer.claims:
        p = c.provenance
        if c.kind in ("numeric", "numeric_derived"):
            if not isinstance(p, ExprProvenance):
                out.append(Violation(c.id, "numeric_needs_expr", f"cites a {p.type}"))
                continue
            key = (p.expr, tuple(sorted(p.fact_ids)))
            if key in by_prov:
                r = by_prov[key]
                if c.value is None or c.value != r.value:
                    out.append(Violation(c.id, "value_changed", f"{c.value} != {r.value}"))
                    continue
                shown = format_value(r.value, r.unit)
                if c.display is not None and c.display != shown:
                    out.append(Violation(c.id, "display_changed", f"{c.display!r} != {shown!r}"))
                stray = numbers_in(c.text) - _allowed_numbers(r) - {str(y) for y in years}
                if stray:
                    out.append(Violation(c.id, "number_not_in_result", ",".join(sorted(stray))))
            elif key in latest:
                if c.value is None or c.value != latest[key]:
                    out.append(Violation(c.id, "value_changed", f"{c.value} != {latest[key]}"))
            else:
                out.append(Violation(c.id, "not_a_provided_result", p.expr))
        else:
            if not isinstance(p, ChunkProvenance):
                out.append(Violation(c.id, "text_needs_chunk", f"cites a {p.type}"))
                continue
            text = passages.get(p.chunk_id)
            if text is None:
                out.append(Violation(c.id, "unknown_chunk", p.chunk_id))
                continue
            quote = squash(p.quote)
            if not quote or quote not in squash(text):
                out.append(Violation(c.id, "quote_not_in_chunk", p.quote[:80]))
                continue
            stray = numbers_in(c.text) - numbers_in(p.quote)
            if stray:
                out.append(Violation(c.id, "number_not_in_quote", ",".join(sorted(stray))))
    return out


_MARKERS = re.compile(r"\[\d+\]|\b10-[KQ]\b|\bItem \d+[A-C]?\b|\bQ[1-4]\b", re.IGNORECASE)


def check_markdown(
    answer: Answer,
    results: Sequence[NumericResult],
    passages: Mapping[str, str],
    years: Collection[int] = (),
) -> list[Violation]:
    """Numbers in ``answer_markdown`` that no claim, result or cited quote supports (citation
    markers, "10-K", "Item 7" and "Q2" are ignored). A diagnostic, like the claim checks: the
    prose is where a model most easily slips in a figure of its own."""
    allowed: set[str] = {str(y) for y in years}  # the periods the plan names
    for r in results:
        allowed |= _allowed_numbers(r)
    for c in answer.claims:
        allowed |= numbers_in(c.text) | numbers_in(c.display or "")
        if isinstance(c.provenance, ChunkProvenance):
            allowed |= numbers_in(c.provenance.quote)
    stray = numbers_in(_MARKERS.sub(" ", answer.answer_markdown)) - allowed
    return (
        [Violation("answer_markdown", "number_not_supported", ",".join(sorted(stray)))]
        if stray
        else []
    )
