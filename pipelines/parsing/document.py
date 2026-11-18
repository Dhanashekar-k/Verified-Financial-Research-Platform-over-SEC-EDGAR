"""One filing's bytes + metadata -> one `curated.filings` row and its `curated.chunks` rows.

    filing_row, chunk_rows = parse_document(html_bytes, meta)

Pure Python (the Spark job and the M10 Lambda both call it). It never raises for a bad document:
any failure becomes `parse_status='error'` with `parse_error` (truncated to 500 characters) and
no chunks, so one bad filing cannot kill a job (runbook 3.4).

`parse_status`: `ok` (all core sections found), `partial` (parsed, but some core section is
missing; `parse_error` names them), `no_sections` (no item headings at all) or `error`.
Core sections: 10-K 1, 1A, 7, 8; 10-Q P1-1, P1-2.

Chunks never cross a section; paragraphs are packed into prose chunks and each data table is
chunked separately with its header row repeated. A chunk's `anchor` is the id of the element it
starts at, when it starts exactly at one; otherwise the URL carries a text fragment.
"""

from __future__ import annotations

import gzip
import zlib
from bisect import bisect_right
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from pipelines.common.fiscal_ref import LabelOffset
from pipelines.parsing.chunker import chunk_table, chunk_text
from pipelines.parsing.citations import citation_url
from pipelines.parsing.fiscal_labels import FiscalLabels, filing_fiscal_labels
from pipelines.parsing.html_blocks import Block, html_to_blocks
from pipelines.parsing.sections import Section, detect_sections
from pipelines.parsing.tables import render_row
from pipelines.parsing.tokens import Tokenizer, get_tokenizer

CORE_10K = ("1", "1A", "7", "8")
CORE_10Q = ("P1-1", "P1-2")
MAX_ERROR_CHARS = 500
DOC_SOURCE_HTML = "edgar_html"


