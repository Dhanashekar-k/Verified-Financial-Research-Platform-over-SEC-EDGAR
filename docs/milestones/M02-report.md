# M2 Definition-of-Done report (local half measured; owner inputs and cloud half outstanding)

Generated 2026-11-17 by following `.claude/skills/dod/SKILL.md`. Numbers are copied from the files named
below; a number with no file is marked `NOT MEASURED`. Where a number comes from a command run for this
report, the command is shown. Everything measured here describes the **135 real filings fetched
locally** (5 CIKs: Apple, Microsoft, Amazon, JPMorgan, Berkshire; `data/raw/filings`), not the 13,239-filing
target list, and the 84 FinanceBench PDFs.

## Verdict

**M2 is NOT done.** Four items FAIL or cannot be run, for reasons that are not code defects:

1. The section-boundary accuracy (≥ 90% on 100 labels) is `NOT MEASURED`: `eval/sets/sections_gold.jsonl` is
   Shekar's file and does not exist.
2. The three real fixture filings (`tests/fixtures/filings/`) do not exist, so the real-filing tests skip.
3. The FinanceBench rows in `curated.chunks` were not produced: `data/financebench_companies.csv` (company → CIK)
   is Shekar's file and does not exist. Extraction itself was measured (84/84 PDFs).
4. The full fetch and parse of the 13,239 targets is the cloud sprint (nothing was applied, pushed or run in AWS).

Two checks FAIL on what was measured: strict parse success is 93 of 135 filings `ok` (target ≥ 95%), and the 20
random chunks did not all read cleanly. Causes are listed under "Open issues".

## Checklist (M02 runbook section 6)

| # | Item | Status | Evidence |
|---|---|---|---|
| 1a | `filings.csv` counts recorded | PASS | `make list-filings` style run: `python -m pipelines.fetch.list_filings --keep-going --summary-out docs/milestones/M02-targets.json` → "wrote 13239 targets", 500 of 500 CIKs listed, 0 failed. Counts in the numbers table. |
| 1b | fetch complete | NOT RUN | Only 135 of 13,239 targets were fetched, locally, to test the fetcher (135 fetched, 0 failed; `docs/milestones/M02-parse-metrics.json` `html.n_filings`). The full fetch belongs on an AWS box (cloud sprint). |
| 1c | `raw.filings_html` has one row per target | NOT RUN | 135 rows for the 135 fetched (command: Spark `count()` on `lake.raw.filings_html`, all HTTP 200). One row per fetched filing holds; for the 13,239 targets it is not run. |
| 2a | Parse success ≥ 95% (or documented) | **FAIL** (documented) | 93 `ok` + 42 `partial` of 135, 0 `error`, 0 `no_sections` (`html.status_by_form`). All 42 `partial` are 10-Qs of two companies; causes under Open issues. The 32 10-Ks all parsed `ok`. |
| 2b | Section-boundary accuracy ≥ 90% on the 100 labels | NOT RUN | `python -m eval.sections_eval` → "NOT MEASURED (eval/sets/sections_gold.jsonl does not exist yet)". The tool is tested with a synthetic gold file only (`tests/unit/test_sections_eval.py`, 9 passed). |
| 3a | Chunk count and token histogram recorded | PASS | `html.n_chunks`, `html.token_histogram`, `html.max_tokens` in `M02-parse-metrics.json`; the Spark job's table agrees: `lake.curated.chunks` 38922 rows (command output). |
| 3b | Property tests green | PASS | `pytest tests/unit/parsing/test_chunker.py` → 19 passed (no chunk over 400 tokens, overlap ≤ 80, chunks reproduce the text, every chunk adds text). `make test` → 1199 passed, 1 skipped. |
| 4 | 20 random chunks read cleanly (notes in the parse report) | **FAIL** | Read by Claude, not Shekar (seed 20261117, `html.random_chunks`). No hidden XBRL, no repeated page headers, tables keep their header and rows. But 7 of 20 sit under a section label that does not describe them, and 3 more have noise. Details under Open issues; Shekar should re-read. |
| 5 | 50/50 source URLs return 200 | PASS (documents) / NOT RUN (anchors) | `python -m eval.check_source_urls --rate 2` → "32/32 documents returned 200 (50 sampled chunk URLs)". The fragment (`#id` or `#:~:text=`) is never seen by a server: the 5-in-a-browser spot check is **NOT RUN** (Shekar). |
| 6a | All FinanceBench PDFs parsed with page numbers (extraction) | PASS | `financebench.parse_status` `{"ok": 84}`, `pages_with_text` 11948, every chunk has `page` (measured with a placeholder CIK of -1; extraction only). |
| 6b | FinanceBench rows in `curated.chunks` | NOT RUN | `lake.curated.chunks` holds only `edgar_html` rows (command output: `doc edgar_html 38922`). Without the company file `parse_filings` logs "FinanceBench PDFs NOT parsed" (`test_without_the_owners_company_file_the_pdfs_are_reported_not_parsed`). Needs `data/financebench_companies.csv`. |

