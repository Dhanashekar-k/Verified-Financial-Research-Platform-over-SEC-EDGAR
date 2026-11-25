"""retrieve() and the multi-value filters against a real Postgres + pgvector (Testcontainers).

The data is the tiny serving export the retrieval tests use (60 chunks, 3 companies cik 1000..1002,
fiscal years 2020..2023, a few crafted texts). The embedder and reranker are test doubles (they are
collaborators; the SQL, the fusion, the filters and the boost are real).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pytest

from app.engine.plan.schema import CompanyRef, PeriodRef, QueryPlan
from app.engine.retrieval import dense_search, lexical_search
from app.engine.retrieval.query import DEFAULT_SECTION_BONUS, RetrievalConfig, retrieve
from tests.integration.test_retrieval_pg import IMAGE, SMALL_TEXTS, Dataset, _build


@pytest.fixture(scope="module")
def pg() -> Iterator[str]:
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # older testcontainers
        from testcontainers.postgres import PostgresContainer  # type: ignore[no-redef]
    with PostgresContainer(IMAGE, driver=None) as c:
        yield c.get_connection_url()


@pytest.fixture(scope="module")
def ds(pg: str, tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    root: Path = tmp_path_factory.mktemp("engine_retrieval")
    return _build(pg, root, n_chunks=60, n_facts=30, n_companies=3, texts=SMALL_TEXTS)


@pytest.fixture
def conn(ds: Dataset) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(ds.dsn, autocommit=True) as c:
        yield c


class FakeEmbedder:
    """Query text -> a stored chunk's own vector (so that chunk is the nearest neighbour)."""

    def __init__(self, ds: Dataset, target: int) -> None:
        self.vec = [float(x) for x in ds.vectors[target]]
        self.calls: list[list[str]] = []

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vec for _ in texts]


class KeywordReranker:
    name = "keyword"

    def __init__(self, word: str) -> None:
        self.word = word

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        return [float(p.lower().count(self.word)) for p in passages]


def plan(**kw: Any) -> QueryPlan:
    base: dict[str, Any] = {"intent": "narrative"}
    base.update(kw)
    return QueryPlan(**base)


# --- the multi-value filters on the primitives -------------------------------------------------


def test_lexical_and_dense_accept_any_of_filters(
    conn: psycopg.Connection[Any], ds: Dataset
) -> None:
    hits = lexical_search(conn, "dividend or covenant", limit=50, ciks=[1000, 1001])
    assert hits and {ds.ciks[ds.index_of(h.chunk_id)] for h in hits} <= {1000, 1001}
    hits = lexical_search(conn, "dividend or covenant or the", limit=60, fiscal_years=[2021, 2022])
    assert {ds.years[ds.index_of(h.chunk_id)] for h in hits} <= {2021, 2022}
    dh = dense_search(conn, ds.vectors[0], limit=60, ciks=[1002], fiscal_years=[2020, 2023])
    assert dh and all(ds.ciks[ds.index_of(h.chunk_id)] == 1002 for h in dh)
    assert all(ds.years[ds.index_of(h.chunk_id)] in (2020, 2023) for h in dh)


def test_an_empty_any_of_filter_matches_nothing(conn: psycopg.Connection[Any], ds: Dataset) -> None:
    assert lexical_search(conn, "dividend", ciks=[]) == []
    assert dense_search(conn, ds.vectors[0], ciks=[]) == []


def test_filters_combine_with_the_single_value_ones_by_and(
    conn: psycopg.Connection[Any], ds: Dataset
) -> None:
    hits = dense_search(conn, ds.vectors[0], limit=60, cik=1000, ciks=[1001, 1002])
    assert hits == []


# --- retrieve ----------------------------------------------------------------------------------


def test_filters_restrict_results_to_the_planned_company_and_year(
    conn: psycopg.Connection[Any], ds: Dataset
) -> None:
    p = plan(
        companies=[CompanyRef(mention="Co", cik=1001)],
        periods=[PeriodRef(fiscal_year=2021)],
    )
    res = retrieve(
        conn,
        p,
        "dividend policy covenant",
        embedder=FakeEmbedder(ds, 5),
        reranker=KeywordReranker("dividend"),
    )
    assert res.chunks and res.filters == {"ciks": [1001], "fiscal_years": [2021]}
    for c in res.chunks:
        i = ds.index_of(c.chunk_id)
        assert (ds.ciks[i], ds.years[i]) == (1001, 2021)
    assert [c.rank for c in res.chunks] == list(range(1, len(res.chunks) + 1))
    assert not res.relaxed_year_filter


def test_without_filters_other_companies_come_back(
    conn: psycopg.Connection[Any], ds: Dataset
) -> None:
    p = plan(companies=[CompanyRef(mention="Co", cik=1001)], periods=[PeriodRef(fiscal_year=2021)])
    cfg = RetrievalConfig(filters=False, rerank=False, top_k=40)
    res = retrieve(
        conn, p, "dividend covenant", embedder=FakeEmbedder(ds, 5), reranker=None, cfg=cfg
    )
    assert {ds.ciks[ds.index_of(c)] for c in res.chunk_ids} > {1001}