class FilingMeta(BaseModel):
    """What the parse job knows about a filing before reading its HTML."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    accession: str
    cik: int
    form: str
    filed: date
    accepted_at: datetime | None = None
    period_of_report: date | None = None
    primary_doc_url: str
    html_s3_uri: str | None = None
    fye_mmdd: str | None = None
    fye_from_companies: bool = False
    label_offset: LabelOffset | None = None
    sub_fy: int | None = None
    sub_fp: str | None = None
    corpus_version: int = 1


class FilingRow(BaseModel):
    """`curated.filings` (contract section 2.2)."""

    accession: str
    cik: int
    form: str
    filed: date | None  # NULL for FinanceBench PDFs
    accepted_at: datetime | None
    period_of_report: date | None
    fiscal_year: int | None
    fiscal_period: str | None
    primary_doc_url: str
    html_s3_uri: str | None
    parse_status: str
    parse_error: str | None
    n_sections: int
    n_chunks: int
    doc_source: str
    dq_flags: list[str]


class ChunkRow(BaseModel):
    """`curated.chunks` (contract section 2.2)."""

    chunk_id: str
    cik: int
    accession: str
    form: str
    fiscal_year: int | None
    fiscal_period: str | None
    section: str
    section_title: str | None
    chunk_index: int
    text: str
    n_tokens: int
    is_table: bool
    source_url: str
    anchor: str | None
    page: int | None
    doc_source: str
    corpus_version: int


def _labels(meta: FilingMeta) -> FiscalLabels:
    try:
        return filing_fiscal_labels(
            form=meta.form,
            period_of_report=meta.period_of_report,
            fye_mmdd=meta.fye_mmdd,
            fye_from_companies=meta.fye_from_companies,
            offset=meta.label_offset,
            sub_fy=meta.sub_fy,
            sub_fp=meta.sub_fp,
        )
    except ValueError:
        return FiscalLabels(None, None, ("fiscal_unresolved",))


def _filing_row(
    meta: FilingMeta,
    labels: FiscalLabels,
    status: str,
    error: str | None,
    n_sec: int,
    n_chunks: int,
) -> FilingRow:
    return FilingRow(
        accession=meta.accession,
        cik=meta.cik,
        form=meta.form,
        filed=meta.filed,
        accepted_at=meta.accepted_at,
        period_of_report=meta.period_of_report,
        fiscal_year=labels.fiscal_year,
        fiscal_period=labels.fiscal_period,
        primary_doc_url=meta.primary_doc_url,
        html_s3_uri=meta.html_s3_uri,
        parse_status=status,
        parse_error=error[:MAX_ERROR_CHARS] if error else None,
        n_sections=n_sec,
        n_chunks=n_chunks,
        doc_source=DOC_SOURCE_HTML,
        dq_flags=list(labels.dq_flags),
    )


def _section_chunks(
    meta: FilingMeta, labels: FiscalLabels, section: Section, blocks: list[Block], tok: Tokenizer
) -> list[ChunkRow]:
    rows: list[ChunkRow] = []

    def add(text: str, n_tokens: int, anchor: str | None, is_table: bool) -> None:
        index = len(rows)
        rows.append(
            ChunkRow(
                chunk_id=f"{meta.accession}#{section.code}#{index:04d}",
                cik=meta.cik,
                accession=meta.accession,
                form=meta.form,
                fiscal_year=labels.fiscal_year,
                fiscal_period=labels.fiscal_period,
                section=section.code,
                section_title=section.title,
                chunk_index=index,
                text=text,
                n_tokens=n_tokens,
                is_table=is_table,
                source_url=citation_url(meta.primary_doc_url, anchor, text, is_table=is_table),
                anchor=anchor,
                page=None,
                doc_source=DOC_SOURCE_HTML,
                corpus_version=meta.corpus_version,
            )
        )

    run: list[Block] = []

    def flush_run() -> None:
        if not run:
            return
        offsets, text = [], ""
        for b in run:
            offsets.append(len(text))
            text += b.text + "\n"
        text = text.rstrip("\n")
        for c in chunk_text(text, tok):
            at = bisect_right(offsets, c.start) - 1
            anchor = run[at].anchor if c.start == offsets[at] else None
            if anchor is None and not rows and c.start == 0:
                anchor = section.anchor  # the section's first chunk cites its heading's id
            add(c.text, c.n_tokens, anchor, False)
        run.clear()

    body_blocks = blocks[section.start : section.end]
    if section.body_prefix:  # a heading fused with its first sentence: that sentence is body text
        body_blocks = [Block("paragraph", section.body_prefix, anchor=section.anchor), *body_blocks]
    for block in body_blocks:
        if block.kind == "paragraph":
            run.append(block)
            continue
        flush_run()
        lines = [render_row(r) for r in block.rows]
        header = lines[: block.header_rows] if block.header_rows < len(lines) else []
        for n, c in enumerate(chunk_table(header, lines[len(header) :], tok)):
            add(c.text, c.n_tokens, block.anchor if n == 0 else None, True)
    flush_run()
    return rows


def parse_document(
    data: bytes, meta: FilingMeta, tok: Tokenizer | None = None
) -> tuple[FilingRow, list[ChunkRow]]:
    labels = _labels(meta)
    try:
        tok = tok or get_tokenizer()
        blocks = html_to_blocks(data)
        sections = detect_sections(blocks, meta.form)
        chunks: list[ChunkRow] = []
        for section in sections:
            chunks.extend(_section_chunks(meta, labels, section, blocks, tok))
    except Exception as exc:  # noqa: BLE001 - a bad document must never kill the job
        return _filing_row(meta, labels, "error", f"{type(exc).__name__}: {exc}", 0, 0), []

    if not sections:
        return _filing_row(meta, labels, "no_sections", None, 0, 0), []
    core = CORE_10K if meta.form.startswith("10-K") else CORE_10Q
    missing = [c for c in core if c not in {s.code for s in sections}]
    status = "partial" if missing else "ok"
    error = f"missing sections: {', '.join(missing)}" if missing else None
    return _filing_row(meta, labels, status, error, len(sections), len(chunks)), chunks


def error_result(meta: FilingMeta, message: str) -> tuple[FilingRow, list[ChunkRow]]:
    """The row for a filing that could not be parsed for a reason outside the HTML itself."""
    return _filing_row(meta, _labels(meta), "error", message, 0, 0), []


def parse_gz_document(
    content: bytes | None, meta: FilingMeta, tok: Tokenizer | None = None
) -> tuple[FilingRow, list[ChunkRow]]:
    """`parse_document` for a stored `.htm.gz`; a missing or corrupt object is an error row."""
    if content is None:
        return error_result(meta, f"object missing: {meta.html_s3_uri}")
    try:
        html = gzip.decompress(content)
    except (OSError, EOFError, zlib.error) as exc:
        return error_result(meta, f"{type(exc).__name__}: cannot gunzip {meta.html_s3_uri}: {exc}")
    return parse_document(html, meta, tok)
