"""Known section-detection defects (registry: tests/fixtures/known_defects/section_failures.json).

Each case is a real S&P 500 10-K, FY2020-2025, that the CURRENT parser misses a required item on.
Two cases were fixed (Allstate's fused Item 1 heading, Everest's Item 7A displaced by a
cross-reference fragment) and are now ordinary regression tests. Every other item is listed under
`needs_gold_label` and not asserted: no body heading says where it starts, so its truth needs a
hand label first. Once a label confirms an item, add it to the registry's `expected_found` with its
evidence and mark its test `xfail(strict=True)` until the parser finds it.

The filings are not committed (several MB each). `python -m eval.blast_radius` caches them under
`data/fixture_candidates/blast/`; without the cache these tests skip.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from pipelines.parsing.html_blocks import html_to_blocks
from pipelines.parsing.sections import detect_sections

REPO = Path(__file__).resolve().parents[3]
REGISTRY = json.loads((REPO / "tests/fixtures/known_defects/section_failures.json").read_text())
CASES: list[dict[str, Any]] = REGISTRY["cases"]
CACHE = REPO / "data/fixture_candidates/blast"
REQUIRED = {"1", "1A", "7", "7A", "8"}
FIXED: list[dict[str, Any]] = REGISTRY["fixed"]


def cached(case: dict[str, Any]) -> Path:
    return CACHE / f"{case['ticker']}_{case['accession']}.htm.gz"


def test_the_registry_is_consistent_with_the_blast_radius_measurement():
    blast = json.loads((REPO / "docs/milestones/M02-blast-radius.json").read_text())
    failing = {f["accession"]: f for f in blast["filings"] if f["missing_required"]}

    assert {c["accession"] for c in CASES} == set(failing)
    for c in CASES:
        assert c["missing_required_today"] == failing[c["accession"]]["missing_required"]
        assert set(c["expected_found"]) | set(c["needs_gold_label"]) == set(
            c["missing_required_today"]
        )
        assert not set(c["expected_found"]) & set(c["needs_gold_label"])
        assert set(c["missing_required_today"]) <= REQUIRED
        assert set(c["evidence"]) == set(c["expected_found"]), (
            "every asserted item needs its evidence"
        )


def test_the_two_fixed_cases_are_recorded_with_what_was_wrong_and_what_fixed_it():
    assert [(f["ticker"], f["item"]) for f in FIXED] == [("ALL-PB", "1"), ("EG", "7A")]
    for f in FIXED:
        assert f["was"] and f["fix"].startswith("pipelines/parsing/sections.py")
    assert not {f["accession"] for f in FIXED} & {c["accession"] for c in CASES} - {
        "0001095073-23-000007"
    }


def test_the_two_failures_found_before_the_survey_are_registered_but_not_asserted():
    by_ticker = {c["ticker"]: c for c in CASES}

    assert by_ticker["PHM"]["missing_required_today"] == ["1"]
    assert by_ticker["NEMCL"]["missing_required_today"] == ["7"]
    # neither filing writes a recognisable body heading, so where the item starts is a label
    assert by_ticker["PHM"]["expected_found"] == [] and by_ticker["NEMCL"]["expected_found"] == []
    assert by_ticker["NEMCL"]["miss_causes"] == {"7": "toc_or_index_only"}


def test_every_registered_case_says_what_is_missing_and_why():
    for c in CASES:
        assert c["accession"].count("-") == 2 and c["url"].startswith("https://www.sec.gov/")
        assert c["missing_required_today"] and set(c["miss_causes"]) == set(
            c["missing_required_today"]
        )
        if c["needs_gold_label"]:
            assert c["note"], f"{c['accession']} needs a note on why it is not asserted"


@pytest.mark.parametrize("fix", FIXED, ids=lambda f: f"{f['ticker']}-{f['item']}")
def test_a_fixed_defect_stays_fixed_on_the_real_filing(fix: dict[str, Any]):
    path = cached(fix)
    if not path.is_file():
        pytest.skip(f"not cached: run `python -m eval.blast_radius` ({path.name})")

    found = {
        s.code for s in detect_sections(html_to_blocks(gzip.decompress(path.read_bytes())), "10-K")
    }

    assert fix["item"] in found


def test_the_everest_fragment_is_no_longer_taken_for_item_8():
    """The old detection of Everest's Item 8 was the cross-reference fragment 'ITEM 8,'."""
    fix = next(f for f in FIXED if f["ticker"] == "EG")
    path = cached(fix)
    if not path.is_file():
        pytest.skip(f"not cached: run `python -m eval.blast_radius` ({path.name})")
    blocks = html_to_blocks(gzip.decompress(path.read_bytes()))

    sections = detect_sections(blocks, "10-K")

    assert all(blocks[s.heading_index].text.strip() != "ITEM 8," for s in sections)
