"""eval/retrieval_ab.py with a fake embedder on a 6-chunk synthetic pool (no real models)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from iceberg_testing import add_stale_file, make_chunks_table

from eval import retrieval_ab as ab

POOL = [
    ab.PoolChunk("fb:A#p1#0000", "A", 1, "alpha revenue"),
    ab.PoolChunk("fb:A#p2#0000", "A", 2, "alpha risk"),
    ab.PoolChunk("fb:A#p3#0000", "A", 3, "alpha capex"),
    ab.PoolChunk("fb:B#p1#0000", "B", 1, "beta revenue"),
    ab.PoolChunk("fb:B#p2#0000", "B", 2, "beta risk"),
    ab.PoolChunk("fb:B#p3#0000", "B", 3, "beta capex"),
]
QUERIES = [
    ab.Query("fb-00001", "alpha revenue", "A", frozenset({1})),
    ab.Query("fb-00002", "beta capex", "B", frozenset({3})),
]


class FakeEmbedder:
    """One-hot embedding of a chunk's text; the instruction flips the query to a decoy chunk."""

    def __init__(self, name: str, decoy_on_instruction: bool) -> None:
        self.name = name
        self.decoy = decoy_on_instruction
        self.vocab = [c.text for c in POOL]

    def _vec(self, text: str) -> np.ndarray:
        v = np.full(len(self.vocab), 0.01, dtype=np.float32)
        v[self.vocab.index(text)] = 1.0
        return v / np.linalg.norm(v)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts])

    def embed_queries(self, questions: Sequence[str], *, instruction: bool) -> np.ndarray:
        # The decoy (with the instruction) points every query at "alpha risk" (A p2).
        texts = ["alpha risk" if (instruction and self.decoy) else q for q in questions]
        return np.stack([self._vec(t) for t in texts])


def _embedders() -> dict[str, ab.Embedder]:
    return {
        "quantized": FakeEmbedder("q-model", decoy_on_instruction=True),
        "fp32": FakeEmbedder("fp32-model", decoy_on_instruction=False),
    }


def test_per_query_metrics_recall_and_rr() -> None:
    m = ab.per_query_metrics([False, False, True] + [False] * 17)
    assert m == {
        "hit_at_5": 1.0,
        "hit_at_10": 1.0,
        "hit_at_20": 1.0,
        "rr_at_10": pytest.approx(1 / 3),
    }
    miss = ab.per_query_metrics([False] * 7 + [True] + [False] * 12)
    assert miss["hit_at_5"] == 0.0 and miss["hit_at_10"] == 1.0 and miss["rr_at_10"] == 1 / 8


def test_page_hit_requires_same_document() -> None:
    q = QUERIES[0]  # doc A page 1
    assert ab.page_hits([POOL[3], POOL[0]], q) == [False, True]  # B p1 is not a hit


def test_exact_top_k_is_best_first_and_deterministic_on_ties() -> None:
    p = np.eye(3, dtype=np.float32)
    q = np.array([[0.0, 1.0, 0.0], [0.0, 0.0, 0.0]], dtype=np.float32)
    top = ab.exact_top_k(q, p, 3)
    assert top[0, 0] == 1
    assert list(top[1]) == [0, 1, 2]  # all tied -> index order


def test_arm_wiring_instruction_and_model() -> None:
    per_arm = ab.run_arms(POOL, QUERIES, _embedders())
    assert set(per_arm) == {a.name for a in ab.ARMS}
    # no instruction: each query finds its own chunk first
    assert [r["rr_at_10"] for r in per_arm["quantized-noinstr"]] == [1.0, 1.0]
    # quantized + instruction: decoy A p2 ranks first; query 1's gold (A p1) is not rank 1
    assert per_arm["quantized+instr"][0]["rr_at_10"] < 1.0
    # fp32 ignores the decoy
    assert [r["rr_at_10"] for r in per_arm["fp32+instr"]] == [1.0, 1.0]


def test_config_rejects_fp32_resolving_to_the_quantized_model() -> None:
    same = {"quantized": FakeEmbedder("m", False), "fp32": FakeEmbedder("m", False)}
    with pytest.raises(ValueError, match="same model"):
        ab.build_config(same, POOL, QUERIES)
    cfg = ab.build_config(_embedders(), POOL, QUERIES)
    assert cfg["models"]["fp32"] != cfg["models"]["quantized"]
    assert cfg["caveat"] == ab.CAVEAT
    assert cfg["incumbent"] == "quantized+instr"


def _metrics(hit_rank: int | None) -> dict[str, float]:
    hits = [False] * 20
    if hit_rank is not None:
        hits[hit_rank] = True
    return ab.per_query_metrics(hits)


def _per_arm(spec: dict[str, list[int | None]]) -> dict[str, list[dict[str, float]]]:
    return {arm: [_metrics(r) for r in ranks] for arm, ranks in spec.items()}


N = 40
NONE: list[int | None] = [None] * N
TOP: list[int | None] = [0] * N


def test_model_trigger_fires_for_a_clearly_better_fp32_arm() -> None:
    per_arm = _per_arm(
        {
            "quantized+instr": NONE,
            "fp32+instr": TOP,
            "quantized-noinstr": NONE,
            "fp32-noinstr": NONE,
        }
    )
    out = ab.summarize(per_arm, n_resamples=500)
    assert out["trigger"]["fired"] is True
    assert out["trigger"]["contrasts"] == ["fp32+instr vs quantized+instr"]
    assert out["trigger"]["caveat"] == ab.CAVEAT
    assert out["arms"]["fp32+instr"]["hit_at_10"] == 1.0
    assert "multiplicity" in out["trigger"]["multiplicity"]


