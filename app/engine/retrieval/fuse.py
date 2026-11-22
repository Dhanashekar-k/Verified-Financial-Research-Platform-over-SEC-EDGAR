"""Reciprocal-rank fusion of the lexical and dense lists (M03 section 3.4)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg

from app.engine.retrieval.dense import dense_search
from app.engine.retrieval.lexical import lexical_search
from app.engine.retrieval.types import ChunkHit

RRF_K = 60


def rrf(rank_lists: Sequence[Sequence[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """`score(id) = sum over lists of 1 / (k + rank)`, best first, ties broken by id.

    Each list is best-first with ranks from 1; an id repeated inside one list counts once, at its
    best (first) rank.
    """
    scores: dict[str, float] = {}
    for ranked in rank_lists:
        seen: set[str] = set()
        rank = 0
        for chunk_id in ranked:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            rank += 1
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def hybrid_search(
    conn: psycopg.Connection[Any],
    question: str,
    query_vector: Sequence[float],
    *,
    limit: int = 10,
    per_list: int = 50,
    ef_search: int = 100,
    cik: int | None = None,
    fiscal_year: int | None = None,
) -> list[ChunkHit]:
    """Lexical top `per_list` + dense top `per_list`, fused with RRF, best `limit` returned."""
    lexical = lexical_search(conn, question, limit=per_list, cik=cik, fiscal_year=fiscal_year)
    dense = dense_search(
        conn, query_vector, limit=per_list, ef_search=ef_search, cik=cik, fiscal_year=fiscal_year
    )
    fused = rrf([[h.chunk_id for h in lexical], [h.chunk_id for h in dense]])
    return [
        ChunkHit(chunk_id=cid, rank=i, score=score)
        for i, (cid, score) in enumerate(fused[:limit], start=1)
    ]
