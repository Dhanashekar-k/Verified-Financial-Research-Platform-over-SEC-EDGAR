"""Tests for ``pipelines/parsing/document.py``: bytes + metadata -> one filing row + chunk rows.

The HTML is a small synthetic 10-K (hyperlinked TOC, body, a data table); it is NOT a substitute
for the owner's real fixture filings (see ``test_real_fixtures.py``).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

import pipelines.parsing.document as document
from pipelines.common.fiscal_ref import LabelOffset
from pipelines.parsing.document import ChunkRow, FilingMeta, parse_document
from pipelines.parsing.tokens import get_tokenizer

URL = "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"
TOK = get_tokenizer()


def para(n: int, topic: str) -> str:
    return "".join(
        f"<p>{topic} sentence number {i} says that revenue and margins moved because of "
        f"demand, supply chains, currency and pricing across segments and regions.</p>"
        for i in range(n)
    )


HTML_10K = f"""<html><head><title>10-K</title></head><body>
<ix:header><ix:hidden>HIDDEN-XBRL-0000320193</ix:hidden></ix:header>
<div>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</div>
<table>
<tr><td><a href="#b1">Item 1.</a></td><td>Business</td><td>1</td></tr>
<tr><td><a href="#b1a">Item 1A.</a></td><td>Risk Factors</td><td>5</td></tr>
<tr><td><a href="#b7">Item 7.</a></td><td>MD&amp;A</td><td>20</td></tr>
<tr><td><a href="#b8">Item 8.</a></td><td>Financial Statements</td><td>30</td></tr>
</table>
<div id="b1">Item 1. Business</div>{para(30, "Business")}
<div id="b1a">Item 1A. Risk Factors</div>{para(60, "Risk")}
<div id="b7">Item 7. Management's Discussion and Analysis</div>{para(40, "Liquidity")}
<div id="b8">Item 8. Financial Statements and Supplementary Data</div>
<p>The following table presents results.</p>
<table>
<tr><td>In millions</td><td>2023</td><td>2022</td></tr>
<tr><td>Net sales</td><td>$</td><td>383,285</td><td>$</td><td>394,328</td></tr>
<tr><td>Net income</td><td>$</td><td>96,995</td><td>$</td><td>99,803</td></tr>
</table>
{para(5, "Notes")}
<div>SIGNATURES</div><p>Pursuant to the requirements, the registrant signed.</p>
</body></html>"""


def meta(**kw) -> FilingMeta:
    base = {
        "accession": "0000320193-23-000106",
        "cik": 320193,
        "form": "10-K",
        "filed": date(2023, 11, 3),
        "accepted_at": datetime(2023, 11, 2, 18, 8, 27),
        "period_of_report": date(2023, 9, 30),
        "primary_doc_url": URL,
        "html_s3_uri": "s3://lake/raw/filings/320193/0000320193-23-000106.htm.gz",
        "fye_mmdd": "0930",
        "label_offset": LabelOffset(0, True),
    }
    return FilingMeta(**(base | kw))


def parse(html: str = HTML_10K, **kw):
    return parse_document(html.encode("utf-8"), meta(**kw))


def test_the_filing_row_carries_metadata_fiscal_labels_and_counts():
    filing, chunks = parse()

    assert filing.accession == "0000320193-23-000106" and filing.cik == 320193
    assert (filing.fiscal_year, filing.fiscal_period) == (2023, "FY")
    assert filing.dq_flags == []
    assert filing.parse_status == "ok" and filing.parse_error is None
    assert filing.n_sections == 4 and filing.n_chunks == len(chunks) > 4
    assert filing.doc_source == "edgar_html"
    assert filing.primary_doc_url == URL and filing.html_s3_uri.startswith("s3://lake/raw")


def test_chunk_ids_follow_accession_section_index_and_are_dense_per_section():
    _, chunks = parse()

    by_section: dict[str, list[ChunkRow]] = {}
    for c in chunks:
        by_section.setdefault(c.section, []).append(c)
    assert list(by_section) == ["1", "1A", "7", "8"]
    for section, cs in by_section.items():
        assert [c.chunk_index for c in cs] == list(range(len(cs)))
        assert [c.chunk_id for c in cs] == [
            f"0000320193-23-000106#{section}#{i:04d}" for i in range(len(cs))
        ]


def test_every_chunk_is_in_one_section_within_400_tokens_with_the_contract_columns():
    _, chunks = parse()

    for c in chunks:
        assert c.n_tokens == TOK.count(c.text) <= 400
        assert (c.cik, c.form, c.fiscal_year, c.fiscal_period) == (320193, "10-K", 2023, "FY")
        assert c.doc_source == "edgar_html" and c.corpus_version == 1 and c.page is None
        assert c.source_url.startswith(URL + "#")
    titles = {c.section: c.section_title for c in chunks}
    assert titles["1A"] == "Risk Factors"


def test_toc_text_hidden_xbrl_and_the_cover_page_never_reach_a_chunk():
    _, chunks = parse()
    everything = " ".join(c.text for c in chunks)

    assert "HIDDEN-XBRL" not in everything
    assert "UNITED STATES SECURITIES" not in everything
    assert "Pursuant to the requirements" not in everything  # after SIGNATURES
    assert "Business | 1" not in everything


def test_a_section_first_chunk_cites_the_heading_id_and_later_chunks_use_a_text_fragment():
    _, chunks = parse()

    first = next(c for c in chunks if c.section == "1A" and c.chunk_index == 0)
    later = next(c for c in chunks if c.section == "1A" and c.chunk_index == 1)
    assert (first.anchor, first.source_url) == ("b1a", URL + "#b1a")
    assert later.anchor is None and "#:~:text=" in later.source_url


def test_data_tables_are_their_own_chunks_with_the_header_row_and_is_table():
    _, chunks = parse()

    tables = [c for c in chunks if c.is_table]
    assert len(tables) == 1
    assert tables[0].section == "8"
    assert tables[0].text.splitlines() == [
        "In millions | 2023 | 2022",
        "Net sales | $383,285 | $394,328",
        "Net income | $96,995 | $99,803",
    ]
    assert tables[0].source_url.startswith(URL + "#:~:text=In%20millions")


def test_a_filing_with_no_item_headings_has_status_no_sections_and_no_chunks():
    filing, chunks = parse("<html><body><p>Just some text.</p></body></html>")

    assert (filing.parse_status, chunks) == ("no_sections", [])
    assert filing.n_sections == 0 and filing.n_chunks == 0


def test_missing_core_sections_make_the_status_partial_and_name_them():
    html = "<html><body><div>Item 1. Business</div>" + para(5, "x") + "</body></html>"

    filing, chunks = parse(html)

    assert filing.parse_status == "partial"
    assert filing.parse_error == "missing sections: 1A, 7, 8"
    assert chunks


def test_a_10q_needs_part_one_items_one_and_two():
    html = (
        "<html><body><div>PART I</div><div>Item 1. Financial Statements</div>"
        + para(5, "x")
        + "<div>Item 2. Management's Discussion</div>"
        + para(5, "y")
        + "</body></html>"
    )

    filing, chunks = parse(html, form="10-Q", period_of_report=date(2023, 7, 1))

    assert (filing.parse_status, filing.fiscal_period) == ("ok", "Q3")
    assert {c.section for c in chunks} == {"P1-1", "P1-2"}


def test_an_exception_while_parsing_is_captured_not_raised(monkeypatch: pytest.MonkeyPatch):
    def boom(_: bytes):
        raise RuntimeError("selectolax exploded")

    monkeypatch.setattr(document, "html_to_blocks", boom)

    filing, chunks = parse()

    assert filing.parse_status == "error"
    assert filing.parse_error == "RuntimeError: selectolax exploded"
    assert (filing.fiscal_year, filing.fiscal_period) == (2023, "FY")  # labels do not need the HTML
    assert chunks == [] and filing.n_chunks == 0


def test_a_very_long_error_message_is_truncated():
    # str(exception) can be huge for malformed input; the column must stay small
    filing, _ = parse_document(b"", meta())

    assert filing.parse_status in {"no_sections", "error"}
    assert filing.parse_error is None or len(filing.parse_error) <= 500


def test_fiscal_flags_flow_through_to_the_filing_row():
    filing, _ = parse(label_offset=None, fye_from_companies=True)

    assert filing.dq_flags == ["label_offset_default", "fye_from_companies"]


def test_parsing_is_deterministic():
    assert parse() == parse()


def test_an_unsupported_form_is_an_error_row_not_an_exception():
    filing, chunks = parse(form="8-K")

    assert filing.parse_status == "error" and "form" in (filing.parse_error or "")
    assert chunks == []
