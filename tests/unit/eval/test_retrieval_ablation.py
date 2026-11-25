"""eval/retrieval_ablation.py: question building, the six rows, per-question hits, summaries."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.engine.retrieval.query import RetrievalConfig, RetrievalResult
from eval import retrieval_ablation as ab
from eval.schemas import Evidence, FinanceBenchRow, NarrativeRow


def fb_row(rid: str = "fb-1", company: str = "3M", doc: str = "3M_2018_10K") -> FinanceBenchRow:
    return FinanceBenchRow(
        id=rid,
        question="What was capex?",
        gold_answer="x",
        company=company,
        doc_name=doc,
        evidence=[Evidence(page=1, text="t")],
        question_type="m",
        reasoning="r",
    )


def nar_row(rid: str = "nar-1") -> NarrativeRow:
    return NarrativeRow(
        id=rid,
        question="What risks?",
        cik=5,
        fiscal_year=2021,
        gold_chunk_ids=["g1"],
        gold_points=["p"],
        drafted_by="d",
        verified_by="v",
        verified_at=date(2026, 1, 1),
    )


def test_fiscal_year_comes_from_the_doc_name() -> None:
    assert ab.fiscal_year_of("3M_2018_10K") == 2018
    assert ab.fiscal_year_of("AMERICAN_WATER_WORKS_2022_10K") == 2022
    # quarterly, earnings and 8-K documents (15 of the 45 dev FinanceBench rows)
    assert ab.fiscal_year_of("Pfizer_2023Q2_10Q") == 2023
    assert ab.fiscal_year_of("MGMRESORTS_2022Q4_EARNINGS") == 2022
    assert ab.fiscal_year_of("AMCOR_2022_8K_dated-2022-07-01") == 2022
    with pytest.raises(ValueError, match="no fiscal year"):
        ab.fiscal_year_of("somefile")


def test_rows_add_one_component_at_a_time_and_end_at_the_default() -> None:
    names = [n for n, _ in ab.ROWS]
    assert names == [
        "lexical only",
        "dense only",
        "hybrid (RRF)",
        "hybrid + rerank",
        "hybrid + rerank + filters",
        "hybrid + rerank + filters + section boost (oracle hint)",
    ]
    cfgs = dict(ab.ROWS)
    flags = ("lexical", "dense", "rerank", "filters", "section_boost")
    on = {n: tuple(getattr(c, f) for f in flags) for n, c in cfgs.items()}
    assert on["lexical only"] == (True, False, False, False, False)
    assert on["dense only"] == (False, True, False, False, False)
    assert on["hybrid (RRF)"] == (True, True, False, False, False)
    assert on["hybrid + rerank"] == (True, True, True, False, False)
    assert on["hybrid + rerank + filters"] == (True, True, True, True, False)
    assert cfgs["hybrid + rerank + filters + section boost (oracle hint)"] == RetrievalConfig()


def test_questions_use_an_oracle_plan_and_skip_rows_without_gold() -> None:
    items = [
        ("financebench", fb_row("fb-1"), "dev"),
        ("financebench", fb_row("fb-2"), "dev"),
        ("narrative", nar_row(), "dev"),
    ]
    qs = ab.build_questions(items, {"3M": 66740}, {"fb-1": ["fb:3M#p1#0"]}, {5: "Five Inc"})
    assert [q.id for q in qs] == ["fb-1", "nar-1"], (
        "a row with no gold chunk is unscorable, not a miss"
    )
    fb, nar = qs
    assert [(c.mention, c.cik) for c in fb.plan.companies] == [("3M", 66740)]
    assert [p.fiscal_year for p in fb.plan.periods] == [2018] and fb.gold == ["fb:3M#p1#0"]
    assert [(c.mention, c.cik) for c in nar.plan.companies] == [("Five Inc", 5)]
    assert nar.plan.periods[0].fiscal_year == 2021 and nar.gold == ["g1"]


def test_run_ablation_scores_hit_at_k_and_only_passes_what_a_row_uses() -> None:
    qs = ab.build_questions(
        [("narrative", nar_row("n1"), "dev"), ("narrative", nar_row("n2"), "dev")],
        {},
        {},
        {5: "Five Inc"},
    )
    seen: list[dict[str, Any]] = []

    def fake_retrieve(conn: Any, plan: Any, question: str, **kw: Any) -> RetrievalResult:
        seen.append({"embedder": kw["embedder"], "reranker": kw["reranker"], "cfg": kw["cfg"]})
        # gold "g1" is rank 3 for the first question, absent for the second
        from app.engine.retrieval.query import RankedChunk

        ids = ["a", "b", "g1"] if len(seen) % 2 == 1 else ["a", "b", "c"]
        return RetrievalResult(
            chunks=[RankedChunk(i, n, 1.0, None, "t") for n, i in enumerate(ids, 1)],
            queries=[question],
            filters={},
            relaxed_year_filter=len(seen) % 2 == 0,
        )

    rows = [("lexical only", ab.ROWS[0][1]), ("hybrid + rerank", ab.ROWS[3][1])]
    out = ab.run_ablation(
        qs,
        rows,
        retrieve_fn=fake_retrieve,
        conn=None,
        embedder="E",
        reranker="R",
        log=lambda _: None,
    )
    assert seen[0]["embedder"] is None and seen[0]["reranker"] is None, "lexical needs neither"
    assert seen[2]["embedder"] == "E" and seen[2]["reranker"] == "R"
    q1, q2 = out[0]["per_question"]
    assert q1["hits"] == {"1": 0.0, "3": 1.0, "5": 1.0, "8": 1.0} and q2["hits"]["8"] == 0.0
    assert q1["relaxed_year_filter"] is False and q2["relaxed_year_filter"] is True
    assert out[0]["config"]["dense"] is False and q1["latency_ms"] >= 0


def test_summarize_reports_recall_with_ci_latency_and_relaxations() -> None:
    row = {
        "name": "x",
        "config": {},
        "per_question": [
            {
                "id": "a",
                "set": "narrative",
                "latency_ms": 10.0,
                "relaxed_year_filter": False,
                "hits": {"1": 1.0, "3": 1.0, "5": 1.0, "8": 1.0},
            },
            {
                "id": "b",
                "set": "narrative",
                "latency_ms": 30.0,
                "relaxed_year_filter": True,
                "hits": {"1": 0.0, "3": 0.0, "5": 1.0, "8": 1.0},
            },
        ],
    }
    (s,) = ab.summarize([row])
    nar = s["sets"]["narrative"]
    assert nar["n"] == 2 and nar["recall_at_1"] == 0.5 and nar["recall_at_8"] == 1.0
    assert len(nar["recall_at_5_ci95"]) == 2 and nar["recall_at_8_ci95"] == [1.0, 1.0]
    assert s["latency_ms"]["p50"] == 20.0 and s["n_relaxed_year_filter"] == 1
    assert s["per_question"] == row["per_question"], "the flags stay for paired comparisons"


def test_the_oracle_section_hint_is_the_gold_chunks_own_section() -> None:
    assert ab.gold_section(["0000320193-24-000123#1A#0004", "0000320193-24-000123#1A#0007"]) == "1A"
    assert ab.gold_section(["a#7#0001", "a#1A#0002"]) is None, "mixed sections: no single hint"
    assert ab.gold_section(["fb:3M_2018_10K#p59#0000"]) is None, "a PDF chunk has no section"
    plan = ab.oracle_plan("Five Inc", 5, 2021, "7")
    assert plan.section_hints == ["7"] and ab.oracle_plan("x", 1, 2020).section_hints == []
    row = nar_row().model_copy(update={"gold_chunk_ids": ["acc#7#0001"]})
    (q,) = ab.build_questions([("narrative", row, "dev")], {}, {}, {5: "Five Inc"})
    assert q.plan.section_hints == ["7"], "otherwise the boost row is a copy of the row before it"


def test_every_row_of_the_filtered_set_filters_and_each_set_has_its_own_folder() -> None:
    from eval.retrieval_ablation import FILTERED_ROWS, OUT_DIRS, ROW_SETS

    assert all(cfg.filters and not cfg.section_boost for _, cfg in FILTERED_ROWS)
    assert len({name for name, _ in FILTERED_ROWS}) == len(FILTERED_ROWS)
    assert set(OUT_DIRS) == set(ROW_SETS) and len(set(OUT_DIRS.values())) == len(OUT_DIRS)
    by_name = dict(FILTERED_ROWS)
    assert by_name["lexical + filters"].lexical and not by_name["lexical + filters"].dense
    assert by_name["hybrid + filters + rerank"].rerank
