"""Rollins 10-K FY2020: "Item 1.A." (dot before the letter) must be item 1A, not a second item 1.

Before the fix `sections._ITEM` read "Item 1.A. Risk Factors" as item 1 with the title "A. Risk
Factors": the real Item 1 chunk 0 was lost to the risk-factor text and 1A was "section not found"
(section-gold rows ROL 1 and ROL 1A, `docs/milestones/M02-sections-eval.json`, ADR-029).

The filing is not committed (2.9 MB); it is in the blast-radius cache
`data/fixture_candidates/blast` (gitignored). Without it the real-filing test skips, like the
other real-filing regression tests; the synthetic cases in `test_sections.py` always run.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from pipelines.parsing.html_blocks import html_to_blocks
from pipelines.parsing.sections import detect_sections

REPO = Path(__file__).resolve().parents[3]
FILING = REPO / "data/fixture_candidates/blast/ROL_0001171200-21-000076.htm.gz"

pytestmark = pytest.mark.skipif(not FILING.exists(), reason="blast-radius cache not present")


def test_rollins_item_1a_and_1b_are_found_and_item_1_starts_at_its_own_heading():
    blocks = html_to_blocks(gzip.decompress(FILING.read_bytes()))

    sections = {s.code: s for s in detect_sections(blocks, "10-K")}

    assert {"1", "1A", "1B", "7", "7A", "8"} <= set(sections)
    assert sections["1"].end <= sections["1A"].heading_index + 1  # item 1 ends where 1A begins
    risk = " ".join(b.text for b in blocks[sections["1A"].start : sections["1A"].start + 3])
    assert "You should" in risk and "risk factors" in risk
    business = " ".join(b.text for b in blocks[sections["1"].start : sections["1"].start + 4])
    assert "Rollins" in business