### Other checks run

| Check | Status | Evidence |
|---|---|---|
| Spark vs pure-Python equivalence (mandatory) | PASS | `pytest tests/integration/test_parse_filings.py …` → 14 passed. Mutating the Spark join (`period_of_report` without the `sub.period` fallback) made `test_the_spark_output_equals_the_pure_python_parse_for_every_filing` fail; restoring it passed. On the real data both paths give 38922 chunks (`html.n_chunks`; `lake.curated.chunks`). |
| Text and facts agree on fiscal labels (ADR-023) | PASS (35 of 135 comparable) | `docs/milestones/M02-fiscal-agreement.json`: `with_facts_compared` 35, `agree` 35, `mismatches` `[]`. 100 filings have no facts in the lake (only 7 FSDS quarters are loaded), so they are not compared. |
| `make test` / `make test-integration` / `make lint` | PASS | `make test` 1199 passed, 1 skipped (this run); `make test-integration` 351 passed (earlier this session, after the last change to integration-tested code); lint: "All checks passed", mypy clean. |
| `make check-gold` | PASS (skip) | "check-gold: skipped, no gold files exist yet". |
| EMR image builds and runs the parse job | PASS (local) | `docker build --platform linux/arm64 infra/emr` (not pushed); the parse job on that image ran from the built `pipelines.zip` alone: Python 3.12.15, Spark 3.5.8-amzn-0, three real filings `ok`. This is a local container, not EMR. |
| Terraform | PASS (fmt, validate only) | `terraform fmt -check -recursive` OK; `terraform validate` in `envs/dev`: "The configuration is valid". Nothing was planned or applied. |

## Numbers

| Number | Value | Source file | Key |
|---|---:|---|---|
| Target filings (10-K, 10-K/A, 10-Q, 10-Q/A) | 13239 | `docs/milestones/M02-targets.json` | `n_targets` |
| CIKs listed / requested | 500 / 500 | same | `n_ciks_listed`, `n_ciks_requested` |
| 10-K / 10-K/A | 3012 / 86 | same | `by_form["10-K"]`, `by_form["10-K/A"]` |
| 10-Q / 10-Q/A | 10109 / 32 | same | `by_form["10-Q"]`, `by_form["10-Q/A"]` |
| Targets with report year 2026 (10-K+10-K/A+10-Q+10-Q/A) | 101+1+1399+3 | same | `by_form_and_year["10-K\|2026"]` and the three others |
| Filings fetched and parsed locally | 135 | `docs/milestones/M02-parse-metrics.json` | `html.n_filings` |
| `ok` 10-K / `ok` 10-Q / `partial` 10-Q | 32 / 61 / 42 | same | `html.status_by_form` |
| Chunks / table chunks | 38922 / 15062 | same | `html.n_chunks`, `html.n_table_chunks` |
| Chunks cited by element id | 12971 | same | `html.n_chunks_with_anchor` |
| Max tokens / chunks over 400 | 400 / 0 | same | `html.max_tokens`, `html.chunks_over_400` |
| Token histogram (1-50, 51-100, 101-150, 151-200, 201-250, 251-300, 301-350, 351-400) | 3324, 4799, 3939, 3141, 2812, 2057, 2717, 16133 | same | `html.token_histogram` |
| Chunks in section `15` / `P2-6` | 5923 / 10254 | same | `html.chunks_per_section` |
| FinanceBench questions / documents | 150 / 84 | same | `financebench.n_questions`, `financebench.n_documents` |
| FinanceBench PDFs `ok` / pages with text / chunks | 84 / 11948 / 29620 | same | `financebench.parse_status.ok`, `pages_with_text`, `n_chunks` |
| Evidence items whose `evidence_page_num` is the 0-based PDF page index | 189 (of 189 usable) | same | `financebench.evidence_page_convention.evidence_page_num_is_0_based_index` |
| Source URLs: sampled chunk URLs / distinct documents / 200 | 50 / 32 / 32 | `docs/milestones/M02-url-check.json` | `sampled_chunk_urls`, `distinct_documents`, `ok` |
| Filings compared with their facts / agreeing / not comparable | 35 / 35 / 100 | `docs/milestones/M02-fiscal-agreement.json` | `with_facts_compared`, `agree`, `without_facts_not_compared` |
| Section-boundary accuracy | NOT MEASURED | n/a | no gold file |
| Full fetch: bytes, minutes, cost; full parse: minutes, cost | NOT MEASURED | n/a | cloud sprint (`docs/milestones/job-metrics/` is where `emr_run` writes them) |
| Bytes stored for the 135 local filings (gzip), sum over rows | 40089905 | `data/raw/filings/_manifest.jsonl` (gitignored) | `bytes` |

