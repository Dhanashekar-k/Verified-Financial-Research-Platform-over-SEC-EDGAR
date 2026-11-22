"""The pure measurement helpers of the retrieval bench (`serving/bench/retrieval_bench.py`)."""

from __future__ import annotations

import random

import pytest

from serving.bench.retrieval_bench import (
    cpu_steal_fraction,
    cpu_steal_of_busy_time,
    lexical_question,
    percentiles,
    pick_default_ef,
    pseudo_query,
    recall_at_k,
)


def test_recall_is_the_share_of_the_exact_top_k_that_was_found():
    exact = [f"c{i}" for i in range(10)]

    assert recall_at_k(exact, exact) == 1.0
    assert recall_at_k([*exact[:7], "x1", "x2", "x3"], exact) == pytest.approx(0.7)
    assert recall_at_k(["x"] * 10, exact) == 0.0
    assert recall_at_k(exact[::-1], exact) == 1.0  # order inside the top-k does not matter


def test_recall_with_no_ground_truth_is_one_not_a_crash():
    assert recall_at_k(["a"], []) == 1.0


def test_percentiles_of_a_known_sample():
    out = percentiles(list(range(1, 101)))

    assert out["n"] == 100
    assert out["p50_ms"] == pytest.approx(50.5)
    assert out["p95_ms"] == pytest.approx(95.05)
    assert out["max_ms"] == 100.0


def test_pseudo_query_is_a_middle_span_and_short_chunks_give_none():
    text = " ".join(f"word{i}" for i in range(100))

    q = pseudo_query(text, random.Random(1))

    assert q is not None and len(q.split()) == 12
    assert q.split()[0] not in {"word0", "word1"}  # not the opening of the chunk
    assert pseudo_query("too short to use", random.Random(1)) is None


def test_pseudo_query_is_deterministic_for_a_seed():
    text = " ".join(f"w{i}" for i in range(200))

    assert pseudo_query(text, random.Random(7)) == pseudo_query(text, random.Random(7))


def test_lexical_question_defaults_to_an_and_query_and_or_is_explicit():
    text = "the supply chain disruptions affected supply of components and logistics"

    assert " or " not in lexical_question(text)
    assert set(lexical_question(text).split()) == {
        "disruptions",
        "components",
        "logistics",
        "supply",
        "chain",
        "affected",
    }


def test_lexical_question_is_a_broad_or_query_of_distinct_long_words():
    q = lexical_question(
        "the supply chain disruptions affected supply of components and logistics", mode="or"
    )

    terms = q.split(" or ")
    assert len(terms) == len(set(t.lower() for t in terms)) <= 6
    assert "disruptions" in terms and "components" in terms
    assert lexical_question("a b c") == "a b c"  # nothing long enough: keep the text


def test_cpu_steal_fraction_from_two_proc_stat_lines():
    before = "cpu  100 0 100 800 0 0 0 0 0 0"
    after = "cpu  200 0 200 1500 0 0 0 100 0 0"  # +100 +100 +700 idle, +100 steal = 1000 total

    assert cpu_steal_fraction(before, after) == pytest.approx(0.1)
    assert cpu_steal_fraction(before, before) == 0.0


def test_default_ef_is_the_smallest_one_that_reaches_the_target():
    curve = {40: {"recall_at_10": 0.91}, 100: {"recall_at_10": 0.96}, 200: {"recall_at_10": 0.99}}

    assert pick_default_ef(curve) == 100
    assert pick_default_ef(curve, target=0.995) is None


def test_steal_as_a_share_of_busy_time_is_not_diluted_by_idle():
    before = "cpu  0 0 0 0 0 0 0 0 0 0"
    after = "cpu  80 0 0 900 0 0 0 20 0 0"  # 1000 total, 900 idle, 100 busy of which 20 stolen

    assert cpu_steal_fraction(before, after) == pytest.approx(0.02)  # diluted by the idle time
    assert cpu_steal_of_busy_time(before, after) == pytest.approx(0.2)
