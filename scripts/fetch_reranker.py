"""Download a cross-encoder reranker into data/models/rerankers (a read from Hugging Face).

    uv run python -m scripts.fetch_reranker [--model Xenova/ms-marco-MiniLM-L-6-v2]

Prints the ONNX file's path, size and sha256: pin that sha in the ADR that chooses the reranker
(the embedder is pinned the same way, `pipelines/common/embedding_artifact.py`).
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from app.engine.retrieval.rerank import DEFAULT_CACHE_DIR, DEFAULT_RERANKER, fetch_cross_encoder


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=DEFAULT_RERANKER)
    ap.add_argument("--dest", type=Path, default=DEFAULT_CACHE_DIR)
    args = ap.parse_args(argv)
    onnx = fetch_cross_encoder(args.model, args.dest)
    digest = hashlib.sha256(onnx.read_bytes()).hexdigest()
    print(f"{args.model}: {onnx} ({onnx.stat().st_size} bytes) sha256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
