"""Tests for ``pipelines/parsing/pdf.py``: FinanceBench PDFs -> page-numbered chunks.

PDFs are built in memory with PyMuPDF (synthetic; no real FinanceBench file is committed).
"""

from __future__ import annotations

import pymupdf
import pytest

from pipelines.parsing.pdf import PdfMeta, doc_labels, parse_pdf
from pipelines.parsing.tokens import get_tokenizer

URL = "https://raw.githubusercontent.com/patronus-ai/financebench/main/pdfs/3M_2018_10K.pdf"


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 550, 780), text, fontsize=9)
    return doc.tobytes()


def meta(**kw) -> PdfMeta:
    base = {"doc_name": "3M_2018_10K", "cik": 66740, "source_url": URL}
    return PdfMeta(**(base | kw))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("3M_2018_10K", (2018, "FY", "10-K")),
        ("AMCOR_2022_10K", (2022, "FY", "10-K")),
        ("BESTBUY_2024Q2_10Q", (2024, "Q2", "10-Q")),
        ("AMD_2022_8K_dated-2022-01-31_earnings", (2022, None, "8-K")),
        ("WEIRDNAME", (None, None, None)),
    ],
)
def test_labels_come_from_the_document_name(name: str, expected):
    assert doc_labels(name) == expected


def test_each_page_is_chunked_separately_with_one_based_page_numbers():
    pdf = make_pdf(["First page about revenue growth.", "Second page about risk factors."])

    filing, chunks = parse_pdf(pdf, meta())

    assert filing.parse_status == "ok" and filing.n_chunks == len(chunks) == 2
    assert [(c.page, c.chunk_id) for c in chunks] == [
        (1, "fb:3M_2018_10K#p1#0000"),
        (2, "fb:3M_2018_10K#p2#0000"),
    ]
    assert "revenue growth" in chunks[0].text and "risk factors" in chunks[1].text


def test_rows_carry_the_contract_columns_for_pdf_documents():
    filing, [c, *_] = parse_pdf(make_pdf(["Net sales were 32,765 million."]), meta())

    assert (filing.accession, filing.cik, filing.form) == ("fb:3M_2018_10K", 66740, "10-K")
    assert (filing.fiscal_year, filing.fiscal_period, filing.doc_source) == (
        2018,
        "FY",
        "financebench_pdf",
    )
    assert (c.section, c.doc_source, c.is_table, c.anchor) == (
        "PDF",
        "financebench_pdf",
        False,
        None,
    )
    assert (c.cik, c.fiscal_year, c.fiscal_period, c.corpus_version) == (66740, 2018, "FY", 1)
    assert c.source_url == URL + "#page=1"
    assert c.n_tokens == get_tokenizer().count(c.text)


def test_a_long_page_splits_into_several_chunks_within_400_tokens():
    sentence = "The company reported higher operating income because of pricing and volume. "
    pdf = make_pdf([sentence * 60])

    _, chunks = parse_pdf(pdf, meta())

    assert len(chunks) >= 2
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(c.n_tokens <= 400 and c.page == 1 for c in chunks)


def test_blank_pages_produce_no_chunks_and_the_pdf_still_counts_as_ok():
    filing, chunks = parse_pdf(make_pdf(["", "Only this page has text."]), meta())

    assert [c.page for c in chunks] == [2]
    assert filing.parse_status == "ok"


def test_a_pdf_with_no_text_at_all_is_flagged_no_sections():
    filing, chunks = parse_pdf(make_pdf(["", ""]), meta())

    assert (filing.parse_status, chunks) == ("no_sections", [])


def test_a_missing_cik_is_an_error_naming_the_file_the_owner_writes():
    filing, chunks = parse_pdf(make_pdf(["Text."]), meta(cik=None))

    assert filing.parse_status == "error" and chunks == []
    assert "financebench_companies.csv" in (filing.parse_error or "")


def test_corrupt_bytes_are_captured_as_an_error_row():
    filing, chunks = parse_pdf(b"%PDF-1.7 this is not a pdf", meta())

    assert filing.parse_status == "error" and chunks == []
    assert filing.parse_error and len(filing.parse_error) <= 500


def test_lines_wrapped_by_the_pdf_are_joined_into_sentences():
    pdf = make_pdf(["Revenue grew\nstrongly in the\nfourth quarter."])

    _, [c] = parse_pdf(pdf, meta())

    assert "Revenue grew strongly in the fourth quarter." in c.text


def test_table_rows_keep_their_cells_on_one_line_and_prose_is_reflowed():
    doc = pymupdf.open()
    page = doc.new_page()
    for y, cells in ((100, ("Net sales", "2023", "2022")), (116, ("Revenue", "1,000", "900"))):
        for x, text in zip((60, 300, 400), cells, strict=True):
            page.insert_text((x, y), text, fontsize=9)
    page.insert_textbox(
        pymupdf.Rect(50, 200, 300, 260), "Para two begins here and wraps over lines.", fontsize=9
    )

    _, [c] = parse_pdf(doc.tobytes(), meta())

    assert c.text.splitlines() == [
        "Net sales | 2023 | 2022",
        "Revenue | 1,000 | 900",
        "Para two begins here and wraps over lines.",
    ]
