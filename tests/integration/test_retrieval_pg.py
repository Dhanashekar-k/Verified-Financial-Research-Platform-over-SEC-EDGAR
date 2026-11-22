"""Tests for ``app/engine/retrieval`` (lexical, dense, hybrid) against a real Postgres + pgvector.

The tables are populated the production way: a tiny Parquet export is loaded with
``serving.load.load_snapshot.load_version`` and made live with ``swap``, so the primitives query the
real ``chunks`` VIEW and the real HNSW / GIN indexes. One container per module, one database per
dataset (module scope: the retrieval tests only read).

Dense expectations are computed in numpy from the SAME vectors the export holds, rounded to half
precision first (the column is ``halfvec``), so the brute-force ordering is the exact ordering
Postgres computes up to float noise (tests allow 1e-4 on scores).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import numpy as np
import psycopg
import pytest

from app.engine.retrieval import ChunkHit, dense_search, hybrid_search, lexical_search, rrf
from pipelines.common.embedding import DIM, l2_normalize
from serving.load.load_snapshot import load_version, swap
from tests.support.serving_export import embedding_matrix, rare_word, read_table, write_export

IMAGE = "pgvector/pgvector:pg17"


@dataclass(frozen=True)
class Dataset:
    dsn: str
    ids: list[str]
    texts: list[str]
    ciks: list[int]
    years: list[int]
    vectors: np.ndarray  # (n, 384) float32, as exported

    def index_of(self, chunk_id: str) -> int:
        return self.ids.index(chunk_id)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    """One pgvector/pg17 container for the module."""
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # older testcontainers
        from testcontainers.postgres import PostgresContainer  # type: ignore[no-redef]
    with PostgresContainer(IMAGE, driver=None) as pg:
        yield pg.get_connection_url()


def _build(pg_url: str, root: Path, **export_kw: Any) -> Dataset:
    """Create a database, write + load + swap an export (version 1), return what was exported."""
    name = f"t_{uuid4().hex[:12]}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = urlsplit(pg_url)._replace(path=f"/{name}").geturl()
    write_export(root, 1, **export_kw)
    load_version(dsn, version=1, source=str(root))
    swap(dsn, 1)
    table = read_table(root, 1, "chunks").sort_by("chunk_id")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("ANALYZE")
    return Dataset(
        dsn=dsn,
        ids=table["chunk_id"].to_pylist(),
        texts=table["text"].to_pylist(),
        ciks=table["cik"].to_pylist(),
        years=table["fiscal_year"].to_pylist(),
        vectors=embedding_matrix(table),
    )


SMALL_TEXTS = {
    20: "Dividend dividend dividend dividend policy of the board. Marker mmmm.",
    21: (
        "A single dividend is mentioned once in a much longer passage about unrelated operating "
        "matters, logistics, facilities, leases, depreciation schedules and similar topics that "
        "dilute the match considerably across many more words."
    ),
    30: "The covenant breach was waived by the lenders.",
    32: "Covenant compliance certificates are delivered quarterly.",
}


@pytest.fixture(scope="module")
def small(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    """60 chunks, 3 companies (cik 1000..1002), fiscal years 2020..2023, a few crafted texts."""
    root = tmp_path_factory.mktemp("small_export")
    return _build(pg_url, root, n_chunks=60, n_facts=30, n_companies=3, texts=SMALL_TEXTS)


@pytest.fixture(scope="module")
def big(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    """600 chunks; the 60 with fiscal_year 2019 sit FAR from the query direction.

    Others are 8u + noise, the 2019 group is -8u + noise for a unit vector u, so for query u the
    nearest 100 rows are all non-2019 and a plain (non-iterative) HNSW scan with a fiscal_year
    filter finds nothing.
    """
    root = tmp_path_factory.mktemp("big_export")
    rng = np.random.default_rng(7)
    u = np.zeros(DIM)
    u[0] = 1.0
    years = [2019 if i % 10 == 0 else 2020 + i % 4 for i in range(600)]
    sign = np.array([-1.0 if y == 2019 else 1.0 for y in years])[:, None]
    vectors = l2_normalize(sign * 8.0 * u + rng.normal(size=(600, DIM)))
    return _build(
        pg_url,
        root,
        n_chunks=600,
        n_facts=20,
        n_companies=5,
        chunk_fiscal_years=years,
        embeddings=vectors,
    )


@pytest.fixture
def conn(small: Dataset) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(small.dsn, autocommit=True) as c:
        yield c


# --------------------------------------------------------------------------- helpers


def half(x: np.ndarray) -> np.ndarray:
    """float32 -> float16 -> float64: what a halfvec column holds."""
    return np.asarray(x, dtype=np.float32).astype(np.float16).astype(np.float64)


def brute_force(
    ds: Dataset, query: np.ndarray, mask: np.ndarray | None = None
) -> list[tuple[str, float]]:
    """Exact cosine ranking (best first, ties by id) of the half-rounded vectors."""
    mat = half(ds.vectors)
    q = half(query)
    sims = (mat @ q) / (np.linalg.norm(mat, axis=1) * np.linalg.norm(q))
    order = sorted(
        (i for i in range(len(ds.ids)) if mask is None or mask[i]),
        key=lambda i: (-sims[i], ds.ids[i]),
    )
    return [(ds.ids[i], float(sims[i])) for i in order]


def _pin_planner_to_ordered_index_scan(c: psycopg.Connection[Any]) -> None:
    """Session settings that leave only an ordered index scan (the HNSW index) for ORDER BY <=>.

    No seq scan, no bitmap scan (the (cik, fiscal_year) btree could answer the filter) and no
    explicit sort (which a btree or heap plan would need to order by distance).
    """
    c.execute("SET enable_seqscan = off")
    c.execute("SET enable_bitmapscan = off")
    c.execute("SET enable_sort = off")


def query_vector(seed: int) -> np.ndarray:
    return l2_normalize(np.random.default_rng(seed).normal(size=(1, DIM)))[0]


def assert_well_formed(hits: list[ChunkHit], *, limit: int) -> None:
    assert len(hits) <= limit
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True), scores
    assert len({h.chunk_id for h in hits}) == len(hits)
    assert all(isinstance(h, ChunkHit) and isinstance(h.chunk_id, str) for h in hits)
    assert all(isinstance(h.score, float) for h in hits)


# --------------------------------------------------------------------------- lexical


def test_lexical_rare_word_finds_its_chunk_ranked_first(
    small: Dataset, conn: psycopg.Connection[Any]
) -> None:
    """A word that occurs in exactly one chunk returns exactly that chunk, rank 1."""
    hits = lexical_search(conn, rare_word(7))
    assert [h.chunk_id for h in hits] == [small.ids[7]]
    assert hits[0].rank == 1
    assert hits[0].score > 0


def test_lexical_results_are_well_formed_and_exist_in_the_table(
    small: Dataset, conn: psycopg.Connection[Any]
) -> None:
    """A common word matches many chunks: ranks 1..n, scores non-increasing, limit honoured."""
    hits = lexical_search(conn, "revenue", limit=15)
    assert len(hits) == 15
    assert_well_formed(hits, limit=15)
    assert {h.chunk_id for h in hits} <= set(small.ids)
    assert len(lexical_search(conn, "revenue", limit=3)) == 3
    # default limit is 50, more than the 60-row table's matches trimmed to 50
    assert len(lexical_search(conn, "revenue")) == 50


def test_lexical_ranks_denser_matches_higher(small: Dataset, conn: psycopg.Connection[Any]) -> None:
    """ts_rank_cd: a chunk repeating the term outranks one mentioning it once in a long text."""
    hits = lexical_search(conn, "dividend")
    assert [h.chunk_id for h in hits] == [small.ids[20], small.ids[21]]
    assert hits[0].score > hits[1].score


def test_lexical_uses_websearch_syntax(small: Dataset, conn: psycopg.Connection[Any]) -> None:
    """websearch_to_tsquery syntax: quoted phrase, leading minus and plain words."""
    phrase = {h.chunk_id for h in lexical_search(conn, '"covenant breach"')}
    assert phrase == {small.ids[30]}
    both = {h.chunk_id for h in lexical_search(conn, "covenant")}
    assert {small.ids[30], small.ids[32]} <= both
    minus = {h.chunk_id for h in lexical_search(conn, "covenant -breach")}
    assert small.ids[32] in minus and small.ids[30] not in minus


def test_lexical_filters_restrict_results(small: Dataset, conn: psycopg.Connection[Any]) -> None:
    """cik, fiscal_year and both together restrict to exactly the matching chunks."""
    has_revenue = [i for i, t in enumerate(small.texts) if "revenue" in t.lower()]
    assert len(has_revenue) > 30

    by_cik = lexical_search(conn, "revenue", limit=100, cik=1001)
    want = {small.ids[i] for i in has_revenue if small.ciks[i] == 1001}
    assert {h.chunk_id for h in by_cik} == want and want

    by_year = lexical_search(conn, "revenue", limit=100, fiscal_year=2022)
    want = {small.ids[i] for i in has_revenue if small.years[i] == 2022}
    assert {h.chunk_id for h in by_year} == want and want

    both = lexical_search(conn, "revenue", limit=100, cik=1001, fiscal_year=2022)
    want = {small.ids[i] for i in has_revenue if small.ciks[i] == 1001 and small.years[i] == 2022}
    assert {h.chunk_id for h in both} == want
    assert lexical_search(conn, "revenue", cik=424242) == []
    # filters apply before `limit`: a rare word of another company's chunk is not returned
    other = next(i for i in range(60) if small.ciks[i] != 1000)
    assert lexical_search(conn, rare_word(other), cik=1000) == []


def test_lexical_empty_and_stopword_queries_return_nothing(conn: psycopg.Connection[Any]) -> None:
    """A query with no searchable terms (stop words, blank, punctuation) returns []."""
    for q in ("the of and", "", "   ", "!!! ???", '"'):
        assert lexical_search(conn, q) == [], q


def test_lexical_question_is_a_bound_parameter_not_sql(
    small: Dataset, conn: psycopg.Connection[Any]
) -> None:
    """Quotes and SQL metacharacters in the question are data: no error, the table is untouched."""
    for q in ("'; DROP TABLE chunks_v1; --", 'revenue" OR 1=1 --', "a\\b%_ \x7f"):
        lexical_search(conn, q)
    assert conn.execute("SELECT count(*) FROM chunks").fetchone() == (len(small.ids),)


def test_lexical_handles_non_ascii(small: Dataset, conn: psycopg.Connection[Any]) -> None:
    """A non-ASCII query word matches the chunk whose text contains it (chunk 0 holds 'café')."""
    hits = lexical_search(conn, "café")
    assert [h.chunk_id for h in hits] == [small.ids[0]]


# --------------------------------------------------------------------------- dense


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_dense_top5_equals_brute_force_cosine(
    small: Dataset, conn: psycopg.Connection[Any], seed: int
) -> None:
    """HNSW top-5 equals the exact numpy cosine ordering; score = 1 - cosine distance."""
    q = query_vector(seed)
    hits = dense_search(conn, q.tolist(), limit=5)
    want = brute_force(small, q)[:5]
    assert [h.chunk_id for h in hits] == [cid for cid, _ in want]
    for hit, (_, sim) in zip(hits, want, strict=True):
        assert hit.score == pytest.approx(sim, abs=1e-4)
    assert_well_formed(hits, limit=5)


def test_dense_accepts_any_float_sequence_and_a_chunks_own_vector_is_its_nearest(
    small: Dataset, conn: psycopg.Connection[Any]
) -> None:
    """list and tuple inputs agree; a chunk's own vector retrieves that chunk at score ~1."""
    i = 13
    vec = small.vectors[i]
    from_list = dense_search(conn, vec.tolist(), limit=3)
    from_tuple = dense_search(conn, tuple(float(x) for x in vec), limit=3)
    assert from_list == from_tuple
    assert from_list[0].chunk_id == small.ids[i]
    assert from_list[0].score == pytest.approx(1.0, abs=1e-3)


