"""Unit tests for `select_versions_ref` (M01 section 3.5, "Dedup and restatements").

Target: `pipelines/common/fiscal_ref.py::select_versions_ref`. No Spark. The case data and the
hand-derived expected rows live in `tests/fiscal_cases.py` (`VERSION_SCENARIOS`); each named
scenario is exercised by its own test below so the docstring says what is being pinned.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from pipelines.common.fiscal_ref import SelectedVersion, select_versions_ref
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