def test_instruction_only_improvement_does_not_fire_the_reembed_trigger() -> None:
    """No-instruction beats instruction in BOTH models: a flag flip, not a re-embed."""
    per_arm = _per_arm(
        {"quantized+instr": NONE, "fp32+instr": NONE, "quantized-noinstr": TOP, "fp32-noinstr": TOP}
    )
    out = ab.summarize(per_arm, n_resamples=500)
    assert out["trigger"]["fired"] is False
    assert out["trigger"]["instruction_off_better"] is True
    key = "quantized-noinstr vs quantized+instr"
    assert out["instruction_effect"][key]["hit_at_10"]["challenger_better"] is True


def test_trigger_does_not_fire_when_the_ci_includes_zero() -> None:
    mixed: list[int | None] = [0, None] * (N // 2)
    shifted: list[int | None] = [None, 0] * (N // 2)  # same rate, different questions
    per_arm = _per_arm(
        {
            "quantized+instr": mixed,
            "fp32+instr": shifted,
            "quantized-noinstr": mixed,
            "fp32-noinstr": mixed,
        }
    )
    out = ab.summarize(per_arm, n_resamples=500)
    c = out["model_effect"]["fp32+instr vs quantized+instr"]["hit_at_10"]
    assert c["ci95"][0] < 0 < c["ci95"][1]
    assert out["trigger"]["fired"] is False


def test_trigger_does_not_fire_for_a_worse_challenger() -> None:
    per_arm = _per_arm(
        {"quantized+instr": TOP, "fp32+instr": NONE, "quantized-noinstr": TOP, "fp32-noinstr": NONE}
    )
    out = ab.summarize(per_arm, n_resamples=500)
    assert out["trigger"]["fired"] is False
    assert out["model_effect"]["fp32+instr vs quantized+instr"]["hit_at_10"]["mean_diff"] == -1.0


def test_trigger_ignores_an_improvement_only_at_hit_at_5() -> None:
    """fp32 moves hits from rank 6-10 to rank 1: hit@5 improves, hit@10 doesn't change."""
    late: list[int | None] = [7] * N
    per_arm = _per_arm(
        {
            "quantized+instr": late,
            "fp32+instr": TOP,
            "quantized-noinstr": late,
            "fp32-noinstr": late,
        }
    )
    out = ab.summarize(per_arm, n_resamples=500)
    contrast = out["model_effect"]["fp32+instr vs quantized+instr"]
    assert contrast["hit_at_5"]["challenger_better"] is True
    assert contrast["hit_at_10"]["challenger_better"] is False
    assert out["trigger"]["fired"] is False


def _pool_rows() -> list[dict[str, object]]:
    return [
        {
            "chunk_id": "fb:A#p1#0000",
            "page": 1,
            "doc_source": "financebench_pdf",
            "text": "alpha",
            "n_tokens": 40,
        },
        {
            "chunk_id": "fb:A#p2#0000",
            "page": 2,
            "doc_source": "financebench_pdf",
            "text": "x",
            "n_tokens": 3,
        },  # degenerate: serving excludes it (ADR-035)
        {
            "chunk_id": "0001#7#0001",
            "page": None,
            "doc_source": "edgar_html",
            "text": "extra",
            "n_tokens": 40,
        },
    ]


def test_load_pool_reads_current_snapshot_excludes_degenerate_and_ignores_stale(
    tmp_path: Path,
) -> None:
    table = make_chunks_table(tmp_path, _pool_rows())
    add_stale_file(table, _pool_rows())  # would duplicate every id if read
    pool, info = ab.load_pool(table)
    assert [c.chunk_id for c in pool] == ["fb:A#p1#0000"]  # no extras requested
    assert info.excluded_degenerate == 1
    assert isinstance(info.snapshot_id, int)
    pool, _ = ab.load_pool(table, ["0001#7#0001"])
    assert [c.chunk_id for c in pool] == ["0001#7#0001", "fb:A#p1#0000"]
    assert len({c.chunk_id for c in pool}) == len(pool)


def test_load_queries_filters_to_the_dev_split(tmp_path: Path) -> None:
    rows = [
        {
            "id": f"fb-0000{i}",
            "question": f"q{i}",
            "gold_answer": "1",
            "company": "A",
            "doc_name": "A",
            "evidence": [{"page": 1, "text": "t"}],
            "question_type": "metrics-generated",
            "reasoning": "r",
        }
        for i in range(1, 4)
    ]
    fb = tmp_path / "financebench.jsonl"
    fb.write_text("".join(json.dumps(r) + "\n" for r in rows))
    splits = tmp_path / "splits.json"
    splits.write_text(
        json.dumps(
            {"sets": {"financebench": {"dev": ["fb-00002"], "test": ["fb-00001", "fb-00003"]}}}
        )
    )
    assert [q.id for q in ab.load_queries(fb, split="dev", splits_path=splits)] == ["fb-00002"]
    assert len(ab.load_queries(fb, split="all")) == 3
    with pytest.raises(FileNotFoundError, match="splits"):
        ab.load_queries(fb, split="dev", splits_path=tmp_path / "missing.json")
    with pytest.raises(FileNotFoundError, match="promotes"):
        ab.load_queries(tmp_path / "nope.jsonl", split="all")
