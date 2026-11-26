"""Live evidence for one composed answer: the cited facts and the cited passages.

Facts are read from the serving ``facts`` view **now**, not taken from what the compiler returned,
so a check sees whether a cited fact exists. Only passages the composer was shown are evidence: a
claim citing any other chunk finds nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from app.engine.answer import Answer, ChunkProvenance, ExprProvenance
from app.engine.plan.schema import NumericResult
from app.engine.verify.models import ChunkRow, Evidence, FactRow

_FACT_COLUMNS = tuple(FactRow.model_fields)
_CHUNK_COLUMNS = tuple(ChunkRow.model_fields)
_SELECT_FACTS = f"SELECT {', '.join(_FACT_COLUMNS)} FROM facts WHERE fact_id = ANY(%s)"
_SELECT_CHUNKS = f"SELECT {', '.join(_CHUNK_COLUMNS)} FROM chunks WHERE chunk_id = ANY(%s)"


def wanted_ids(answer: Answer, results: Sequence[NumericResult]) -> tuple[set[int], set[str]]:
    """Fact ids a claim cites or the compiler built (with restated latest ids); cited chunk ids."""
    facts: set[int] = set()
    chunks: set[str] = set()
    for r in results:
        facts.update(r.fact_ids)
        facts.update(n.latest_fact_id for n in r.notes if n.latest_fact_id is not None)
    for claim in answer.claims:
        p = claim.provenance
        if isinstance(p, ExprProvenance):
            facts.update(p.fact_ids)
        elif isinstance(p, ChunkProvenance):
            chunks.add(p.chunk_id)
    return facts, chunks


def _select(conn: Any, sql: str, columns: Sequence[str], ids: Collection[Any]) -> list[Any]:
    if not ids:
        return []
    rows = conn.execute(sql, [sorted(ids)]).fetchall()
    return [dict(zip(columns, row, strict=True)) for row in rows]


def load_evidence(
    conn: Any,
    answer: Answer,
    results: Sequence[NumericResult],
    shown_chunk_ids: Collection[str],
) -> Evidence:
    """Parameterized reads. A derived fact's ``derived_from`` parents are loaded too; anything
    not found is simply absent, and the checks fail closed on it."""
    fact_ids, chunk_ids = wanted_ids(answer, results)
    facts: dict[int, FactRow] = {}
    todo = fact_ids
    while todo:
        found = [
            FactRow.model_validate(r) for r in _select(conn, _SELECT_FACTS, _FACT_COLUMNS, todo)
        ]
        facts.update({f.fact_id: f for f in found})
        todo = {p for f in found for p in f.derived_from or []} - facts.keys()
    chunks = [
        ChunkRow.model_validate(r)
        for r in _select(conn, _SELECT_CHUNKS, _CHUNK_COLUMNS, chunk_ids & set(shown_chunk_ids))
    ]
    return Evidence(facts=facts, chunks={c.chunk_id: c for c in chunks})
