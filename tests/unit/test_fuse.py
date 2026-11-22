"""Tests for ``app.engine.retrieval.fuse.rrf`` (reciprocal-rank fusion), pure and DB-free.

Contract: ``score(id) = sum over lists of 1 / (k + rank)`` with rank starting at 1 and ``k = 60`` by
default; best first; ties broken by ``chunk_id`` ascending; an id repeated inside ONE list counts
once, at its best (first) rank.

Deliberately not pinned: how ranks are numbered after a duplicate (`["a", "a", "b"]`: is b rank 2 or
3). Tests only put duplicates after every id whose rank they could shift.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.engine.retrieval.fuse import rrf

IDS = st.text(alphabet="abcdefghij", min_size=1, max_size=3)
RANKED = st.lists(IDS, max_size=12, unique=True)


def _ids(result: list[tuple[str, float]]) -> list[str]:
    return [cid for cid, _ in result]


# --------------------------------------------------------------------------- exact scores


def test_exact_scores_for_hand_built_lists() -> None:
    """a: 1/61 only; b: 1/62 + 1/61 (both lists); c: 1/63; d: 1/62. Order: b, a, d, c."""
    result = rrf([["a", "b", "c"], ["b", "d"]])

    assert _ids(result) == ["b", "a", "d", "c"]
    scores = dict(result)
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["c"] == pytest.approx(1 / 63)
    assert scores["d"] == pytest.approx(1 / 62)


def test_returns_a_list_of_id_score_pairs() -> None:
    """The shape is list[tuple[str, float]]."""
    result = rrf([["a", "b"]])
    assert isinstance(result, list)
    assert all(isinstance(item, tuple) and len(item) == 2 for item in result)
    assert all(isinstance(cid, str) and isinstance(score, float) for cid, score in result)


def test_an_id_in_both_lists_beats_ids_in_one_even_at_rank_one() -> None:
    """y is rank 2 in both lists (2/62); x and z are rank 1 in one list each (1/61): y wins."""
    result = rrf([["x", "y"], ["z", "y"]])
    assert result[0][0] == "y"
    assert result[0][1] == pytest.approx(2 / 62)
    assert dict(result)["x"] == pytest.approx(1 / 61)
    assert dict(result)["z"] == pytest.approx(1 / 61)
    assert result[0][1] > dict(result)["x"]


def test_custom_k_changes_the_scores() -> None:
    """k is the constant in 1 / (k + rank): k=1 gives 1/2, 1/3; k=10 gives 1/11."""
    assert rrf([["a", "b"]], k=1) == [("a", pytest.approx(1 / 2)), ("b", pytest.approx(1 / 3))]
    assert rrf([["a"]], k=10)[0][1] == pytest.approx(1 / 11)
    assert rrf([["a"]])[0][1] == pytest.approx(1 / 61)  # default k = 60


def test_three_lists_sum_all_contributions() -> None:
    """Scores add over every list the id appears in."""
    result = dict(rrf([["a", "b"], ["b", "a"], ["a"]]))
    assert result["a"] == pytest.approx(1 / 61 + 1 / 62 + 1 / 61)
    assert result["b"] == pytest.approx(1 / 62 + 1 / 61)


def test_long_lists_use_the_position_as_rank() -> None:
    """Rank is the 1-based position: item at index 999 contributes 1/(60+1000)."""
    ids = [f"id{i:04d}" for i in range(1000)]
    scores = dict(rrf([ids]))
    assert scores["id0000"] == pytest.approx(1 / 61)
    assert scores["id0999"] == pytest.approx(1 / 1060)
    assert _ids(rrf([ids])) == ids


# --------------------------------------------------------------------------- ties and empties


def test_ties_are_broken_by_chunk_id() -> None:
    """Equal scores sort by chunk_id ascending, whatever order the lists present them in."""
    assert _ids(rrf([["b"], ["a"]])) == ["a", "b"]
    assert _ids(rrf([["c"], ["b"], ["a"]])) == ["a", "b", "c"]
    # a and b swap places across the two lists: identical sums, so id order decides
    assert _ids(rrf([["b", "a"], ["a", "b"]])) == ["a", "b"]
    assert _ids(rrf([["a", "b"], ["b", "a"]])) == ["a", "b"]


def test_ties_use_plain_string_order() -> None:
    """The tie-break is string comparison ('10' < '9', uppercase before lowercase)."""
    assert _ids(rrf([["9"], ["10"]])) == ["10", "9"]
    assert _ids(rrf([["b"], ["B"]])) == ["B", "b"]


def test_empty_inputs() -> None:
    """No lists, or only empty lists, fuse to an empty result; one empty list is ignored."""
    assert rrf([]) == []
    assert rrf([[], []]) == []
    assert rrf([["a"], []]) == [("a", pytest.approx(1 / 61))]
    assert rrf([[], ["a", "b"]]) == [("a", pytest.approx(1 / 61)), ("b", pytest.approx(1 / 62))]


def test_duplicates_inside_one_list_count_once_at_their_best_rank() -> None:
    """['a', 'b', 'a']: a scores 1/61 (rank 1 only), not 1/61 + 1/63; b keeps rank 2."""
    result = dict(rrf([["a", "b", "a"]]))
    assert result["a"] == pytest.approx(1 / 61)
    assert result["b"] == pytest.approx(1 / 62)
    assert len(result) == 2


def test_duplicates_in_one_list_do_not_inflate_against_a_second_list() -> None:
    """a repeated three times in list 1 still adds only one 1/61 term to its list-2 contribution."""
    result = dict(rrf([["a", "b", "a", "a"], ["b", "a"]]))
    assert result["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert result["b"] == pytest.approx(1 / 62 + 1 / 61)


def test_inputs_are_not_mutated_and_tuples_are_accepted() -> None:
    """rrf reads its arguments only; any Sequence of Sequences works."""
    lists = [["a", "b", "a"], ["b"]]
    snapshot = [list(x) for x in lists]
    rrf(lists)
    assert lists == snapshot
    assert _ids(rrf((("a", "b"), ("b",)))) == ["b", "a"]


# --------------------------------------------------------------------------- properties


@given(st.lists(RANKED, max_size=4))
@settings(max_examples=200)
def test_property_result_is_the_union_each_id_once(lists: list[list[str]]) -> None:
    """The output ids are exactly the union of the input ids, each once."""
    result = rrf(lists)
    union = {cid for lst in lists for cid in lst}
    assert len(_ids(result)) == len(set(_ids(result)))
    assert set(_ids(result)) == union


@given(st.lists(RANKED, max_size=4))
@settings(max_examples=200)
def test_property_sorted_best_first_with_id_tiebreak(lists: list[list[str]]) -> None:
    """Scores never increase down the list and equal scores are in ascending id order."""
    result = rrf(lists)
    for (id_a, s_a), (id_b, s_b) in zip(result, result[1:], strict=False):
        assert s_a >= s_b
        if s_a == s_b:
            assert id_a < id_b


@given(st.lists(RANKED, max_size=4), st.integers(min_value=1, max_value=200))
@settings(max_examples=200)
def test_property_score_matches_the_formula(lists: list[list[str]], k: int) -> None:
    """Every score equals sum(1 / (k + rank)) over the lists (ids are unique within a list here)."""
    result = dict(rrf(lists, k=k))
    for cid, score in result.items():
        expected = sum(1 / (k + lst.index(cid) + 1) for lst in lists if cid in lst)
        assert score == pytest.approx(expected, rel=1e-9)


@given(RANKED, RANKED)
@settings(max_examples=200)
def test_property_list_order_does_not_matter_for_two_lists(a: list[str], b: list[str]) -> None:
    """Fusing (a, b) or (b, a) gives the identical result (two-term float addition commutes)."""
    assert rrf([a, b]) == rrf([b, a])


@given(st.lists(IDS, max_size=15))
@settings(max_examples=200)
def test_property_trailing_duplicates_change_nothing(ids: list[str]) -> None:
    """Appending repeats of ids already listed (after all of them) changes nothing."""
    unique = list(dict.fromkeys(ids))
    assert rrf([unique + unique]) == rrf([unique])


@given(RANKED)
@settings(max_examples=100)
def test_property_a_single_list_keeps_its_order(ids: list[str]) -> None:
    """Fusing one list returns it unchanged in order (higher rank, strictly higher score)."""
    assert _ids(rrf([ids])) == ids
