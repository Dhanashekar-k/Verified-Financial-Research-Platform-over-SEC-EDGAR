"""Invariants over the owner's REAL fixture filings in ``tests/fixtures/filings/``.

The three fixtures (a 10-K with a hyperlinked TOC, an older 10-K without ids, a 10-Q) are the
owner's to supply (M02 section 5); Claude does not fabricate them. Name each file
``<anything>.htm`` or ``.html`` and put a sidecar ``<same name>.json`` next to it with
``{"form": "10-K", "accession": "...", "cik": 0, "period_of_report": "YYYY-MM-DD",
"fye_mmdd": "MMDD"}``. Until the files exist these tests are skipped and the M02 report says
NOT MEASURED for fixture-based checks.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from pipelines.common.fiscal_ref import LabelOffset
from pipelines.parsing.document import FilingMeta, parse_document
from pipelines.parsing.tokens import get_tokenizer

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "filings"
FILES = sorted([*FIXTURES.glob("*.htm"), *FIXTURES.glob("*.html")]) if FIXTURES.exists() else []
CORE_10K = {"1", "1A", "7", "8"}
CORE_10Q = {"P1-1", "P1-2"}

pytestmark = pytest.mark.skipif(
    not FILES, reason="NOT MEASURED: no real fixture filings in tests/fixtures/filings/ yet"
)


def load(path: Path):
    side = json.loads(path.with_suffix(".json").read_text())
    meta = FilingMeta(
        accession=side["accession"],
        cik=side["cik"],
        form=side["form"],
        filed=date.fromisoformat(side.get("filed", side["period_of_report"])),
        period_of_report=date.fromisoformat(side["period_of_report"]),
        primary_doc_url=f"https://www.sec.gov/Archives/edgar/data/{side['cik']}/x/{path.name}",
        html_s3_uri=str(path),
        fye_mmdd=side["fye_mmdd"],
        label_offset=LabelOffset(0, True),
    )
    return meta, parse_document(path.read_bytes(), meta)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_a_real_filing_parses_with_its_core_sections_and_clean_chunks(path: Path):
    meta, (filing, chunks) = load(path)
    core = CORE_10K if meta.form.startswith("10-K") else CORE_10Q

    assert filing.parse_status == "ok", filing.parse_error
    assert core <= {c.section for c in chunks}
    tok = get_tokenizer()
    for c in chunks:
        assert c.n_tokens == tok.count(c.text) <= 400
        assert "ix:header" not in c.text and "xbrli:" not in c.text
        assert c.chunk_id.startswith(meta.accession + "#")
    assert len({c.chunk_id for c in chunks}) == len(chunks)
