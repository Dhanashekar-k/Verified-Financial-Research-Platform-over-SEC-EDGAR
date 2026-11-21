"""Compare two sets of embeddings of the same chunks (the x86-vs-ARM check of M3, ADR-036).

Pure numpy, no cloud calls: `scripts/check_cross_arch.py` reads the Glue vectors and embeds the same
texts locally, then these functions say how closely the two agree.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import numpy as np

# The M3 gate: every chunk must agree to this cosine, or the full embedding run does not start.
MIN_COSINE = 0.9999
BIN_EDGES = (0, 50, 150, 300, 401)  # n_tokens bins; the chunker caps chunks at 400 tokens


def cosine_rows(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosine similarity of row i of `a` with row i of `b` (float64), for any row norms."""
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch: {a.shape} vs {b.shape}")
    x, y = a.astype(np.float64), b.astype(np.float64)
    denom = np.linalg.norm(x, axis=1) * np.linalg.norm(y, axis=1)
    return np.where(denom > 0, np.einsum("ij,ij->i", x, y) / np.where(denom > 0, denom, 1.0), 0.0)


@dataclass(frozen=True)
class Agreement:
    n: int
    min_cosine: float
    mean_cosine: float
    p1_cosine: float
    max_abs_diff: float
    n_below_gate: int
    gate: float
    passed: bool

    def as_dict(self) -> dict[str, float | int | bool]:
        return asdict(self)


def summarize(a: np.ndarray, b: np.ndarray, gate: float = MIN_COSINE) -> Agreement:
    """Row-wise agreement of two (n, dim) matrices against the cosine `gate`."""
    if len(a) == 0:
        raise ValueError("no vectors to compare")
    cos = cosine_rows(a, b)
    diff = np.abs(a.astype(np.float64) - b.astype(np.float64))
    below = int((cos < gate).sum())
    return Agreement(
        n=int(len(cos)),
        min_cosine=float(cos.min()),
        mean_cosine=float(cos.mean()),
        p1_cosine=float(np.percentile(cos, 1)),
        max_abs_diff=float(diff.max()),
        n_below_gate=below,
        gate=gate,
        passed=bool(below == 0 and not np.isnan(cos).any()),
    )


def parse_athena_array(text: str) -> np.ndarray:
    """An Athena `array<float>` cell such as '[0.1, -0.25, 3.0E-4]' as a float32 vector."""
    values = ast.literal_eval(
        text.strip().replace("E-", "e-").replace("E+", "e+").replace("E", "e")
    )
    return np.asarray(values, dtype=np.float32)


def stratified_pick(n_tokens: Sequence[int], n: int) -> list[int]:
    """Indices of up to `n` items spread evenly over the `BIN_EDGES` token bins, in input order.

    The input is assumed to be in a deterministic pseudo-random order (the caller sorts by
    `xxhash64(chunk_id)`), so taking the first items of each bin is reproducible. A bin with too few
    items gives its share to the others, so the total is `min(n, len(n_tokens))`.
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    bins: list[list[int]] = [[] for _ in range(len(BIN_EDGES) - 1)]
    for i, tokens in enumerate(n_tokens):
        for b in range(len(BIN_EDGES) - 1):
            if BIN_EDGES[b] <= tokens < BIN_EDGES[b + 1]:
                bins[b].append(i)
                break
    picked: list[int] = []
    # Round-robin over the bins until n items are taken or every bin is empty.
    cursor = [0] * len(bins)
    while len(picked) < min(n, len(n_tokens)):
        progressed = False
        for b, items in enumerate(bins):
            if cursor[b] < len(items) and len(picked) < n:
                picked.append(items[cursor[b]])
                cursor[b] += 1
                progressed = True
        if not progressed:
            break
    return sorted(picked)