def test_dense_default_limit_and_limit_larger_than_table(
    small: Dataset, conn: psycopg.Connection[Any]
) -> None:
    """Default limit 50 returns 50 of 60 rows; a limit above the row count returns all once."""
    q = query_vector(4)
    assert len(dense_search(conn, q.tolist())) == 50
    everything = dense_search(conn, q.tolist(), limit=500)
    assert sorted(h.chunk_id for h in everything) == sorted(small.ids)


def test_dense_filters_restrict_and_match_filtered_brute_force(
    small: Dataset, conn: psycopg.Connection[Any]
) -> None:
    """cik / fiscal_year / both filter in SQL; filtered top-5 equals the filtered exact ranking."""
    q = query_vector(5)
    ciks = np.array(small.ciks)
    years = np.array(small.years)
    cases: list[tuple[dict[str, int], np.ndarray]] = [
        ({"cik": 1002}, ciks == 1002),
        ({"fiscal_year": 2021}, years == 2021),
        ({"cik": 1000, "fiscal_year": 2020}, (ciks == 1000) & (years == 2020)),
    ]
    for kwargs, mask in cases:
        hits = dense_search(conn, q.tolist(), limit=5, **kwargs)
        want = brute_force(small, q, mask)[:5]
        assert [h.chunk_id for h in hits] == [cid for cid, _ in want], kwargs
        assert all(mask[small.index_of(h.chunk_id)] for h in hits)
    assert dense_search(conn, q.tolist(), cik=424242) == []
    # every row of a filter group is returned when limit exceeds its size
    group = dense_search(conn, q.tolist(), limit=50, cik=1001)
    assert sorted(h.chunk_id for h in group) == sorted(
        small.ids[i] for i in range(60) if small.ciks[i] == 1001
    )


