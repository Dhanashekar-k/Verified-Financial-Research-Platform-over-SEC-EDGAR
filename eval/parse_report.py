"""Measure the parse over a local filing store and write the numbers the M02 report quotes.

    uv run python -m eval.parse_report [--store data/raw/filings]
        [--financebench data/raw/financebench] [--seed 20261117]
        [--out docs/milestones/M02-parse-metrics.json]
        [--render docs/milestones/M02-parse-report.md]

Everything here is MEASURED on the files in `--store` (what was fetched), never extrapolated: the
report must say how many filings that is. The store is parsed with `parse_document`, the same
function the Spark job runs. Output (JSON, the only source for the markdown):

* filings by form and parse_status (`ok`, `partial`, `no_sections`, `error`), the missing core
  sections of every non-ok filing, and chunk counts;
* the token histogram of all chunks (bins of 50) and its max (must be <= 400);
* 20 random chunks (seeded) for a human to read, and 50 random source URLs for `check_source_urls`;
* FinanceBench: PDFs parsed, pages with text, chunks, and whether the dataset's `evidence_page_num`
  is the 0-based index of the PDF page that holds the evidence text.

FinanceBench PDFs are parsed here with a placeholder CIK (-1) ONLY to measure extraction; real rows
need the owner's `data/financebench_companies.csv` and are produced by `parse_filings`.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pymupdf

from pipelines.fetch.fetch_filings import MANIFEST_KEY, FetchRecord
from pipelines.parsing.document import ChunkRow, FilingMeta, FilingRow, parse_document
from pipelines.parsing.pdf import PdfMeta, parse_pdf

DEFAULT_STORE = Path("data/raw/filings")
DEFAULT_FB = Path("data/raw/financebench")
DEFAULT_OUT = Path("docs/milestones/M02-parse-metrics.json")
BIN = 50
MAX_TOKENS = 400
SAMPLE_CHUNKS = 20
SAMPLE_URLS = 50
EXCERPT_CHARS = 500


def _records(store: Path) -> list[FetchRecord]:
    path = store / MANIFEST_KEY
    lines = path.read_text().splitlines() if path.is_file() else []
    return sorted(
        (FetchRecord.model_validate_json(ln) for ln in lines if ln.strip()),
        key=lambda r: r.accession,
    )


def parse_store(store: Path) -> tuple[list[FilingRow], list[ChunkRow]]:
    filings: list[FilingRow] = []
    chunks: list[ChunkRow] = []
    for rec in _records(store):
        if rec.http_status != 200:
            continue
        raw = gzip.decompress((store / str(rec.cik) / f"{rec.accession}.htm.gz").read_bytes())
        meta = FilingMeta(
            accession=rec.accession, cik=rec.cik, form=rec.form, filed=rec.filed,
            period_of_report=rec.report_date or date.min, primary_doc_url=rec.url,
        )  # fmt: skip
        filing, rows = parse_document(raw, meta)
        filings.append(filing)
        chunks.extend(rows)
    return filings, chunks


def token_histogram(tokens: list[int]) -> dict[str, int]:
    last = MAX_TOKENS // BIN - 1  # bins are (1-50], (51-100], ... so 50 and 400 stay in range
    hist: Counter[int] = Counter(min(max(t - 1, 0) // BIN, last) for t in tokens)
    return {f"{b * BIN + 1}-{(b + 1) * BIN}": hist.get(b, 0) for b in range(MAX_TOKENS // BIN)}


def html_metrics(store: Path, seed: int) -> dict[str, Any]:
    filings, chunks = parse_store(store)
    status = Counter((f.form, f.parse_status) for f in filings)
    rng = random.Random(seed)
    sample = rng.sample(chunks, min(SAMPLE_CHUNKS, len(chunks)))
    urls = rng.sample(chunks, min(SAMPLE_URLS, len(chunks)))
    tokens = [c.n_tokens for c in chunks]
    return {
        "store": str(store),
        "n_filings": len(filings),
        "status_by_form": {f"{f}|{s}": n for (f, s), n in sorted(status.items())},
        "non_ok": [
            {
                "accession": f.accession,
                "cik": f.cik,
                "form": f.form,
                "status": f.parse_status,
                "error": f.parse_error,
            }
            for f in filings
            if f.parse_status != "ok"
        ],  # fmt: skip
        "n_chunks": len(chunks),
        "n_table_chunks": sum(c.is_table for c in chunks),
        "n_chunks_with_anchor": sum(c.anchor is not None for c in chunks),
        "max_tokens": max(tokens) if tokens else None,
        "chunks_over_400": sum(t > MAX_TOKENS for t in tokens),
        "token_histogram": token_histogram(tokens),
        "chunks_per_section": dict(sorted(Counter(c.section for c in chunks).items())),
        "random_chunks": [
            {
                "chunk_id": c.chunk_id,
                "section": c.section,
                "is_table": c.is_table,
                "n_tokens": c.n_tokens,
                "source_url": c.source_url,
                "text": c.text[:EXCERPT_CHARS],
            }
            for c in sample
        ],  # fmt: skip
        "url_sample": [c.source_url for c in urls],
        "seed": seed,
    }


def financebench_metrics(base: Path) -> dict[str, Any]:
    questions = [
        json.loads(ln)
        for ln in (base / "financebench_open_source.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    docs = sorted({q["doc_name"] for q in questions})
    status: Counter[str] = Counter()
    pages: set[tuple[str, int]] = set()
    n_chunks, max_tokens = 0, 0
    for name in docs:
        data = (base / "pdfs" / f"{name}.pdf").read_bytes()
        filing, rows = parse_pdf(data, PdfMeta(doc_name=name, cik=-1, source_url="x"))
        status[filing.parse_status] += 1
        pages.update((name, c.page) for c in rows if c.page)
        n_chunks += len(rows)
        max_tokens = max([max_tokens, *(c.n_tokens for c in rows)])

    norm = lambda s: re.sub(r"\W+", "", s.lower())  # noqa: E731
    convention: Counter[str] = Counter()
    cache: dict[str, list[str]] = {}
    for q in questions:
        name = q["doc_name"]
        if name not in cache:
            with pymupdf.open(base / "pdfs" / f"{name}.pdf") as doc:
                cache[name] = [norm(p.get_text()) for p in doc]
        for ev in q["evidence"]:
            num, text = ev.get("evidence_page_num"), norm(ev.get("evidence_text", ""))[:80]
            if num is None or len(text) < 30:
                convention["unusable"] += 1
                continue
            hits = [i for i, t in enumerate(cache[name]) if text in t]
            if not hits:
                convention["text_not_found"] += 1
            elif num in hits:
                convention["evidence_page_num_is_0_based_index"] += 1
            elif num - 1 in hits:
                convention["evidence_page_num_is_1_based_number"] += 1
            else:
                convention["found_on_another_page"] += 1
    return {
        "n_questions": len(questions),
        "n_documents": len(docs),
        "parse_status": dict(status),
        "pages_with_text": len(pages),
        "n_chunks": n_chunks,
        "max_tokens": max_tokens,
        "evidence_page_convention": dict(convention),
        "cik": "placeholder -1 (extraction only; real CIKs: data/financebench_companies.csv)",
    }


def render(m: dict[str, Any]) -> str:
    h = m["html"]
    lines = [
        "# M2 parse report (measured by `eval/parse_report.py`)",
        "",
        "Numbers below are copied from `docs/milestones/M02-parse-metrics.json` "
        f"(seed {h['seed']}).",
        f"They describe the {h['n_filings']} filings fetched into `{h['store']}`, NOT the full",
        "S&P 500 target list: the full fetch and parse are NOT MEASURED (cloud sprint).",
        "",
        "## Parse status",
        "",
        "| form | status | filings |",
        "|---|---|---:|",
    ]
    for key, n in h["status_by_form"].items():
        form, status = key.split("|")
        lines.append(f"| {form} | {status} | {n} |")
    lines += [
        "",
        f"Chunks: {h['n_chunks']} ({h['n_table_chunks']} table chunks, "
        f"{h['n_chunks_with_anchor']} cited by element id). Max tokens {h['max_tokens']}; "
        f"chunks over 400: {h['chunks_over_400']}.",
        "",
        "| tokens | chunks |",
        "|---|---:|",
        *(f"| {k} | {v} |" for k, v in h["token_histogram"].items()),
        "",
        "## Filings that are not `ok`",
        "",
    ]
    by_cause = Counter((x["cik"], x["form"], x["status"], x["error"]) for x in h["non_ok"])
    if by_cause:
        lines += ["| cik | form | status | detail | filings |", "|---:|---|---|---|---:|"]
        lines += [
            f"| {c} | {f} | {s} | {e or ''} | {n} |" for (c, f, s, e), n in sorted(by_cause.items())
        ]
    else:
        lines.append("None.")
    fb = m.get("financebench")
    lines += ["", "## FinanceBench PDFs", ""]
    if fb:
        lines += [
            f"{fb['n_documents']} documents cited by {fb['n_questions']} questions: "
            f"{fb['parse_status']}; {fb['pages_with_text']} pages with text; "
            f"{fb['n_chunks']} chunks; "
            f"max tokens {fb['max_tokens']}. CIK: {fb['cik']}.",
            f"Evidence page convention (measured): {fb['evidence_page_convention']}.",
        ]
    else:
        lines.append("NOT MEASURED (no FinanceBench download).")
    lines += ["", f"## {len(h['random_chunks'])} random chunks (to read; seed {h['seed']})", ""]
    for c in h["random_chunks"]:
        lines += [
            f"### `{c['chunk_id']}` ({c['n_tokens']} tokens{', table' if c['is_table'] else ''})",
            "",
            "```text",
            c["text"],
            "```",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the parse and write the report inputs.")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--financebench", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=20261117)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--render", type=Path, default=None)
    args = parser.parse_args(argv)

    metrics: dict[str, Any] = {"html": html_metrics(args.store, args.seed)}
    if args.financebench is not None:
        metrics["financebench"] = financebench_metrics(args.financebench)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    if args.render:
        args.render.write_text(render(metrics), encoding="utf-8")
        print(f"wrote {args.render}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
