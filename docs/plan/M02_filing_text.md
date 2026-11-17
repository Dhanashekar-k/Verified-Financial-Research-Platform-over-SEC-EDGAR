# M2: Filing text corpus

**Time:** about 18 h (fast track: about 14 h, 10-K only) · **Roles:** data engineering (parsing at scale), FDE (messy documents), AI (retrieval quality starts here) · **Depends on:** M1 (companies table)

## 1. Goal

Clean, sectioned, chunked text for S&P 500 10-Ks (and 10-Qs on the full track) from FY2020 to the latest filings, plus **every FinanceBench source document**. Every chunk carries a working citation URL.

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| `curated.companies` (S&P 500 + FinanceBench flags) | `data/targets/filings.csv` (cik, accession, form, filed, report_date, primary_doc, url) |
| submissions API (one call per CIK) | Gzipped HTML in `s3://<lake>/raw/filings/{cik}/{accession}.htm.gz`, plus `raw.filings_html` |
| Filing HTML (≤ 8 req/s) | `curated.filings` with `parse_status` and `parse_error` |
| FinanceBench PDFs (GitHub) | `curated.chunks` with `doc_source ∈ {edgar_html, financebench_pdf}` |
| — | `docs/milestones/M02-parse-report.md`: parse success rate, section-boundary accuracy on a labeled sample, chunk length histogram, 20 random chunks |

Expected volume [Guessing]: about 3,000 10-Ks (+ about 9,000 10-Qs on the full track); about 0.5–1M chunks of ~400 tokens. Measure and record it.

## 3. Steps

### 3.1 Target list (1 h)
`pipelines/fetch/list_filings.py`: for each tracked CIK, call `submissions` (plus older pages from `filings.files` if needed), filter `form ∈ {10-K, 10-K/A, 10-Q, 10-Q/A}` with report dates from FY2020 onward, and build `primary_doc_url`. Write `data/targets/filings.csv` and print counts by form and year.

### 3.2 Fetch to S3 (2 h build + runtime)
`pipelines/fetch/fetch_filings.py --targets data/targets/filings.csv --dest s3://<lake>/raw/filings/`:
- Uses the shared SEC client (8 req/s). It's **idempotent**: a HEAD on S3 skips existing objects. It gzips before upload and records `bytes`, `sha256` and `http_status` in `raw.filings_html`.
- **Run it in AWS**, not from home: a temporary `t4g.small` (or the serving box once M9 exists) in a public subnet with an instance profile allowed `s3:PutObject` on the prefix. About 12k requests at 8/s is under 30 minutes of request time; bandwidth dominates [Guessing].
- FinanceBench: download the PDFs from the GitHub repo (`pdfs/`) to `s3://<lake>/raw/financebench/pdfs/` and the JSONL to `eval/sets/raw/`.

### 3.3 Shared parsing library (6 h; Claude writes it, you review against real filings)
`pipelines/parsing/` is pure Python, with no Spark imports, so it can be reused by the Spark job (M2) and the Lambda (M10):
1. **HTML → blocks:** parse with selectolax. Drop the inline-XBRL header and hidden elements (`ix:header`, `display:none`) [Likely]. Emit an ordered list of blocks: `heading | paragraph | table`, each with the nearest element `id` (for anchors).
2. **Tables:** detect tables with numeric content and serialize them row-wise: `Row label | col1 | col2 …`, repeating the header row at the top of every table chunk. Mark `is_table=true`. Drop layout tables (single cell, no numbers).
3. **Section detection:** regexes for item headings (`Item 1A. Risk Factors`, `ITEM 7. MANAGEMENT'S DISCUSSION…`, with case, punctuation and whitespace variants; 10-Q `Part I – Item 2`). **Skip the table of contents:** headings that appear as link text, or that are followed by under N characters before the next item heading, are TOC entries. Choose the ordered sequence of item headings with the most text between them.
4. **Section codes:** 10-K `1, 1A, 1B, 1C, 2, 3, 5, 7, 7A, 8, 9A, …`; 10-Q `P1-1, P1-2, P1-3, P1-4, P2-1, P2-1A, …`.
5. **Chunking:** within a section only, pack paragraphs into chunks of **≤ 400 tokens measured with the embedder's tokenizer** (`tokenizers` with `BAAI/bge-small-en-v1.5`), with about 60 tokens of overlap. Never split a sentence if you can avoid it. Tables are chunked separately (≤ 400 tokens, header repeated).
6. **Citation URL:** `primary_doc_url + "#" + anchor_id` when an id exists. Otherwise use a **text fragment**, `primary_doc_url + "#:~:text=" + urlencode(first ~8 words)`, which highlights the passage in Chromium browsers and recent Safari [Likely].
7. **PDF path** (FinanceBench): PyMuPDF page text → chunks with `page` set, `section='PDF'`, `fiscal_year` parsed from `doc_name` (e.g. `3M_2018_10K` → 2018), and CIK from a hand-made map `data/financebench_companies.csv`.

