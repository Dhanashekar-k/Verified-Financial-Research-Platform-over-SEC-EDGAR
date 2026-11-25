"""Lexical retrieval: Postgres full-text search over the live `chunks` view (M03 section 3.4).

Called "lexical", not BM25: `ts_rank_cd` ranks by cover density, not by BM25.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg

from app.engine.retrieval.types import ChunkHit


def lexical_search(
    conn: psycopg.Connection[Any],
    question: str,
    *,
    limit: int = 50,
    cik: int | None = None,
    fiscal_year: int | None = None,
    ciks: Sequence[int] | None = None,
    fiscal_years: Sequence[int] | None = None,
) -> list[ChunkHit]:
    """Top `limit` chunks matching `websearch_to_tsquery('english', question)`, best first.

    The filters are optional; they are applied in SQL, so `limit` rows come back whenever that
    many chunks match. `ciks` / `fiscal_years` are "any of" filters (an empty sequence matches
    nothing); they combine with `cik` / `fiscal_year` by AND.
    """
    where = ["tsv @@ q"]
    params: list[Any] = [question]
    if cik is not None:
        where.append("cik = %s")
        params.append(cik)
    if fiscal_year is not None:
        where.append("fiscal_year = %s")
        params.append(fiscal_year)
    if ciks is not None:
        where.append("cik = ANY(%s)")
        params.append(list(ciks))
    if fiscal_years is not None:
        where.append("fiscal_year = ANY(%s)")
        params.append(list(fiscal_years))
    params.append(limit)
    sql = (
        "SELECT chunk_id, ts_rank_cd(tsv, q) AS score "
        "FROM chunks, websearch_to_tsquery('english', %s) AS q "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY score DESC, chunk_id LIMIT %s"
    )
    rows = conn.execute(sql, params).fetchall()
    return [ChunkHit(chunk_id=r[0], rank=i, score=float(r[1])) for i, r in enumerate(rows, start=1)]