def test_dense_filter_matching_few_rows_still_returns_limit_rows(big: Dataset) -> None:
    """Iterative scan: 60 of 600 rows match, none near the query; limit=10 still yields 10 rows.

    Precondition (asserted): with iterative_scan off, the SAME query on the HNSW index returns
    fewer than 10 rows, so the 10 returned by dense_search can only come from hnsw.iterative_scan.
    The planner is pinned to the HNSW index with enable_seqscan=off on the session.
    """
    q = np.zeros(DIM)
    q[0] = 1.0
    qlit = "[" + ",".join(repr(float(x)) for x in q) + "]"
    sql = (
        "SELECT chunk_id FROM chunks WHERE fiscal_year = 2019 "
        "ORDER BY embedding <=> %s::halfvec LIMIT 10"
    )
    with psycopg.connect(big.dsn, autocommit=True) as c:
        _pin_planner_to_ordered_index_scan(c)
        hnsw_index = c.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
            "WHERE am.amname = 'hnsw'"
        ).fetchone()
        assert hnsw_index is not None
        plan = "\n".join(r[0] for r in c.execute("EXPLAIN " + sql, (qlit,)).fetchall())
        assert hnsw_index[0] in plan, f"precondition: planner must use the HNSW index\n{plan}"
        with c.transaction():
            c.execute("SET LOCAL hnsw.iterative_scan = off")
            c.execute("SET LOCAL hnsw.ef_search = 100")
            plain = c.execute(sql, (qlit,)).fetchall()
        assert len(plain) < 10, f"precondition: plain HNSW scan returned {len(plain)} rows"

        hits = dense_search(c, q.tolist(), limit=10, fiscal_year=2019)

    assert len(hits) == 10
    assert [h.rank for h in hits] == list(range(1, 11))
    assert len({h.chunk_id for h in hits}) == 10
    assert all(big.years[big.index_of(h.chunk_id)] == 2019 for h in hits)
    with psycopg.connect(big.dsn, autocommit=True) as c2:
        _pin_planner_to_ordered_index_scan(c2)
        every = dense_search(c2, q.tolist(), limit=100, fiscal_year=2019)
    assert len(every) == 60


