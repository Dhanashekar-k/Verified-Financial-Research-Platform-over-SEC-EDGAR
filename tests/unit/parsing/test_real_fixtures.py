"""Invariants over the REAL fixture filings in ``tests/fixtures/filings/``.

The fixtures are real EDGAR primary documents (never edited) chosen by the owner: a 10-K with an
opaque-id anchor TOC (Apple FY2024), a 10-K with a named-id anchor TOC and a non-zero label offset
(Ulta FY2024: its fiscal year ends about 31 January and is named for the year it began), and a
10-Q (Apple Q3 FY2026). Each ``<name>.htm`` has a sidecar ``<name>.json`` with
``form, accession, cik, filed, period_of_report, fye_mmdd, label_offset, label_offset_is_constant,
expected_fiscal_year, expected_fiscal_period``. The sidecar is checked against the document's own
``dei`` tags (period end, fiscal year and period focus), so a wrong sidecar fails here instead of
quietly weakening the fiscal-label invariant. With no fixtures the tests skip and the M02 report
says NOT MEASURED.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from selectolax.lexbor import LexborHTMLParser

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


def sidecar(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.with_suffix(".json").read_text())
    return data


def load(path: Path):
    side = sidecar(path)
    meta = FilingMeta(
        accession=side["accession"],
        cik=side["cik"],
        form=side["form"],
        filed=date.fromisoformat(side.get("filed", side["period_of_report"])),
        period_of_report=date.fromisoformat(side["period_of_report"]),
        primary_doc_url=f"https://www.sec.gov/Archives/edgar/data/{side['cik']}/x/{path.name}",
        html_s3_uri=str(path),
        fye_mmdd=side["fye_mmdd"],
        label_offset=LabelOffset(
            side.get("label_offset", 0), side.get("label_offset_is_constant", True)
        ),
    )
    return meta, parse_document(path.read_bytes(), meta)


def dei_tags(path: Path) -> dict[str, str]:
    """The cover-page `dei:*` facts of an inline-XBRL filing (first occurrence of each)."""
    tree = LexborHTMLParser(path.read_bytes().decode("utf-8", "replace"))
    found: dict[str, str] = {}
    for node in tree.css("ix\\:nonnumeric"):
        name = node.attributes.get("name")
        if name and name.startswith("dei:") and name not in found:
            found[name] = " ".join(node.text(strip=True).split())
    return found


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


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_sidecar_agrees_with_the_documents_own_dei_cover_tags(path: Path):
    """The sidecar's period and expected labels are not taken on trust: the filing says them too."""
    side, dei = sidecar(path), dei_tags(path)

    period_text = dei["dei:DocumentPeriodEndDate"]
    assert (
        datetime.strptime(period_text, "%B %d, %Y").date().isoformat() == side["period_of_report"]
    )
    assert dei["dei:DocumentType"] == side["form"]
    assert int(dei["dei:EntityCentralIndexKey"]) == side["cik"]
    assert int(dei["dei:DocumentFiscalYearFocus"]) == side["expected_fiscal_year"]
    assert dei["dei:DocumentFiscalPeriodFocus"] == side["expected_fiscal_period"]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_filings_fiscal_labels_equal_the_companys_own_labels(path: Path):
    """ADR-023 on real filings: fiscal_ref's rules, with the company's label offset, give the
    fiscal year and period the filing states in its dei cover tags (Ulta's offset is -1)."""
    _, (filing, _) = load(path)
    side = sidecar(path)

    assert (filing.fiscal_year, filing.fiscal_period) == (
        side["expected_fiscal_year"],
        side["expected_fiscal_period"],
    )
    assert filing.dq_flags == []


def test_the_fixtures_cover_a_non_zero_label_offset_and_a_10q():
    sides = [sidecar(p) for p in FILES]

    assert any(s["label_offset"] != 0 for s in sides), "a zero offset everywhere proves little"
    assert any(s["form"].startswith("10-Q") for s in sides)
    assert any(s["toc"]["id_style"] == "named" for s in sides)
    assert any(s["toc"]["id_style"] == "opaque" for s in sides)
