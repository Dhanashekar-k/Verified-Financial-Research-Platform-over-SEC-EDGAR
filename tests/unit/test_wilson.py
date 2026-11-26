"""Wilson score interval for a proportion (eval.stats), used by the M6 Gate 0 rates."""

from __future__ import annotations

import pytest

from eval.stats import wilson_ci


@pytest.mark.parametrize(
    ("k", "n", "lo", "hi"),
    [
        # Computed from the Wilson formula with z = 1.959964.
        (11, 220, 0.0282, 0.0872),
        (0, 10, 0.0, 0.2775),
        (10, 10, 0.7225, 1.0),
        (5, 10, 0.2366, 0.7634),
    ],
)
def test_known_values(k: int, n: int, lo: float, hi: float) -> None:
    got_lo, got_hi = wilson_ci(k, n)
    assert got_lo == pytest.approx(lo, abs=5e-4)
    assert got_hi == pytest.approx(hi, abs=5e-4)


def test_stays_inside_the_unit_interval_and_contains_the_estimate() -> None:
    for n in range(1, 40):
        for k in range(n + 1):
            lo, hi = wilson_ci(k, n)
            assert 0.0 <= lo <= k / n <= hi <= 1.0


@pytest.mark.parametrize(("k", "n"), [(0, 0), (-1, 5), (6, 5)])
def test_rejects_impossible_counts(k: int, n: int) -> None:
    with pytest.raises(ValueError):
        wilson_ci(k, n)
