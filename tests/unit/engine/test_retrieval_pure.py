"""Pure parts of retrieval: OR queries, query building, filters, boosted RRF, rerank."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from app.engine.plan.schema import CompanyRef, PeriodRef, QueryPlan
from app.engine.retrieval.fuse import RRF_K, rrf, rrf_boosted
from app.engine.retrieval.query import (
    DEFAULT_SECTION_BONUS,
    RetrievalConfig,
    build_queries,
    or_query,
    plan_filters,
)
from app.engine.retrieval.rerank import RERANK_CAP, Candidate, rerank


class FakeReranker:
    """A scoring table, standing in for the ONNX model (a collaborator, not the unit)."""

    name = "fake"

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores
        self.seen: list[list[str]] = []

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        self.seen.append(list(passages))
        return [self.scores.get(p, 0.0) for p in passages]


# --- or_query ----------------------------------------------------------------------------------


def test_or_query_keeps_content_words_and_ors_them() -> None:
    q = or_query("What supply chain risks did Nike disclose in its FY2022 10-K?")
    assert q == "supply or chain or risks or nike or disclose or fy2022 or 10"
    assert " and " not in q


def test_or_query_edge_cases() -> None:
    assert or_query("") == "" and or_query("the of and") == ""
    assert or_query("Risk risk RISK") == "risk"  # deduplicated, case-folded
    assert len(or_query(" ".join(f"term{i}" for i in range(100))).split(" or ")) == 24
    assert or_query("DROP TABLE chunks; --") == "drop or table or chunks"  # punctuation is gone


# --- queries and filters -----------------------------------------------------------------------


def _plan(**kw: object) -> QueryPlan:
    base: dict[str, object] = {
        "intent": "narrative",
        "companies": [CompanyRef(mention="Nike", cik=320187)],
        "periods": [PeriodRef(fiscal_year=2022)],
        "narrative_topics": ["supply chain risk", "contract manufacturers"],
    }
    base.update(kw)
    return QueryPlan(**base)  # type: ignore[arg-type]


def test_queries_are_company_plus_topics_then_the_question() -> None:
    cfg = RetrievalConfig()
    qs = build_queries(_plan(), "What supply-chain risks did Nike disclose?", cfg)
    assert qs == [
        "Nike supply chain risk contract manufacturers",
        "What supply-chain risks did Nike disclose?",
    ]


def test_without_topics_or_the_second_query_there_is_still_one_query() -> None:
    q = "How did margins move?"
    assert build_queries(_plan(narrative_topics=[]), q, RetrievalConfig()) == [q]
    one = build_queries(_plan(), q, RetrievalConfig(second_query=False))
    assert one == ["Nike supply chain risk contract manufacturers"]
    assert (
        build_queries(_plan(companies=[], narrative_topics=["risk"]), q, RetrievalConfig())[0]
        == "risk"
    )


def test_filters_come_from_the_plan_and_skip_unresolved_companies() -> None:
    plan = _plan(
        companies=[
            CompanyRef(mention="Nike", cik=320187),
            CompanyRef(mention="Unknown"),
            CompanyRef(mention="Apple", cik=320193),
        ],
        periods=[PeriodRef(fiscal_year=2023), PeriodRef(fiscal_year=2022, fiscal_period="Q2")],
    )
    assert plan_filters(plan) == {"ciks": [320187, 320193], "fiscal_years": [2022, 2023]}
    assert plan_filters(QueryPlan(intent="narrative")) == {"ciks": [], "fiscal_years": []}


def test_a_config_needs_a_retrieval_leg() -> None:
    with pytest.raises(ValueError, match="at least one"):
        RetrievalConfig(lexical=False, dense=False)


# --- boosted RRF -------------------------------------------------------------------------------


def test_boost_is_additive_soft_and_cannot_add_ids() -> None:
    lists = [["a", "b", "c", "d"], ["b", "a", "d", "c"]]
    plain = dict(rrf(lists))
    boosted = dict(rrf_boosted(lists, {"c": 0.01, "zzz": 1.0}))
    assert boosted["c"] == pytest.approx(plain["c"] + 0.01)
    assert "zzz" not in boosted, "a boost never invents a candidate"
    assert {k: v for k, v in boosted.items() if k != "c"} == {
        k: v for k, v in plain.items() if k != "c"
    }
    assert rrf_boosted(lists) == rrf(lists)


def test_a_boost_can_reorder_but_does_not_filter() -> None:
    lists = [["a", "b", "c"]]
    order = [cid for cid, _ in rrf_boosted(lists, {"c": 10 * DEFAULT_SECTION_BONUS})]
    assert order[0] == "c" and set(order) == {"a", "b", "c"}


def test_the_default_bonus_is_a_few_rank_steps_not_a_takeover() -> None:
    step = 1 / (RRF_K + 1) - 1 / (RRF_K + 2)
    assert 2 * step < DEFAULT_SECTION_BONUS < 4 * step
    assert DEFAULT_SECTION_BONUS < 1 / (RRF_K + 1)  # below a rank-1 hit's whole score


# --- rerank ------------------------------------------------------------------------------------


def test_rerank_orders_by_score_and_keeps_ties_in_incoming_order() -> None:
    cands = [Candidate(f"c{i}", f"t{i}") for i in range(5)]
    rr = FakeReranker({"t3": 9.0, "t1": 5.0, "t0": 5.0})
    out = rerank("q", cands, rr, top_k=3)
    assert out == [("c3", 9.0), ("c0", 5.0), ("c1", 5.0)]


def test_rerank_only_scores_the_first_cap_candidates() -> None:
    cands = [Candidate(f"c{i}", f"t{i}") for i in range(100)]
    rr = FakeReranker({"t99": 1000.0})  # beyond the cap: must never win
    out = rerank("q", cands, rr, top_k=8)
    assert len(rr.seen[0]) == RERANK_CAP == 30
    assert "c99" not in {cid for cid, _ in out} and len(out) == 8


def test_rerank_of_nothing_is_nothing() -> None:
    assert rerank("q", [], FakeReranker({}), top_k=8) == []


def test_a_missing_reranker_model_raises_and_never_downloads(tmp_path: Path) -> None:
    """`local_files_only`: an empty cache directory is an error, not a silent fetch."""
    from app.engine.retrieval.rerank import CrossEncoderReranker

    with pytest.raises(ValueError, match="Could not load model"):
        CrossEncoderReranker(cache_dir=tmp_path).score("q", ["passage"])
    assert list(tmp_path.iterdir()) == [], "nothing was written"
    assert CrossEncoderReranker(cache_dir=tmp_path).score("q", []) == []
