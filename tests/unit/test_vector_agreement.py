"""The comparison math of the x86-vs-ARM embedding check (`vector_agreement`, ADR-036). No cloud."""

from __future__ import annotations

import numpy as np
import pytest

from pipelines.common.vector_agreement import (
    MIN_COSINE,
    cosine_rows,
    parse_athena_array,
    stratified_pick,
    summarize,
)


def unit(rows: int, dim: int = 384, seed: int = 0) -> np.ndarray:
    v = np.random.default_rng(seed).normal(size=(rows, dim)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def test_identical_vectors_have_cosine_one_and_pass():
    a = unit(20)

    result = summarize(a, a.copy())

    assert result.n == 20
    assert result.min_cosine == pytest.approx(1.0, abs=1e-6)
    assert result.max_abs_diff == 0.0
    assert result.n_below_gate == 0
    assert result.passed


def test_one_diverging_chunk_fails_the_gate_and_is_counted():
    a = unit(50)
    b = a.copy()
    b[7] = unit(1, seed=99)[0]  # an unrelated vector for one chunk

    result = summarize(a, b)

    assert not result.passed
    assert result.n_below_gate == 1
    assert result.min_cosine < MIN_COSINE
    assert result.mean_cosine > 0.97


def test_a_tiny_perturbation_inside_the_gate_passes():
    a = unit(30)
    b = a + np.random.default_rng(1).normal(scale=1e-4, size=a.shape).astype(np.float32)

    result = summarize(a, b)

    assert result.passed
    assert 0 < result.max_abs_diff < 1e-3


def test_cosine_does_not_assume_unit_norm():
    a = unit(5)

    assert cosine_rows(a * 7.0, a) == pytest.approx(np.ones(5), abs=1e-6)


def test_a_zero_vector_has_cosine_zero_not_nan():
    a, b = unit(3), unit(3, seed=1)
    a[1] = 0.0

    cos = cosine_rows(a, b)

    assert cos[1] == 0.0
    assert not np.isnan(cos).any()


def test_shape_mismatch_and_empty_input_are_errors():
    with pytest.raises(ValueError):
        cosine_rows(unit(3), unit(4))
    with pytest.raises(ValueError):
        summarize(np.zeros((0, 384), dtype=np.float32), np.zeros((0, 384), dtype=np.float32))


def test_the_summary_serializes_to_plain_json_types():
    d = summarize(unit(4), unit(4)).as_dict()

    assert set(d) == {
        "n",
        "min_cosine",
        "mean_cosine",
        "p1_cosine",
        "max_abs_diff",
        "n_below_gate",
        "gate",
        "passed",
    }
    assert all(isinstance(v, (int, float, bool)) for v in d.values())


@pytest.mark.parametrize(
    "cell,expected",
    [
        ("[0.1, -0.25, 3.0]", [0.1, -0.25, 3.0]),
        ("[1.5E-4, -2.0E-5, 0.0]", [1.5e-4, -2.0e-5, 0.0]),
        (" [7.0E+1] ", [70.0]),
    ],
)
def test_athena_array_cells_parse_to_float32(cell: str, expected: list[float]):
    out = parse_athena_array(cell)

    assert out.dtype == np.float32
    assert out == pytest.approx(np.asarray(expected, dtype=np.float32))


def test_stratified_pick_spreads_over_token_bins_and_keeps_input_order():
    tokens = [10, 400, 120, 5, 390, 200, 30, 260, 380, 100, 220, 40]

    picked = stratified_pick(tokens, 8)

    assert picked == sorted(picked)
    assert len(picked) == 8
    bins = {
        0 if t < 50 else 1 if t < 150 else 2 if t < 300 else 3 for t in (tokens[i] for i in picked)
    }
    assert bins == {0, 1, 2, 3}


def test_stratified_pick_gives_a_short_bin_share_to_the_others():
    tokens = [10, 20, 30, 250, 260, 270, 280, 290]  # nothing in the 50-150 or 300+ bins

    assert len(stratified_pick(tokens, 6)) == 6
    assert stratified_pick(tokens, 100) == list(range(len(tokens)))
    assert stratified_pick([], 5) == []


def test_stratified_pick_is_deterministic():
    tokens = list(range(1, 400, 3))

    assert stratified_pick(tokens, 40) == stratified_pick(tokens, 40)
