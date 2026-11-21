"""Chunk and query embeddings (`BAAI/bge-small-en-v1.5`, dim 384), ADR-032 and ADR-033.

fastembed resolves the name `BAAI/bge-small-en-v1.5` to the QUANTIZED ONNX build
`Qdrant/bge-small-en-v1.5-onnx-Q` (`model_optimized.onnx`), not the fp32 model. The model files
are a local directory (staged in S3 for Glue, `make fetch-model` on a laptop) and are never
downloaded at run time.

Passages are embedded with `passage_embed` and never get an instruction. fastembed's `query_embed`
adds NO BGE query instruction for this model, so queries go through `embed_queries`, which
prepends `QUERY_INSTRUCTION` explicitly.

This module imports no Spark: the Glue job, the loader and the API all use it.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
ARTIFACT_HF_REPO = "Qdrant/bge-small-en-v1.5-onnx-Q"
ARTIFACT_ONNX_FILE = "model_optimized.onnx"
# Commit of ARTIFACT_HF_REPO the model was fetched from (from the Hugging Face download metadata,
# 2026-11-21); `scripts/stage_model.py download` fetches exactly this revision.
ARTIFACT_HF_REVISION = "aa8f8b060edb00e03bfdd08813a2949946c8ba55"
# sha256 of the ONNX file fetched from ARTIFACT_HF_REPO on 2026-11-21 (`make fetch-model`,
# 66,465,124 bytes). Recorded in the export manifest as `embedding_artifact.onnx_sha256`; callers
# that must not drift pass it as `expected_onnx_sha256`.
ARTIFACT_ONNX_SHA256 = "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431"

_HASH_BLOCK = 1 << 20


class ModelIntegrityError(Exception):
    """The ONNX file is missing or its sha256 is not the pinned one."""


def query_text(question: str) -> str:
    """The text a query is embedded as: the BGE instruction followed by the question, unchanged."""
    return QUERY_INSTRUCTION + question


def l2_normalize(vectors: np.ndarray) -> np.ndarray:
    """float32 copy of `vectors` with every nonzero row scaled to unit norm; zero rows stay zero.

    The norm is accumulated in float64 so very large values cannot overflow.
    """
    v = np.asarray(vectors, dtype=np.float64)
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    safe = np.where(norms > 0, norms, 1.0)
    out: np.ndarray = (v / safe).astype(np.float32)
    return out


def batched(items: Sequence[str], size: int) -> Iterator[list[str]]:
    """Consecutive slices of `items` of length `size` (the last may be shorter), in order."""
    if size < 1:
        raise ValueError(f"batch size must be >= 1, got {size}")
    return (list(items[i : i + size]) for i in range(0, len(items), size))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(_HASH_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def verify_model_dir(model_dir: Path, expected_onnx_sha256: str) -> None:
    """Raise `ModelIntegrityError` unless `<model_dir>/model_optimized.onnx` has this sha256."""
    onnx = Path(model_dir) / ARTIFACT_ONNX_FILE
    if not onnx.is_file():
        raise ModelIntegrityError(f"{onnx} is missing")
    actual = sha256_file(onnx)
    if actual != expected_onnx_sha256:
        raise ModelIntegrityError(f"{onnx} sha256 {actual} != pinned {expected_onnx_sha256}")


class Embedder:
    """The quantized bge model loaded from a local directory (one instance per Python process)."""

    def __init__(
        self,
        model_dir: Path,
        *,
        threads: int = 1,
        expected_onnx_sha256: str | None = None,
    ) -> None:
        model_dir = Path(model_dir)
        if expected_onnx_sha256 is not None:
            verify_model_dir(model_dir, expected_onnx_sha256)
        self._model = TextEmbedding(MODEL_NAME, specific_model_path=str(model_dir), threads=threads)

    def _embed(
        self,
        run: Callable[..., Iterable[np.ndarray]],
        texts: Sequence[str],
        batch_size: int,
    ) -> np.ndarray:
        if batch_size < 1:
            raise ValueError(f"batch size must be >= 1, got {batch_size}")
        if len(texts) == 0:
            return np.zeros((0, DIM), dtype=np.float32)
        rows = list(run(list(texts), batch_size=batch_size))
        return l2_normalize(np.stack(rows))

    def embed_passages(self, texts: Sequence[str], batch_size: int = 256) -> np.ndarray:
        """(n, 384) float32 unit vectors for document text; no instruction is ever added."""
        return self._embed(self._model.passage_embed, texts, batch_size)

    def embed_queries(
        self,
        questions: Sequence[str],
        batch_size: int = 256,
        *,
        instruction: bool = True,
    ) -> np.ndarray:
        """(n, 384) float32 unit vectors for questions; the BGE instruction is prepended (ADR-033).

        `instruction=False` embeds the questions as written (the M4 with/without comparison).
        """
        texts = [query_text(q) for q in questions] if instruction else list(questions)
        return self._embed(self._model.query_embed, texts, batch_size)


_CACHE: dict[tuple[str, int, str | None], Embedder] = {}
_LOCK = threading.Lock()


def get_embedder(
    model_dir: str | Path,
    threads: int = 1,
    *,
    expected_onnx_sha256: str | None = None,
) -> Embedder:
    """The process-wide `Embedder` for `(model_dir, threads, pin)`; one model load per process.

    The pin is part of the cache key, so asking for a verified embedder never returns an instance
    that was built without verification (and a wrong pin always raises).
    """
    key = (str(Path(model_dir).resolve()), threads, expected_onnx_sha256)
    with _LOCK:
        embedder = _CACHE.get(key)
        if embedder is None:
            embedder = Embedder(
                Path(model_dir), threads=threads, expected_onnx_sha256=expected_onnx_sha256
            )
            _CACHE[key] = embedder
        return embedder
