"""The retrieval pipeline for a QueryPlan (M05 section 3.6).

    queries (company + topics, then the question) -> lexical top-50 + dense top-50 per query
    -> RRF (+ a soft section bonus) -> cross-encoder rerank of the first 30 -> top 8

Filters are ``cik in plan companies`` and ``fiscal_year in plan periods`` (the filing's fiscal
year). A ``section_hints`` match adds an RRF bonus and is never a filter: a wrong planner guess
must not kill recall (M05 section 7). If the filters leave nothing, the year filter is dropped once
(the company filter never is: an answer about the wrong company is worse than none) and the result
says so. Every component can be switched off through ``RetrievalConfig``; that is what the ablation
runs.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg

from app.engine.plan.schema import QueryPlan
from app.engine.retrieval.dense import dense_search
from app.engine.retrieval.fuse import RRF_K, rrf_boosted
from app.engine.retrieval.lexical import lexical_search
from app.engine.retrieval.rerank import RERANK_CAP, Candidate, Reranker, rerank

# One step down the RRF ranking near the top costs about 1/(k+1) - 1/(k+2); the default bonus is
# worth roughly three steps there. A starting point, tuned on dev by the ablation, never on test.
DEFAULT_SECTION_BONUS = 3.0 / (RRF_K + 1) ** 2
MAX_OR_TERMS = 24
_STOP = frozenset(
    "a an and are as at be been by did do does for from had has have how in into is it its of on "
    "or that the their these this to was were what when where which who why will with would".split()
)


class QueryEmbedder(Protocol):
    def embed_queries(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True)
class RetrievalConfig:
    lexical: bool = True
    dense: bool = True
    rerank: bool = True
    filters: bool = True
    section_boost: bool = True
    second_query: bool = True  # also run the question itself next to "company + topics"
    per_list: int = 50
    rerank_cap: int = RERANK_CAP
    top_k: int = 8
    ef_search: int = 800  # ADR-040
    section_bonus: float = DEFAULT_SECTION_BONUS

    def __post_init__(self) -> None:
        if not (self.lexical or self.dense):
            raise ValueError("a retrieval config needs at least one of lexical and dense")


@dataclass(frozen=True)
class RankedChunk:
    chunk_id: str
    rank: int
    score: float
    section: str | None
    text: str


@dataclass
class RetrievalResult:
    chunks: list[RankedChunk]
    queries: list[str]
    filters: dict[str, list[int]]
    relaxed_year_filter: bool = False
    n_candidates: int = 0
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def chunk_ids(self) -> list[str]:
        return [c.chunk_id for c in self.chunks]


def or_query(text: str) -> str:
    """A `websearch_to_tsquery` string that ORs the content words of ``text``. A natural-language
    question ANDed term by term matches almost nothing; ranking (`ts_rank_cd`) does the work."""
    terms: list[str] = []
    for word in re.findall(r"[A-Za-z0-9]+", text.lower()):
        if len(word) >= 2 and word not in _STOP and word not in terms:
            terms.append(word)
    return " or ".join(terms[:MAX_OR_TERMS])


def build_queries(plan: QueryPlan, question: str, cfg: RetrievalConfig) -> list[str]:
    """``company + topics`` first (when the plan has topics), then the question."""
    queries: list[str] = []
    if plan.narrative_topics:
        names = " ".join(c.mention for c in plan.companies)
        queries.append(f"{names} {' '.join(plan.narrative_topics)}".strip())
    if cfg.second_query or not queries:
        queries.append(question)
    return list(dict.fromkeys(queries))


def plan_filters(plan: QueryPlan) -> dict[str, list[int]]:
    """CIKs and filing fiscal years named by the plan (empty list = no filter on that column)."""
    ciks = sorted({c.cik for c in plan.companies if c.cik is not None})
    years = sorted({p.fiscal_year for p in plan.periods})
    return {"ciks": ciks, "fiscal_years": years}


def _timed(timings: dict[str, float], key: str, since: float) -> None:
    timings[key] = timings.get(key, 0.0) + (time.perf_counter() - since) * 1000


def retrieve(
    conn: psycopg.Connection[Any],
    plan: QueryPlan,
    question: str,
    *,
    embedder: QueryEmbedder | None,
    reranker: Reranker | None,
    cfg: RetrievalConfig = RetrievalConfig(),
) -> RetrievalResult:
    if cfg.dense and embedder is None:
        raise ValueError("dense retrieval needs an embedder")
    if cfg.rerank and reranker is None:
        raise ValueError("rerank needs a reranker")
    timings: dict[str, float] = {}
    queries = build_queries(plan, question, cfg)
    filters = plan_filters(plan) if cfg.filters else {"ciks": [], "fiscal_years": []}

    vectors: list[Sequence[float]] = []
    if cfg.dense:
        t = time.perf_counter()
        assert embedder is not None
        vectors = [list(v) for v in embedder.embed_queries(queries)]
        _timed(timings, "embed", t)

    def run_lists(active: dict[str, list[int]]) -> list[list[str]]:
        kw: dict[str, Any] = {}
        if active["ciks"]:
            kw["ciks"] = active["ciks"]
        if active["fiscal_years"]:
            kw["fiscal_years"] = active["fiscal_years"]
        lists: list[list[str]] = []
        for i, q in enumerate(queries):
            if cfg.lexical:
                t0 = time.perf_counter()
                hits = lexical_search(conn, or_query(q), limit=cfg.per_list, **kw)
                _timed(timings, "lexical", t0)
                lists.append([h.chunk_id for h in hits])
            if cfg.dense:
                t0 = time.perf_counter()
                dhits = dense_search(
                    conn, vectors[i], limit=cfg.per_list, ef_search=cfg.ef_search, **kw
                )
                _timed(timings, "dense", t0)
                lists.append([h.chunk_id for h in dhits])
        return lists

    rank_lists = run_lists(filters)
    relaxed = False
    if not any(rank_lists) and filters["fiscal_years"]:
        relaxed = True
        filters = {"ciks": filters["ciks"], "fiscal_years": []}
        rank_lists = run_lists(filters)

    candidates = list(dict.fromkeys(cid for lst in rank_lists for cid in lst))
    hints = set(plan.section_hints) if cfg.section_boost else set()

    t = time.perf_counter()
    sections: dict[str, str | None] = {}
    meta: dict[str, tuple[str | None, str]] = {}

    def fetch_sections(ids: Sequence[str]) -> None:
        rows = conn.execute(
            "SELECT chunk_id, section FROM chunks WHERE chunk_id = ANY(%s)", [list(ids)]
        ).fetchall()
        sections.update({r[0]: r[1] for r in rows})

    def fetch(ids: Sequence[str]) -> None:
        missing = [i for i in ids if i not in meta]
        if not missing:
            return
        rows = conn.execute(
            "SELECT chunk_id, section, text FROM chunks WHERE chunk_id = ANY(%s)", [missing]
        ).fetchall()
        meta.update({r[0]: (r[1], r[2]) for r in rows})

    boosts: dict[str, float] = {}
    if hints and candidates:
        fetch_sections(candidates)  # section only: the text of ~200 candidates is not needed yet
        boosts = {c: cfg.section_bonus for c in candidates if sections.get(c) in hints}
    fused = rrf_boosted(rank_lists, boosts)
    head = [cid for cid, _ in fused[: max(cfg.rerank_cap, cfg.top_k)]]
    fetch(head)
    _timed(timings, "fuse_fetch", t)

    present = [cid for cid in head if cid in meta]  # a chunk swapped out mid-query is skipped
    fused_score = dict(fused)
    if cfg.rerank:
        assert reranker is not None
        t = time.perf_counter()
        ranked = rerank(
            question,
            [Candidate(cid, meta[cid][1]) for cid in present],
            reranker,
            top_k=cfg.top_k,
            cap=cfg.rerank_cap,
        )
        _timed(timings, "rerank", t)
    else:
        ranked = [(cid, fused_score[cid]) for cid in present[: cfg.top_k]]

    chunks = [
        RankedChunk(cid, i, score, meta[cid][0], meta[cid][1])
        for i, (cid, score) in enumerate(ranked, start=1)
    ]
    return RetrievalResult(
        chunks=chunks,
        queries=queries,
        filters=filters,
        relaxed_year_filter=relaxed,
        n_candidates=len(candidates),
        timings_ms={k: round(v, 3) for k, v in timings.items()},
    )