## Open issues

**Failures found in what was measured**

1. **JPMorgan 10-Qs (21 filings) and Berkshire 10-Qs (21 filings) are `partial`** (`html.non_ok`): JPMorgan's 10-Q has no
   `Item 1/2` headings in the body (cross-reference format), so `P1-1` and `P1-2` are missing; Berkshire's MD&A heading
   has no `Item 2` text, so `P1-2` is missing. No company-specific rule was added. Consequence: JPMorgan's whole
   10-Q body lands in the last section found (`P2-6`: 10254 chunks), and its 10-K financial statements in `15` (5923).
   Section filters for these companies will be wrong until a title-based fallback exists.
2. **The 20-chunk read** (`html.random_chunks`, chunk ids in the file): 7 JPMorgan chunks under a misleading section
   (#2, #4, #5, #8, #11, #12, #17); #7 (Microsoft) a small table read as prose lines; #14 (Microsoft) bullet glyphs
   split from their text; #19 (Amazon) a page footer ("See accompanying notes…") glued to the next statement's title.
3. **Section-boundary accuracy cannot be reported** without the owner's gold file.

**Risks not measured**

4. The running-header filter drops any short paragraph that repeats six or more times with only digits changing; a real
   filing with such repeated lines would lose them. Not measured on real data beyond the 135 filings.
5. `curated.filings.accepted_at` is NULL when the accession is not in the loaded FSDS quarters (the submissions API's
   acceptance time is not captured). Count of NULLs: NOT MEASURED.
6. `fiscal_year` for filings outside the loaded quarters uses `curated.companies.fye_mmdd` and an offset of 0 when the
   company has no 10-K in `fsds_sub` (flags `fye_from_companies`, `label_offset_default`); the share of filings
   carrying those flags is NOT MEASURED on the full list. 35 of 35 comparable filings agreed.
7. `list_filings` is open-ended on the right (to the latest filings, as the runbook says); 1504 of 13239 targets have a
   report year of 2026. Your note said FY2020–2025: cap it with `--to-date` if you want exactly that.
8. The earlier fetch/loader commits got a reviewer pass only at the end of this session (see the review note below if
   it arrived); the later steps were reviewed before commit.

**M1 full history (your cloud-sprint request)**

9. Verified in code: nothing in the FSDS download, raw load, normalize or DQ code filters fiscal years; the downloader
   defaults to `--from 2009q2 --to latest` and the DQ report checks `starts_at_2009q2` and gaps. The local lake holds
   only 7 non-contiguous quarters (`M01-report.md`).
   **Blocker:** `download_fsds` and `fsds_to_iceberg` reject `s3://` (both say so), and `fsds_to_iceberg` validates
   headers and row counts with local file I/O. So the full history can be downloaded on a box with disk and loaded
   locally, but an EMR run of the raw load needs S3 support first. I did not build it: it is a redesign of M1 code
   (distributed header and count checks), not a patch. `make emr-run JOB=normalize_facts` can run the normalize step
   once the raw tables exist in Glue. T1/T2 stay `NOT MEASURED` until then; `pipelines/cloud/emr_run.py` records the
   billed vCPU/memory/storage hours and computes dollars and dollars per million facts only when you supply the three
   unit prices and the measured `curated.facts` row count.

## What I need from you (nothing was fabricated)

1. **Three real fixture filings** in `tests/fixtures/filings/`: one 10-K with a hyperlinked TOC, one older 10-K without
   element ids, one 10-Q. For each `<name>.htm` add `<name>.json`:
   `{"form": "10-K", "accession": "…", "cik": 0, "period_of_report": "YYYY-MM-DD", "fye_mmdd": "MMDD"}`. Candidates for
   the first and third are in `data/raw/filings/<cik>/<accession>.htm.gz` (gunzip them); the local store starts in 2020,
   so the older no-id filing has to come from elsewhere. `tests/unit/parsing/test_real_fixtures.py` then runs.
2. **`eval/sets/sections_gold.jsonl`** (100 labels): rows
   `{"accession","section","first_sentence","labeled_by"}`, `section` as a code (`1`, `1A`, `7`, `7A`, `8`; for 10-Qs
   `P1-2`, `P2-1A`), then `make gold-checksums`. Run `python -m eval.sections_eval --min-accuracy 0.9`.
3. **`data/financebench_companies.csv`**, columns `company,cik`, for these 32 names exactly as the JSONL spells them:
   3M, AES Corporation, AMD, Activision Blizzard, Adobe, Amazon, Amcor, American Express, American Water Works,
   Best Buy, Block, Boeing, CVS Health, Coca-Cola, Corning, Costco, Foot Locker, General Mills, JPMorgan,
   Johnson & Johnson, Kraft Heinz, Lockheed Martin, MGM Resorts, Microsoft, Netflix, Nike, Paypal, PepsiCo, Pfizer,
   Ulta Beauty, Verizon, Walmart. Then `make parse-filings FB=1`.
4. **The 5-in-a-browser spot check** of citation fragments (items from `html.url_sample`).
5. **Cloud sprint, in order** (all yours; nothing was run): `terraform plan/apply` (new `ecr` and `emr` modules);
   `make emr-image-build`, `make emr-image-push ECR_URL=… YES=1`; set `emr_image_uri` and apply again; `make emr-package
   UPLOAD=s3://<lake>/artifacts` (from a clean tree); fetch the 13,239 filings from an AWS box
   (`make fetch-filings DEST=s3://<lake>/raw/filings`); `make emr-run JOB=load_filings_html …` then `JOB=parse_filings`.

## Decisions that need an ADR (or your sign-off)

- **ADR-023 and ADR-024 are drafted** (`docs/decisions.md`, status `proposed`, consequences left to you).
- `curated.filings` is merged on `accession` instead of `overwritePartitions` by `fiscal_year` (a re-parse can move a
  filing between fiscal years and leave a stale row); the runbook text says overwrite.
- `parse_status` values `ok / partial / no_sections / error` and the core-section definition (10-K 1, 1A, 7, 8; 10-Q P1-1,
  P1-2).
- FinanceBench questions are stored under `data/raw/financebench/`, not `eval/sets/raw/` (the hook protects the latter
  as gold); M4 decides when to promote a copy. `chunks.page` is 1-based while the dataset's `evidence_page_num` is
  0-based (contract section 1.6 now says so).
- Contract changes made, in contract-first order: `curated.filings.dq_flags`, the fiscal-label rules, and
  `raw.filings_html.report_date` (`docs/plan/05_data_contracts.md` sections 1.6, 2.1, 2.2).
- The bundled bge-small tokenizer (`pipelines/parsing/assets/`, pinned by SHA-256) rather than a network download.
- The fixture/gold-dependent steps were skipped, not stubbed with invented data.
