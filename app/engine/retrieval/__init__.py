"""Retrieval primitives over the live serving views (M03 section 3.4; used in full by M5)."""

from app.engine.retrieval.dense import dense_search, vector_literal
from app.engine.retrieval.fuse import hybrid_search, rrf
from app.engine.retrieval.lexical import lexical_search
from app.engine.retrieval.types import ChunkHit

__all__ = [
    "ChunkHit",
    "dense_search",
    "hybrid_search",
    "lexical_search",
    "rrf",
    "vector_literal",
]
