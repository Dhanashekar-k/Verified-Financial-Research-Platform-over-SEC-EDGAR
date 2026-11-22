"""Shared types of the retrieval primitives."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkHit:
    """One retrieved chunk. `rank` starts at 1; `score` is higher-is-better for its method."""

    chunk_id: str
    rank: int
    score: float
