"""What the support judge sees as a claim's evidence (``evidence-v1``, ADR-049).

A text claim: the whole cited passage with whose filing it is. This is looser than a check of
the quote alone, since the rest of the passage may back the claim. A numeric claim: each cited
fact with its company, concept, unit, period and filing, and the expression over them. The
expression is **not** evaluated here: the judge does its own arithmetic, and re-executing it
deterministically is the verifier's job.

This module is independent of ``app/engine/verify`` and was fixed before the first verdict was
seen. Changing the text it renders changes the judge's input: bump ``EVIDENCE_VERSION``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from eval.verifier_trace import ChunkRecord, FactRecord

EVIDENCE_VERSION = "evidence-v1"
UNKNOWN = "unknown"


def _plain(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _company(cik: int | None, names: Mapping[int, str]) -> str:
    if cik is None:
        return UNKNOWN
    return f"{names.get(cik, UNKNOWN)} (CIK {cik})"


def chunk_evidence(chunk: ChunkRecord, names: Mapping[int, str]) -> str:
    year = UNKNOWN if chunk.fiscal_year is None else str(chunk.fiscal_year)
    if chunk.fiscal_period:
        year = f"{year} ({chunk.fiscal_period})"
    section = " ".join(s for s in (chunk.section, chunk.section_title) if s) or UNKNOWN
    return "\n".join(
        [
            "A passage from an SEC filing.",
            f"Company: {_company(chunk.cik, names)}",
            f"Form: {chunk.form or UNKNOWN}",
            f"Fiscal year of the filing: {year}",
            f"Section: {section}",
            "Passage:",
            chunk.text,
        ]
    )


def _period(f: FactRecord) -> str:
    if f.qtrs == 0:
        span = f"a balance as of {f.period_end}"
    elif f.period_start is not None:
        span = f"{f.period_start} to {f.period_end}, {f.qtrs} quarter(s) long"
    else:
        span = f"ending {f.period_end}, {f.qtrs} quarter(s) long"
    return f"fiscal year {f.fiscal_year}, fiscal period {f.fiscal_period}; {span}"


def _version(f: FactRecord) -> str:
    if f.is_original:
        return "as originally filed" + ("" if f.is_latest else " (later restated)")
    return "the latest restated value" if f.is_latest else "an intermediate restatement"


def _fact_lines(f: FactRecord, names: Mapping[int, str]) -> list[str]:
    return [
        f"Fact f:{f.fact_id}",
        f"  Company: {_company(f.cik, names)}",
        f"  Concept (XBRL tag): {f.tag}",
        f"  Value: {_plain(f.value)}",
        f"  Unit: {f.uom}",
        f"  Period: {_period(f)}",
        f"  Source: {f.form} filed {f.filed}; {_version(f)}",
    ]


def expr_evidence(
    expr: str,
    fact_ids: Sequence[int],
    facts: Mapping[int, FactRecord],
    names: Mapping[int, str],
) -> str:
    lines = [
        "XBRL facts reported in SEC filings.",
        "The claim cites this expression over them, where f:<id> stands for that fact's value:",
        f"  {expr}",
    ]
    for fact_id in dict.fromkeys(fact_ids):
        lines.append("")
        fact = facts.get(fact_id)
        if fact is None:
            lines.append(f"Fact f:{fact_id}: no such fact")
            continue
        lines += _fact_lines(fact, names)
        if fact.derived_from:
            lines.append(
                "  This value was not filed as such. It was derived as the first parent fact "
                "below minus the second."
            )
            for parent_id in fact.derived_from:
                parent = facts.get(parent_id)
                shown = (
                    _fact_lines(parent, names) if parent else [f"Fact f:{parent_id}: no such fact"]
                )
                lines += [f"    {line}" for line in shown]
    return "\n".join(lines)
