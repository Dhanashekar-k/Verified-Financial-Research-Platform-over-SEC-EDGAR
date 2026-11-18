"""Known section-detection defects (registry: tests/fixtures/known_defects/section_failures.json).

Each case is a real S&P 500 10-K, FY2020-2025, that the CURRENT parser misses a required item on.
For items where a heading-like block exists in the text (so the item is certainly present), the
test asserts the parser FINDS it, marked `xfail(strict=True)`: today it fails as expected, and the
fix that makes it pass turns the test into an XPASS failure until the marker is removed. Items
where the filing never writes "Item X" are listed under `needs_gold_label` and not asserted: their
truth needs a hand label first.

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
PAIRS = [(c, item) for c in CASES for item in c["expected_found"]]


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


@pytest.mark.xfail(
    strict=True, reason="known defect: a heading that is in the filing is not detected"
)
@pytest.mark.parametrize(
    ("case", "item"), PAIRS, ids=lambda v: v if isinstance(v, str) else v["ticker"]
)
def test_the_parser_finds_the_item(case: dict[str, Any], item: str):
    path = cached(case)
    if not path.is_file():
        pytest.skip(f"not cached: run `python -m eval.blast_radius` ({path.name})")
    raw = gzip.decompress(path.read_bytes())
    found = {s.code for s in detect_sections(html_to_blocks(raw), "10-K")}

    assert item in found