def test_dense_iterative_scan_results_are_still_sorted_best_first(big: Dataset) -> None:
    """With hnsw.iterative_scan = relaxed_order the index may emit rows slightly out of order.

    pgvector documents that: relaxed_order trades exact distance order for completeness. The
    primitive's contract is 'best first' (rank = position, and RRF consumes the rank), so the
    returned hits must be re-sorted by score (SQL subquery or Python) with ranks assigned after.
    The same query is run for several limits and filters; scores must never increase.
    """
    q = np.zeros(DIM)
    q[0] = 1.0
    with psycopg.connect(big.dsn, autocommit=True) as c:
        _pin_planner_to_ordered_index_scan(c)
        for limit in (10, 30, 60):
            hits = dense_search(c, q.tolist(), limit=limit, fiscal_year=2019)
            assert len(hits) == limit
            assert_well_formed(hits, limit=limit)
        for limit in (10, 40):
            assert_well_formed(dense_search(c, q.tolist(), limit=limit), limit=limit)


@pytest.mark.parametrize("autocommit", [True, False])
def test_dense_set_local_does_not_leak_into_the_session(small: Dataset, autocommit: bool) -> None:
    """After dense_search, hnsw.ef_search / hnsw.iterative_scan are back to their old values."""
    with psycopg.connect(small.dsn, autocommit=autocommit) as c:
        # The hnsw.* settings exist only once pgvector's library is loaded in the session.
        c.execute("SELECT '[1,2]'::halfvec")
        before = (
            c.execute("SHOW hnsw.ef_search").fetchone(),
            c.execute("SHOW hnsw.iterative_scan").fetchone(),
        )
        c.commit()
        dense_search(c, query_vector(6).tolist(), limit=5, ef_search=77)
        after = (
            c.execute("SHOW hnsw.ef_search").fetchone(),
            c.execute("SHOW hnsw.iterative_scan").fetchone(),
        )
    assert before == (("40",), ("off",))
    assert after == before