def test_an_empty_year_filter_is_relaxed_once_but_the_company_never_is(
    conn: psycopg.Connection[Any], ds: Dataset
) -> None:
    p = plan(companies=[CompanyRef(mention="Co", cik=1001)], periods=[PeriodRef(fiscal_year=1999)])
    res = retrieve(
        conn, p, "dividend covenant", embedder=FakeEmbedder(ds, 5), reranker=KeywordReranker("x")
    )
    assert res.relaxed_year_filter and res.filters == {"ciks": [1001], "fiscal_years": []}
    assert res.chunks and {ds.ciks[ds.index_of(c)] for c in res.chunk_ids} == {1001}
    none = retrieve(
        conn,
        plan(
            companies=[CompanyRef(mention="Ghost", cik=424242)],
            periods=[PeriodRef(fiscal_year=2021)],
        ),
        "dividend covenant",
        embedder=FakeEmbedder(ds, 5),
        reranker=KeywordReranker("x"),
    )
    assert none.chunks == [] and none.n_candidates == 0, "never fall back to another company"


def test_rerank_decides_the_final_order_and_top_k(
    conn: psycopg.Connection[Any], ds: Dataset
) -> None:
    p = plan()
    cfg = RetrievalConfig(top_k=3, filters=False)
    res = retrieve(
        conn,
        p,
        "dividend covenant",
        embedder=FakeEmbedder(ds, 5),
        reranker=KeywordReranker("dividend"),
        cfg=cfg,
    )
    assert len(res.chunks) == 3
    assert (
        res.chunks[0].chunk_id == ds.ids[20] and res.chunks[0].text.lower().count("dividend") == 4
    )
    assert [c.score for c in res.chunks] == sorted((c.score for c in res.chunks), reverse=True)


def test_each_leg_can_be_switched_off(conn: psycopg.Connection[Any], ds: Dataset) -> None:
    p = plan()
    emb = FakeEmbedder(ds, 7)
    lex = retrieve(
        conn,
        p,
        "dividend",
        embedder=None,
        reranker=None,
        cfg=RetrievalConfig(dense=False, rerank=False, filters=False),
    )
    assert lex.chunk_ids and "embed" not in lex.timings_ms and "dense" not in lex.timings_ms
    dense = retrieve(
        conn,
        p,
        "dividend",
        embedder=emb,
        reranker=None,
        cfg=RetrievalConfig(lexical=False, rerank=False, filters=False, top_k=1),
    )
    assert dense.chunk_ids == [ds.ids[7]] and "lexical" not in dense.timings_ms
    with pytest.raises(ValueError, match="needs an embedder"):
        retrieve(conn, p, "q", embedder=None, reranker=KeywordReranker("x"))
    with pytest.raises(ValueError, match="needs a reranker"):
        retrieve(conn, p, "q", embedder=emb, reranker=None)


def test_the_section_hint_is_a_boost_not_a_filter(
    conn: psycopg.Connection[Any], ds: Dataset
) -> None:
    """Chunk 33 sits far down the fused list; marking its section as a hinted one lifts it, and a
    hint no chunk has leaves the results the same as no hint at all (it never filters)."""
    with psycopg.connect(ds.dsn, autocommit=True) as admin:
        admin.execute("UPDATE chunks_v1 SET section = '7' WHERE chunk_id = %s", [ds.ids[33]])
    cfg = RetrievalConfig(rerank=False, filters=False, top_k=60, per_list=60)
    base = retrieve(
        conn, plan(), "dividend covenant", embedder=FakeEmbedder(ds, 5), reranker=None, cfg=cfg
    )
    hinted = retrieve(
        conn,
        plan(section_hints=["7"]),
        "dividend covenant",
        embedder=FakeEmbedder(ds, 5),
        reranker=None,
        cfg=cfg,
    )
    nohit = retrieve(
        conn,
        plan(section_hints=["8"]),
        "dividend covenant",
        embedder=FakeEmbedder(ds, 5),
        reranker=None,
        cfg=cfg,
    )
    assert set(hinted.chunk_ids) == set(base.chunk_ids), "a boost adds and removes nothing"
    assert nohit.chunk_ids == base.chunk_ids
    pos = {cid: i for i, cid in enumerate(base.chunk_ids)}
    assert hinted.chunk_ids.index(ds.ids[33]) <= pos[ds.ids[33]]
    assert DEFAULT_SECTION_BONUS > 0
    with psycopg.connect(ds.dsn, autocommit=True) as admin:  # leave the shared data as found
        admin.execute("UPDATE chunks_v1 SET section = NULL WHERE chunk_id = %s", [ds.ids[33]])


def test_timings_and_candidate_counts_are_reported(
    conn: psycopg.Connection[Any], ds: Dataset
) -> None:
    res = retrieve(
        conn, plan(), "dividend", embedder=FakeEmbedder(ds, 1), reranker=KeywordReranker("dividend")
    )
    assert {"embed", "lexical", "dense", "fuse_fetch", "rerank"} <= set(res.timings_ms)
    assert all(v >= 0 for v in res.timings_ms.values()) and res.n_candidates > 0
    assert np.isfinite(sum(res.timings_ms.values()))
