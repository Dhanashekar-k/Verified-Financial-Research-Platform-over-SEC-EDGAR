"""Berkshire Hathaway 10-Qs: Item 2 (P1-2) must be found, starting at the MD&A heading.

Registry: tests/fixtures/known_defects/berkshire_10q_item2.json (21 real filings, FY2020-2026).
Before the fix all 21 parsed `partial` ("missing sections: P1-2"): the MD&A heading is repeated as
the running header of every MD&A page, and the noise filter dropped all 18 copies, the real heading
included. Each case pins the missing item, where its section now starts (heading text and first
body block) and the filing's status.

The filings are not committed (several MB each); they are the local fetch store `data/raw/filings`.
Without it these tests skip, like the other real-filing regression tests (`test_known_defects.py`);
the synthetic cases in `test_html_blocks.py` and `test_document.py` always run.
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
REGISTRY: dict[str, Any] = json.loads(
    (REPO / "tests/fixtures/known_defects/berkshire_10q_item2.json").read_text()
)
STORE = REPO / "data/raw/filings" / str(REGISTRY["cik"])


def path(case: dict[str, Any]) -> Path:
    return STORE / f"{case['accession']}.htm.gz"


def test_the_registry_lists_the_21_filings_the_measurement_found():
    assert len(REGISTRY["cases"]) == 21
    assert len({c["accession"] for c in REGISTRY["cases"]}) == 21
    assert REGISTRY["missing_item_before"] == "P1-2"


@pytest.mark.parametrize("case", REGISTRY["cases"], ids=lambda c: c["accession"])
def test_item_2_is_found_and_starts_at_the_mdna_heading(case: dict[str, Any]):
    if not path(case).is_file():
        pytest.skip("NOT MEASURED: the real filing is not in data/raw/filings")
    blocks = html_to_blocks(gzip.decompress(path(case).read_bytes()))

    sections = {s.code: s for s in detect_sections(blocks, REGISTRY["form"])}

    assert REGISTRY["missing_item_before"] in sections
    mda = sections["P1-2"]
    assert mda.heading_text.startswith(REGISTRY["expected"]["heading_prefix"])
    first_body = next(b.text for b in blocks[mda.start : mda.end])
    assert first_body == REGISTRY["expected"]["first_body_block"]
    # the section is the whole MD&A (tens of thousands of characters), not a page of it
    size = sum(len(b.text) for b in blocks[mda.start : mda.end])
    assert size == case["p1_2_chars"]
    # and Item 1 ends where Item 2 begins, so no MD&A text stays inside Financial Statements
    assert sections["P1-1"].end == mda.heading_index
