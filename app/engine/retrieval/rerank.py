"""Cross-encoder rerank of the fused candidates (M05 section 3.6, ADR-009).

The model is loaded once per process (``load_cross_encoder`` is cached) and never on import, so
importing the engine costs nothing and a unit test can pass a fake ``Reranker``. The rerank input
is capped (``RERANK_CAP``): a cross-encoder over the full top 100 makes latency explode (M05
section 7).
Which model is the default is an ablation result, not an assumption: MiniLM-L6 first (M05), the
larger ``BAAI/bge-reranker-base`` as the alternative to measure.
"""

from __future__ import annotations

import functools
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_RERANKER = "Xenova/ms-marco-MiniLM-L-6-v2"
ALTERNATIVE_RERANKER = "BAAI/bge-reranker-base"
RERANK_CAP = 30
DEFAULT_CACHE_DIR = Path("data/models/rerankers")


class Reranker(Protocol):
    name: str

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """One relevance score per passage, higher is better, in input order."""
        ...


@dataclass(frozen=True)
class Candidate:
    chunk_id: str
    text: str


@functools.lru_cache(maxsize=2)
def load_cross_encoder(model_name: str, cache_dir: str, threads: int | None) -> Any:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    # local_files_only: a missing model raises, it never starts a download mid-request.
    return TextCrossEncoder(
        model_name=model_name, cache_dir=cache_dir, threads=threads, local_files_only=True
    )


def fetch_cross_encoder(model_name: str, cache_dir: Path) -> Path:
    """Download ``model_name`` into ``cache_dir`` (the one place a reranker is fetched; a read from
    Hugging Face) and return the ONNX file, whose sha256 is then worth pinning."""
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    TextCrossEncoder(model_name=model_name, cache_dir=str(cache_dir))
    found = sorted(cache_dir.rglob("*.onnx"))
    if not found:
        raise RuntimeError(f"no .onnx file under {cache_dir} after fetching {model_name}")
    return found[0]


class CrossEncoderReranker:
    """fastembed's ONNX cross-encoder. The weights come from ``cache_dir`` (``make fetch-reranker``
    downloads them); a missing model raises ``ValueError`` and never fetches silently."""

    def __init__(
        self,
        model_name: str = DEFAULT_RERANKER,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        threads: int | None = 2,
    ) -> None:
        self.name = model_name
        self._cache_dir = str(cache_dir)
        self._threads = threads

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        model = load_cross_encoder(self.name, self._cache_dir, self._threads)
        scores = [float(s) for s in model.rerank(query, list(passages))]
        if len(scores) != len(passages):
            raise RuntimeError(
                f"reranker returned {len(scores)} scores for {len(passages)} passages"
            )
        return scores


def rerank(
    query: str,
    candidates: Sequence[Candidate],
    reranker: Reranker,
    *,
    top_k: int,
    cap: int = RERANK_CAP,
) -> list[tuple[str, float]]:
    """The best ``top_k`` of the first ``cap`` candidates (their incoming order is the fused
    order), by reranker score, best first. Ties keep the incoming order. Candidates beyond ``cap``
    are never scored and never returned."""
    head = list(candidates[:cap])
    scores = reranker.score(query, [c.text for c in head])
    order = sorted(range(len(head)), key=lambda i: (-scores[i], i))
    return [(head[i].chunk_id, scores[i]) for i in order[:top_k]]
