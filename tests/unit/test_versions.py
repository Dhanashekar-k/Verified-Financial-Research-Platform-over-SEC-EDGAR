"""Unit tests for `select_versions_ref` (M01 section 3.5, "Dedup and restatements").

Target: `pipelines/common/fiscal_ref.py::select_versions_ref`. No Spark. The case data and the
hand-derived expected rows live in `tests/fiscal_cases.py` (`VERSION_SCENARIOS`); each named
scenario is exercised by its own test below so the docstring says what is being pinned.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pipelines.common.fiscal_ref import FactVersion, SelectedVersion, select_versions_ref
from tests.fiscal_cases import VERSION_SCENARIOS, VersionScenario, sorted_version_tuples

SCENARIOS = {s.name: s for s in VERSION_SCENARIOS}


def as_tuples(
    out: list[SelectedVersion],
) -> list[tuple[int, str, Decimal | None, bool, bool, int, bool]]:
    return sorted_version_tuples(
        [
            (
                r.version.fact_key,
                r.version.accession,
                r.version.value,
                r.is_latest,
                r.is_original,
                r.n_versions,
                r.is_restated,
            )
            for r in out
        ]
    )  # type: ignore[arg-type]


def run_scenario(name: str) -> None:
    sc: VersionScenario = SCENARIOS[name]
    out = select_versions_ref(list(sc.versions))
    assert as_tuples(out) == sorted_version_tuples([e.as_tuple() for e in sc.expect])
    # An output row is one of the input versions, not a synthesised copy.
    assert all(r.version in sc.versions for r in out)


def test_single_version_is_one_row_latest_and_original_not_restated() -> None:
    """(1) One reported version: ONE row, is_latest and is_original both true, n_versions 1,
    is_restated False.
    """
    run_scenario("single_version")


def test_same_value_reported_again_collapses_to_one_row_with_latest_accession() -> None:
    """(2) A value re-reported unchanged as a comparative in the next 10-K: latest = the later
    filing, original = the first filing (filing_period == period_end), same value -> ONE row
    carrying the LATEST accession, both flags true, n_versions 2, is_restated False.
    """
    run_scenario("same_value_reported_again_as_comparative")


def test_restated_value_gives_a_latest_row_and_an_original_row() -> None:
    """(3) 100 restated to 90: TWO rows. Latest (90) is_latest only; original (100) is_original
    only; both carry n_versions 2 and is_restated True.
    """
    run_scenario("restated_100_to_90")


def test_ten_k_amendment_with_a_different_value() -> None:
    """(4) A 10-K then a 10-K/A for the same period (both filing_period == period_end), later
    filed, different value: original = the 10-K, latest = the 10-K/A.
    """
    run_scenario("ten_k_then_ten_k_a_with_different_value")


def test_ten_k_amendment_with_the_same_value_is_one_row() -> None:
    """(4b) The 10-K/A repeats the 10-K's value: one row carrying the 10-K/A's accession (it is
    the latest), is_restated False, n_versions 2.
    """
    run_scenario("ten_k_then_ten_k_a_with_same_value")


def test_original_falls_back_to_earliest_filed_when_no_filing_period_matches() -> None:
    """(5) No version has filing_period == period_end: original = the earliest filed overall,
    regardless of the order the rows arrive in.
    """
    run_scenario("no_filing_period_matches_original_is_earliest_filed")


def test_original_is_the_matching_filing_even_if_a_non_match_was_filed_earlier() -> None:
    """(5b) Adversarial: a non-matching version filed earlier (value 80) must not beat the
    version whose filing_period == period_end (value 100). If it did, original 80 != latest 100
    would yield two rows instead of one.
    """
    run_scenario("matching_filing_period_beats_an_earlier_filed_non_match")


def test_latest_tie_on_filed_takes_the_max_accession() -> None:
    """(6) Two versions filed the same day: the latest is the one with the max accession."""
    run_scenario("latest_tie_on_filed_takes_max_accession")


def test_original_tie_on_filed_takes_the_min_accession() -> None:
    """(6) Two matching versions filed the same day: the original is the one with the min
    accession.
    """
    run_scenario("original_tie_on_filed_takes_min_accession")


def test_all_versions_filed_same_day_latest_max_original_min_accession() -> None:
    """(6) Both tie-breaks at once with no filing_period match: latest = max accession,
    original = min accession.
    """
    run_scenario("all_versions_filed_same_day_no_match")


def test_restated_then_restored_value_is_one_row_but_restated() -> None:
    """(7) 100 -> 90 -> 100: original value == latest value, so ONE row with both flags and the
    latest accession, yet n_versions 3 and is_restated True (two distinct values were reported).
    """
    run_scenario("restated_then_restored_100_90_100")


@pytest.mark.parametrize("seed", [None, 1, 2, 3])
def test_independent_fact_keys_do_not_interact_and_input_order_is_irrelevant(
    seed: int | None,
) -> None:
    """(8) All scenarios (each with its own fact_key) in one input: the output is the union of
    the per-scenario outputs, for the given order, reversed (seed None) and three shuffles.
    """
    versions = [v for s in VERSION_SCENARIOS for v in s.versions]
    expected = sorted_version_tuples([e.as_tuple() for s in VERSION_SCENARIOS for e in s.expect])
    if seed is None:
        versions = versions[::-1]
    else:
        random.Random(seed).shuffle(versions)
    assert as_tuples(select_versions_ref(versions)) == expected


# --------------------------------------------------------------------------------------
# Hypothesis (9)
# --------------------------------------------------------------------------------------

_VALUES = [Decimal("100.0000"), Decimal("90.0000"), Decimal("110.5000")]
_PERIOD_END = date(2022, 12, 31)
_OTHER_PERIOD = date(2023, 3, 31)


@st.composite
def version_inputs(draw: st.DrawFn) -> list[FactVersion]:
    """1-3 fact_keys with 1-5 versions each: filed dates collide often (ties), accessions are
    unique and unrelated to filing order, filing_period equals period_end or not, and values
    repeat so that restated and never-restated groups both occur. Input order is shuffled.
    """
    versions: list[FactVersion] = []
    for key in range(1, draw(st.integers(1, 3)) + 1):
        n = draw(st.integers(1, 5))
        order = draw(st.permutations(range(n)))
        for i in range(n):
            form = draw(st.sampled_from(["10-K", "10-K/A", "10-Q"]))
            versions.append(
                FactVersion(
                    fact_key=key,
                    fact_id=key * 100 + i,
                    period_end=_PERIOD_END,
                    value=draw(st.sampled_from(_VALUES)),
                    accession=f"{key:010d}-23-{order[i]:06d}",
                    form=form,
                    filed=date(2023, 2, 1) + timedelta(days=draw(st.integers(0, 3))),
                    is_amendment=form.endswith("/A"),
                    filing_period=draw(st.sampled_from([_PERIOD_END, _OTHER_PERIOD])),
                )
            )
    return draw(st.permutations(versions))


@settings(max_examples=300)
@given(versions=version_inputs())
def test_version_selection_invariants_property(versions: list[FactVersion]) -> None:
    """(9) Per fact_key: exactly one is_latest row and exactly one is_original row; 1 or 2 rows;
    n_versions == the group's input size; is_restated == (more than one distinct value); the
    latest row is the max (filed, accession); the original is the min (filed, accession) among
    filing_period == period_end versions (or among all if none); one row <=> same value.
    """
    out = select_versions_ref(versions)
    assert all(r.version in versions for r in out)
    for key in {v.fact_key for v in versions}:
        group = [v for v in versions if v.fact_key == key]
        rows = [r for r in out if r.version.fact_key == key]
        assert 1 <= len(rows) <= 2
        assert sum(r.is_latest for r in rows) == 1
        assert sum(r.is_original for r in rows) == 1
        assert all(r.n_versions == len(group) for r in rows)
        assert all(r.is_restated == (len({v.value for v in group}) > 1) for r in rows)

        latest = max(group, key=lambda v: (v.filed, v.accession))
        eligible = [v for v in group if v.filing_period == v.period_end] or group
        original = min(eligible, key=lambda v: (v.filed, v.accession))
        latest_row = next(r for r in rows if r.is_latest)
        original_row = next(r for r in rows if r.is_original)
        assert (latest_row.version.filed, latest_row.version.accession) == (
            latest.filed,
            latest.accession,
        )
        assert original_row.version.value == original.value
        if latest.value == original.value:
            assert len(rows) == 1
            assert latest_row is original_row
        else:
            assert len(rows) == 2
            assert latest_row is not original_row
            assert not latest_row.is_original and not original_row.is_latest
            assert (original_row.version.filed, original_row.version.accession) == (
                original.filed,
                original.accession,
            )


@settings(max_examples=100)
@given(versions=version_inputs(), data=st.data())
def test_version_selection_ignores_input_order_property(
    versions: list[FactVersion], data: st.DataObject
) -> None:
    """(8, property) Any permutation of the input gives the same set of output rows."""

    def signature(rows: list[SelectedVersion]) -> Counter[tuple[object, ...]]:
        return Counter(
            (r.version.fact_id, r.is_latest, r.is_original, r.n_versions, r.is_restated)
            for r in rows
        )

    shuffled = data.draw(st.permutations(versions))
    assert signature(select_versions_ref(shuffled)) == signature(select_versions_ref(versions))
