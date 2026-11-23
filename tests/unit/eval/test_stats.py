"""Bootstrap utilities checked against known distributions (M04 step 4)."""

from __future__ import annotations

import numpy as np
import pytest

from eval.stats import bootstrap_ci, paired_bootstrap, percentile


def test_bernoulli_ci_covers_true_rate_and_matches_normal_width() -> None:
    """n=400 draws at p=0.7: the CI contains 0.7 and its width is close to the normal
    approximation 2 * 1.96 * sqrt(p(1-p)/n) = 0.0898."""
    rng = np.random.default_rng(1)
    x = (rng.random(400) < 0.7).astype(float)
    lo, hi = bootstrap_ci(x)
    assert lo < x.mean() < hi
    assert lo < 0.7 < hi
    expected = 2 * 1.96 * np.sqrt(x.mean() * (1 - x.mean()) / 400)
    assert abs((hi - lo) - expected) < 0.01


def test_normal_ci_matches_standard_error() -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(10.0, 2.0, size=1_000)
    lo, hi = bootstrap_ci(x)
    se = x.std(ddof=1) / np.sqrt(x.size)
    assert abs((hi - lo) - 2 * 1.96 * se) < 0.03
    assert lo < 10.0 < hi


def test_ci_coverage_is_near_nominal() -> None:
    """Over 200 Bernoulli(0.6) samples of n=100, about 95% of CIs contain 0.6."""
    rng = np.random.default_rng(3)
    hits = 0
    for i in range(200):
        x = (rng.random(100) < 0.6).astype(float)
        lo, hi = bootstrap_ci(x, n_resamples=2_000, seed=i)
        hits += lo <= 0.6 <= hi
    assert 0.90 <= hits / 200 <= 0.99


def test_same_seed_same_ci_and_different_seed_differs() -> None:
    x = [1, 0, 1, 1, 0, 1, 0, 1, 1, 1] * 5
    assert bootstrap_ci(x) == bootstrap_ci(x)
    # Continuous values: binary means sit on a 1/n grid, so two seeds can share quantiles.
    y = np.random.default_rng(0).normal(size=50)
    assert bootstrap_ci(y, seed=1) != bootstrap_ci(y, seed=2)


def test_constant_sample_has_zero_width_ci() -> None:
    assert bootstrap_ci([1.0] * 30) == (1.0, 1.0)


@pytest.mark.parametrize("bad", [[], [1.0, float("nan")], [[1.0, 2.0]]])
def test_bad_inputs_raise(bad: list) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError):
        bootstrap_ci(bad)


def test_paired_bootstrap_detects_a_real_difference() -> None:
    """System A is right on 80% of 300 questions, B on the same plus nothing else minus 15
    points: the paired CI excludes 0 and the mean difference is exactly the gap."""
    rng = np.random.default_rng(4)
    a = (rng.random(300) < 0.8).astype(float)
    b = a.copy()
    flip = rng.choice(np.flatnonzero(a == 1), size=45, replace=False)
    b[flip] = 0
    res = paired_bootstrap(a, b)
    assert res.mean_diff == pytest.approx(45 / 300)
    assert res.ci95[0] > 0
    assert res.p_two_sided < 0.01
    assert res.n == 300


def test_paired_bootstrap_identical_systems_has_p_one_and_zero_ci() -> None:
    a = [1, 0, 1, 1, 0] * 20
    res = paired_bootstrap(a, a)
    assert res.mean_diff == 0.0 and res.ci95 == (0.0, 0.0)
    assert res.p_two_sided == 1.0


def test_paired_bootstrap_p_is_never_zero() -> None:
    """All resampled means on one side: p is the floor 2/(B+1), not 0."""
    res = paired_bootstrap([1] * 10, [0] * 10, n_resamples=999)
    assert res.p_two_sided == pytest.approx(2 / 1000)


def test_paired_bootstrap_null_rejection_rate_is_near_alpha() -> None:
    """Two independent systems with the same 50% accuracy: the CI excludes 0 in about 5% of
    200 trials (a calibration check, not a single lucky draw)."""
    rng = np.random.default_rng(5)
    rejections = 0
    for i in range(200):
        a = (rng.random(150) < 0.5).astype(float)
        b = (rng.random(150) < 0.5).astype(float)
        lo, hi = paired_bootstrap(a, b, n_resamples=2_000, seed=i).ci95
        rejections += not (lo <= 0 <= hi)
    assert rejections / 200 <= 0.10


def test_pairing_is_tighter_than_independent_resampling() -> None:
    """Highly correlated systems: the paired CI is much narrower than the unpaired difference
    of two marginal CIs would suggest (the reason the paired test exists)."""
    rng = np.random.default_rng(6)
    base = (rng.random(300) < 0.6).astype(float)
    a = base.copy()
    b = base.copy()
    b[:15] = 0
    a[:15] = 1
    res = paired_bootstrap(a, b)
    a_lo, a_hi = bootstrap_ci(a)
    b_lo, b_hi = bootstrap_ci(b)
    assert (res.ci95[1] - res.ci95[0]) < ((a_hi - a_lo) + (b_hi - b_lo)) / 2


def test_paired_bootstrap_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="length"):
        paired_bootstrap([1, 0, 1], [1, 0])


def test_paired_bootstrap_is_deterministic() -> None:
    a, b = [1, 0, 1, 1, 0] * 10, [0, 0, 1, 1, 0] * 10
    assert paired_bootstrap(a, b) == paired_bootstrap(a, b)


def test_percentile() -> None:
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile(list(range(101)), 95) == 95.0
