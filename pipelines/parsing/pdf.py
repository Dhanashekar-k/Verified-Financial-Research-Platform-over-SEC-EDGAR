"""FinanceBench PDFs -> page-numbered chunks (M02 section 3.3 step 7).

PyMuPDF extracts each page's text; chunks never cross a page, so `page` is exact (1-based: the
PyMuPDF page index plus one). A chunk is `fb:{doc_name}#p{page}#{index:04d}`, `section='PDF'`,
`doc_source='financebench_pdf'`, and its `source_url` is the PDF's URL plus `#page=N`.

`fiscal_year`, `fiscal_period` and `form` come from the document name (`3M_2018_10K` -> 2018,
FY, 10-K; `BESTBUY_2024Q2_10Q` -> 2024, Q2, 10-Q). The CIK comes from the owner's
`data/financebench_companies.csv`; without one the document is an `error` row, never a guess.
Like `parse_document` this never raises for a bad file.
"""

from __future__ import annotations

import re

import pymupdf
from pydantic import BaseModel, ConfigDict

from pipelines.parsing.chunker import chunk_text
from pipelines.parsing.document import ChunkRow, FilingRow
from pipelines.parsing.tokens import Tokenizer, get_tokenizer

DOC_SOURCE_PDF = "financebench_pdf"
MAX_ERROR_CHARS = 500
_YEAR_Q = re.compile(r"_(\d{4})(?:Q([1-4]))?(?:_|$)")
_FORM = re.compile(r"_(10K|10Q|8K)(?:_|$)", re.IGNORECASE)
_FORMS = {"10K": "10-K", "10Q": "10-Q", "8K": "8-K"}


class PdfMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_name: str
    cik: int | None
    source_url: str
    corpus_version: int = 1


def doc_labels(doc_name: str) -> tuple[int | None, str | None, str | None]:
    """(fiscal_year, fiscal_period, form) read from a FinanceBench document name."""
    ym = _YEAR_Q.search(doc_name)
    fm = _FORM.search(doc_name)
    form = _FORMS[fm.group(1).upper()] if fm else None
    year = int(ym.group(1)) if ym else None
    if ym and ym.group(2):
        period: str | None = f"Q{ym.group(2)}"
    elif form == "10-K" and year is not None:
        period = "FY"
    else:
        period = None
    return year, period, form


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _page_text(page: pymupdf.Page) -> str:
    """The page as lines: table rows as `a | b | c`, wrapped prose reflowed into paragraphs.

    PyMuPDF puts every text line on its own line, so reading `get_text()` loses both: a statement
    row becomes one cell per line and a paragraph is cut mid-sentence. Here lines are grouped into
    visual rows by vertical overlap; a row with several pieces is a table row (cells in x order),
    and consecutive single-piece rows from one PDF text block are joined into a paragraph.
    """
    pieces: list[tuple[float, float, float, str, int]] = []  # y0, y1, x0, text, block
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                x0, y0, _, y1 = line["bbox"]
                pieces.append((y0, y1, x0, text, block["number"]))
    pieces.sort(key=lambda p: (p[0], p[2]))

    rows: list[list[tuple[float, float, float, str, int]]] = []
    for piece in pieces:
        if rows:
            y0 = min(p[0] for p in rows[-1])
            y1 = max(p[1] for p in rows[-1])
            if _overlap(y0, y1, piece[0], piece[1]) >= 0.5 * min(y1 - y0, piece[1] - piece[0]):
                rows[-1].append(piece)
                continue
        rows.append([piece])

    lines: list[str] = []
    paragraph_block: int | None = None
    for row in rows:
        if len(row) > 1:
            lines.append(" | ".join(p[3] for p in sorted(row, key=lambda p: p[2])))
            paragraph_block = None
        elif paragraph_block == row[0][4] and lines:
            lines[-1] += " " + row[0][3]
        else:
            lines.append(row[0][3])
            paragraph_block = row[0][4]
    return "\n".join(lines)


def _row(meta: PdfMeta, year, period, form, status, error, n_chunks) -> FilingRow:
    return FilingRow(
        accession=f"fb:{meta.doc_name}",
        cik=meta.cik or 0,
        form=form or "",
        filed=None,
        accepted_at=None,
        period_of_report=None,
        fiscal_year=year,
        fiscal_period=period,
        primary_doc_url=meta.source_url,
        html_s3_uri=None,
        parse_status=status,
        parse_error=error[:MAX_ERROR_CHARS] if error else None,
        n_sections=1 if n_chunks else 0,
        n_chunks=n_chunks,
        doc_source=DOC_SOURCE_PDF,
        dq_flags=[],
    )


def parse_pdf(
    data: bytes, meta: PdfMeta, tok: Tokenizer | None = None
) -> tuple[FilingRow, list[ChunkRow]]:
    year, period, form = doc_labels(meta.doc_name)
    if meta.cik is None:
        return (
            _row(
                meta, year, period, form, "error",
                f"no CIK for {meta.doc_name}: add its company to data/financebench_companies.csv",
                0,
            ),
            [],
        )  # fmt: skip
    try:
        tok = tok or get_tokenizer()
        chunks: list[ChunkRow] = []
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            for number, page in enumerate(doc, start=1):
                text = _page_text(page)
                for index, c in enumerate(chunk_text(text, tok)):
                    chunks.append(
                        ChunkRow(
                            chunk_id=f"fb:{meta.doc_name}#p{number}#{index:04d}",
                            cik=meta.cik,
                            accession=f"fb:{meta.doc_name}",
                            form=form or "",
                            fiscal_year=year,
                            fiscal_period=period,
                            section="PDF",
                            section_title=None,
                            chunk_index=index,
                            text=c.text,
                            n_tokens=c.n_tokens,
                            is_table=False,
                            source_url=f"{meta.source_url}#page={number}",
                            anchor=None,
                            page=number,
                            doc_source=DOC_SOURCE_PDF,
                            corpus_version=meta.corpus_version,
                        )
                    )
    except Exception as exc:  # noqa: BLE001 - a bad PDF must never kill the job
        return _row(meta, year, period, form, "error", f"{type(exc).__name__}: {exc}", 0), []
    status = "ok" if chunks else "no_sections"
    return _row(meta, year, period, form, status, None, len(chunks)), chunks


def missing_pdf_result(meta: PdfMeta) -> tuple[FilingRow, list[ChunkRow]]:
    """The row for a cited PDF whose file is not in the object store."""
    year, period, form = doc_labels(meta.doc_name)
    return _row(meta, year, period, form, "error", f"object missing: {meta.doc_name}.pdf", 0), []
