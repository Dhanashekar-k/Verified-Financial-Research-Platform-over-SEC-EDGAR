"""Dense retrieval: pgvector HNSW over the live `chunks` view (M03 section 3.4)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg

from app.engine.retrieval.types import ChunkHit


def vector_literal(vector: Sequence[float]) -> str:
    """`[x1,...,xn]`, the text form pgvector parses into a `halfvec`."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def dense_search(
    conn: psycopg.Connection[Any],
    query_vector: Sequence[float],
    *,
    limit: int = 50,
    ef_search: int = 100,
    cik: int | None = None,
    fiscal_year: int | None = None,
    ciks: Sequence[int] | None = None,
    fiscal_years: Sequence[int] | None = None,
    max_scan_tuples: int | None = None,
) -> list[ChunkHit]:
    """Top `limit` chunks by cosine distance to `query_vector`, best first (score = 1 - distance).

    `hnsw.ef_search` and `hnsw.iterative_scan` are set with `SET LOCAL` inside a transaction, so
    they end with it (on an autocommit connection that is this call). Iterative scans keep a
    filtered query from returning fewer than `limit` rows when the index visits too many rows that
    the filter rejects. `ciks` / `fiscal_years` are "any of" filters (empty matches nothing).
    """
    where = []
    params: list[Any] = [vector_literal(query_vector)]
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
    params += [vector_literal(query_vector), limit]
    clause = f"WHERE {' AND '.join(where)} " if where else ""
    sql = (
        "SELECT chunk_id, 1 - (embedding <=> %s::halfvec) AS score FROM chunks "
        f"{clause}ORDER BY embedding <=> %s::halfvec LIMIT %s"
    )
    with conn.transaction():
        conn.execute(f"SET LOCAL hnsw.ef_search = {int(ef_search)}")
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        if max_scan_tuples is not None:
            # pgvector ends an iterative scan after this many tuples (default 20,000), which can
            # return fewer than `limit` rows for a very selective filter.
            conn.execute(f"SET LOCAL hnsw.max_scan_tuples = {int(max_scan_tuples)}")
        rows = conn.execute(sql, params).fetchall()
    # relaxed_order lets pgvector emit rows slightly out of order; sort best first before ranking.
    ordered = sorted(((float(r[1]), r[0]) for r in rows), key=lambda t: (-t[0], t[1]))
    return [
        ChunkHit(chunk_id=cid, rank=i, score=score)
        for i, (score, cid) in enumerate(ordered, start=1)
    ]