def test_dense_connection_stays_usable_after_a_search(
    small: Dataset, conn: psycopg.Connection[Any]
) -> None:
    """The search leaves the connection idle (no open or aborted transaction)."""
    dense_search(conn, query_vector(8).tolist(), limit=3)
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert conn.execute("SELECT 1").fetchone() == (1,)


# --------------------------------------------------------------------------- hybrid


def test_hybrid_equals_rrf_of_the_two_lists(small: Dataset, conn: psycopg.Connection[Any]) -> None:
    """hybrid_search = RRF over lexical(per_list) and dense(per_list); score is the fused score."""
    q = query_vector(9)
    question = "revenue risk factor on liquidity"
    lex = lexical_search(conn, question, limit=20)
    den = dense_search(conn, q.tolist(), limit=20)
    fused = rrf([[h.chunk_id for h in lex], [h.chunk_id for h in den]])

    hits = hybrid_search(conn, question, q.tolist(), limit=10, per_list=20)

    assert [h.chunk_id for h in hits] == [cid for cid, _ in fused[:10]]
    for hit, (_, score) in zip(hits, fused[:10], strict=True):
        assert hit.score == pytest.approx(score)
    assert_well_formed(hits, limit=10)
    assert {h.chunk_id for h in hits} <= set(small.ids)


def test_hybrid_ranks_a_chunk_found_by_both_methods_first(
    small: Dataset, conn: psycopg.Connection[Any]
) -> None:
    """A chunk that is rank 1 lexically AND densely beats everything and scores 2/(60+1)."""
    i = 7
    hits = hybrid_search(conn, rare_word(i), small.vectors[i].tolist(), limit=5)
    assert hits[0].chunk_id == small.ids[i]
    assert hits[0].score == pytest.approx(2 / 61)
    assert hits[0].rank == 1
    assert hits[1].score < hits[0].score


def test_hybrid_respects_limit_and_per_list(small: Dataset, conn: psycopg.Connection[Any]) -> None:
    """limit caps the output; per_list caps each source (union <= 2 * per_list ids)."""
    q = query_vector(10).tolist()
    assert len(hybrid_search(conn, "revenue", q, limit=3)) == 3
    default = hybrid_search(conn, "revenue", q)
    assert len(default) <= 10
    assert_well_formed(default, limit=10)

    small_lists = hybrid_search(conn, "revenue", q, limit=50, per_list=3)
    lex = lexical_search(conn, "revenue", limit=3)
    den = dense_search(conn, q, limit=3)
    union = {h.chunk_id for h in lex} | {h.chunk_id for h in den}
    assert {h.chunk_id for h in small_lists} == union
    assert len(small_lists) <= 6


def test_hybrid_passes_filters_to_both_retrievers(
    small: Dataset, conn: psycopg.Connection[Any]
) -> None:
    """cik and fiscal_year restrict BOTH sources: every fused hit satisfies the filters."""
    q = query_vector(11).tolist()
    hits = hybrid_search(conn, "revenue risk", q, limit=30, per_list=30, cik=1002, fiscal_year=2022)
    assert hits
    for h in hits:
        i = small.index_of(h.chunk_id)
        assert small.ciks[i] == 1002 and small.years[i] == 2022


def test_hybrid_with_no_lexical_match_falls_back_to_dense(
    small: Dataset, conn: psycopg.Connection[Any]
) -> None:
    """A question with no searchable terms contributes nothing; the dense list alone is returned."""
    q = query_vector(12)
    hits = hybrid_search(conn, "the of and", q.tolist(), limit=5)
    assert [h.chunk_id for h in hits] == [cid for cid, _ in brute_force(small, q)[:5]]
    assert hits[0].score == pytest.approx(1 / 61)
