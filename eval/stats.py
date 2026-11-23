"""Bootstrap confidence intervals and the paired bootstrap for system-vs-system differences.

Percentile bootstrap, 10,000 resamples and a fixed seed by default (.claude/rules/eval.md).
The paired bootstrap resamples question indices once and applies them to both systems, so the
difference keeps the per-question pairing; that is what backs a claim like "+15 points".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

DEFAULT_RESAMPLES = 10_000
DEFAULT_SEED = 13


@dataclass(frozen=True)
class PairedResult:
    mean_diff: float  # mean(a) - mean(b)
    ci95: tuple[float, float]
    p_two_sided: float  # bootstrap two-sided p-value for "no difference"
    n: int


def _as_array(values: Sequence[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.ndim != 1:
        raise ValueError("values must be one-dimensional")
    if arr.size == 0:
        raise ValueError("cannot bootstrap an empty sample")
    if not np.all(np.isfinite(arr)):
        raise ValueError("values must be finite")
    return arr


def bootstrap_ci(
    values: Sequence[float],
    *,
    n_resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile CI of the mean. Booleans count as 0/1, so this covers accuracy and rates."""
    arr = _as_array(values)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(n_resamples, arr.size))
    means = arr[idx].mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def paired_bootstrap(
    a: Sequence[float],
    b: Sequence[float],
    *,
    n_resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    alpha: float = 0.05,
) -> PairedResult:
    """Bootstrap the mean of per-question differences a_i - b_i (same questions, same order)."""
    arr_a, arr_b = _as_array(a), _as_array(b)
    if arr_a.size != arr_b.size:
        raise ValueError(f"paired samples differ in length: {arr_a.size} vs {arr_b.size}")
    diff = arr_a - arr_b
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, diff.size, size=(n_resamples, diff.size))
    means = diff[idx].mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    # Two-sided p: twice the smaller tail on either side of zero, with the +1 correction so a
    # bootstrap p is never exactly 0 (its floor is 2 / (B + 1)); capped at 1.
    tail = min(int(np.sum(means <= 0)), int(np.sum(means >= 0)))
    p = min(1.0, 2.0 * (tail + 1) / (n_resamples + 1))
    return PairedResult(float(diff.mean()), (float(lo), float(hi)), p, int(diff.size))


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (q in [0, 100]) for latency summaries."""
    return float(np.percentile(_as_array(values), q))