### 3.4 Spark job `parse_filings` (3 h)
- Input: `spark.read.format("binaryFile").load("s3://<lake>/raw/filings/*/*.htm.gz")` (or a path list from `raw.filings_html`), repartitioned to about 4× total cores.
- `mapInPandas` / `mapPartitions` calling `pipelines.parsing.parse_document(bytes, meta) -> (filing_row, [chunk_rows])`, catching exceptions per document → `parse_status='error'` and `parse_error`. **Never let one bad filing kill the job.**
- Write `curated.filings` (overwrite partitions by fiscal_year) and `curated.chunks` (MERGE on `chunk_id`, so reruns are idempotent).
- **EMR packaging pitfall:** EMR Serverless runs its own Python version. Check it (`python3 --version` inside the release image) and keep `pipelines/parsing` and `pipelines/spark` compatible with it; add a CI job on that version. Ship dependencies (selectolax, tokenizers, PyMuPDF) with a **custom EMR Serverless image** (arm64, based on the EMR base image) pushed to ECR, or with a `venv-pack` archive via `spark.archives` [Likely; follow the EMR Serverless docs for your release]. You'll reuse the custom image in M3 for embeddings, so it's worth doing once.

### 3.5 Quality evaluation (3 h; **you label**)
- **Section-boundary accuracy:** pick 20 filings (mix of companies, years and forms). For items 1, 1A, 7, 7A and 8, record the true first sentence of each section by hand in `eval/sets/sections_gold.jsonl` (100 labels). The parser must find the right start for ≥ 90%.
- **Parse success rate** over all targets (target ≥ 95%); list failures by cause.
- **Read 20 random chunks** and note anything garbled (hidden XBRL text, broken tables, page headers repeated in text).
- **Citation URL check:** a script requests 50 random `source_url`s at 2 req/s and expects 200 (anchors can't be validated server-side; spot-check 5 in a browser).

## 4. Claude Code session plan

```
/milestone M2
Step 1: list_filings.py + fetch_filings.py per M02 §3.1–3.2. Use pipelines/fetch/sec_client.py.
Local dry run on 5 CIKs, writing to data/raw/filings/. Show counts.
```
```
Step 2: pipelines/parsing per §3.3. Start with html_to_blocks + section detection. Write tests using
3 real filings I'll put in tests/fixtures/filings/ (one 10-K with a hyperlinked TOC, one older 10-K
without ids, one 10-Q). Plan first; explain your TOC-skipping heuristic before coding it.
```
```
Step 3: chunker + tables + citation URLs per §3.3.5–3.3.6, then the PDF path §3.3.7.
Property tests: no chunk exceeds 400 tokens by the bge tokenizer; no chunk spans two sections;
overlap is ≤ 80 tokens; concatenating chunks without overlap reproduces the section text (modulo whitespace).
```
```
Step 4: Spark job parse_filings per §3.4 (local on the 5 CIKs). Then help me build the custom
EMR Serverless image (Dockerfile, arm64, deps) and the Terraform/Make targets to push it to ECR
and run the job on EMR. Don't push or run. I will.
```
```
Step 5: eval/sections_eval.py: compare parser output to eval/sets/sections_gold.jsonl and print
accuracy per section code with the failures listed.
```

## 5. You write

- `eval/sets/sections_gold.jsonl` (100 hand labels; the hook protects it once it's written).
- The review of the TOC heuristic: read it and break it with your fixtures.
- `data/financebench_companies.csv` (about 40 companies → CIK; verify each one in `company_tickers.json`).

## 6. Definition of done

- [ ] `filings.csv` counts recorded; fetch complete; `raw.filings_html` has one row per target.
- [ ] Parse success ≥ 95% (or documented); section-boundary accuracy ≥ 90% on the 100 labels.
- [ ] Chunk count and token histogram recorded; property tests green.
- [ ] 20 random chunks read cleanly (notes in the parse report).
- [ ] 50/50 source URLs return 200.
- [ ] All FinanceBench PDFs parsed with page numbers.

## 7. Pitfalls

- TOC entries mistaken for section starts. This is the top parse bug [Likely].
- Inline XBRL hidden facts leaking into text as number soup.
- Exhibits: fetch only the primary document, not every exhibit (cost and noise).
- Older filings (2020) may use different heading styles from 2025 ones; include both in fixtures.
- Measuring chunk size in the wrong tokenizer (tiktoken ≠ BERT WordPiece).
- Fetching from your home connection and uploading GBs to S3.

## 8. Interview prep

- Why 400 tokens; how you'd choose differently with a long-context embedder; how you'd measure it (evidence recall@k in M4).
- How the TOC heuristic works and its known failure cases.
- Why parsing is where Spark earns its keep (CPU-bound, independent documents; throughput scales with executors).
- How citations survive: anchors vs text fragments; what happens when SEC changes the HTML (it doesn't: filings are immutable).
