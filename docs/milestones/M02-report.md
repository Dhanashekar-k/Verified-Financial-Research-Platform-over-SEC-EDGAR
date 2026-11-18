# M2 Definition-of-Done report (local half measured; owner inputs and cloud half outstanding)

Generated 2026-11-17 by following `.claude/skills/dod/SKILL.md`. Numbers are copied from the files named
below; a number with no file is marked `NOT MEASURED`. Where a number comes from a command run for this
report, the command is shown. Everything measured here describes the **135 real filings fetched
locally** (5 CIKs: Apple, Microsoft, Amazon, JPMorgan, Berkshire; `data/raw/filings`), not the 13,239-filing
target list, and the 84 FinanceBench PDFs.

## Verdict

**M2 is NOT done.** Three items FAIL or cannot be run, for reasons that are not code defects:

1. The section-boundary accuracy (≥ 90% on 100 labels) is `NOT MEASURED`: `eval/sets/sections_gold.jsonl` is
   Shekar's file and does not exist.
2. ~~The three real fixture filings do not exist~~ Resolved: three real filings are in `tests/fixtures/filings/` and
   `tests/unit/parsing/test_real_fixtures.py` runs on them (10 passed): core sections found, chunks clean, sidecars equal the
   filings' own `dei` tags, and the parsed fiscal labels equal the company's own (Ulta needs offset -1).
3. ~~The FinanceBench rows were not produced~~ Resolved: `data/financebench_companies.csv` now exists (resolved and
   verified against the SEC submissions, see below) and `make parse-filings FB=1` loaded the PDFs into the local lake.
   Please review the CSV (it is gitignored under `data/`; the evidence is committed).
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
| 3b | Property tests green | PASS | `pytest tests/unit/parsing/test_chunker.py` → 19 passed (no chunk over 400 tokens, overlap ≤ 80, chunks reproduce the text, every chunk adds text). `make test` → 1215 passed, 0 skipped (the fixtures now run). |
| 4 | 20 random chunks read cleanly (notes in the parse report) | **FAIL** | Read by Claude, not Shekar (seed 20261117, `html.random_chunks`). No hidden XBRL, no repeated page headers, tables keep their header and rows. But 7 of 20 sit under a section label that does not describe them, and 3 more have noise. Details under Open issues; Shekar should re-read. |
| 5 | 50/50 source URLs return 200 | PASS (documents) / NOT RUN (anchors) | `python -m eval.check_source_urls --rate 2` → "32/32 documents returned 200 (50 sampled chunk URLs)". The fragment (`#id` or `#:~:text=`) is never seen by a server: the 5-in-a-browser spot check is **NOT RUN** (Shekar). |
| 6a | All FinanceBench PDFs parsed with page numbers (extraction) | PASS | `financebench.parse_status` `{"ok": 84}`, `pages_with_text` 11948, every chunk has `page` (`docs/milestones/M02-parse-metrics.json`, measured with a placeholder CIK; superseded by 6b). |
| 6b | FinanceBench rows in `curated.chunks` | PASS | `make parse-filings FB=1` (command output): `curated.filings` 219 rows = 135 `edgar_html` + 84 `financebench_pdf` (all `ok`, 32 distinct CIKs, none <= 0); `curated.chunks` 68542 rows = 38922 + 29620, the PDF chunks all with `page` (1 to 549) and at most 400 tokens. A real relative-path bug in `parse_filings` surfaced on this run and was fixed with a regression test. |

### Other checks run

| Check | Status | Evidence |
|---|---|---|
| Spark vs pure-Python equivalence (mandatory) | PASS | `pytest tests/integration/test_parse_filings.py …` → 14 passed. Mutating the Spark join (`period_of_report` without the `sub.period` fallback) made `test_the_spark_output_equals_the_pure_python_parse_for_every_filing` fail; restoring it passed. On the real data both paths give 38922 chunks (`html.n_chunks`; `lake.curated.chunks`). |
| Text and facts agree on fiscal labels (ADR-023) | PASS (35 of 135 comparable) | `docs/milestones/M02-fiscal-agreement.json`: `with_facts_compared` 35, `agree` 35, `mismatches` `[]`. 100 filings have no facts in the lake (only 7 FSDS quarters are loaded), so they are not compared. |
| `make test` / `make test-integration` / `make lint` | PASS | `make test` 1215 passed (this run); `make test-integration` 351 passed (earlier this session, after the last change to integration-tested code); lint: "All checks passed", mypy clean. |
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
8. The earlier fetch/store/loader commits were reviewed only after the parse library. The review found, and
   this session fixed (with tests that fail without the fix): a failed store write kept downloading the whole list
   and skipped the manifest write; an S3 role without `s3:ListBucket` gets 403 (not 404) for a missing key, so the
   first S3 run would crash; one accession under two CIKs collided in the manifest. **The fetch box's instance
   profile needs `s3:PutObject` and `s3:GetObject` on the prefix and `s3:ListBucket` on the bucket** (the runbook
   names only `PutObject`). That instance profile is not in the Terraform yet (`modules/emr` covers the EMR job
   role only), so it is part of the cloud sprint.

**Fixture survey (50 sampled S&P 500 10-Ks, FY2020-2025; `docs/milestones/M02-fixture-survey.json`)**

10. `sgml_wrapped` 0 of `n_sampled` 50, so the SGML-wrapper limitation (ADR-025) does not touch the ingest window;
    the three 2011-2012 candidates do parse to `no_sections` (`pre_2020_candidates`). `without_anchor_toc` lists 3
    (INVH, STZ, USB): essentially every in-window filing has an anchor TOC. Of the 3, USB (FY2025, 1.0 MB) has no
    internal links at all; INVH and STZ link the page numbers of their TOC rows, not the item text. All three still
    parse `ok`, so the heading-text path works on them; nothing here needs a fixture, and one synthetic unit test
    already covers an unlinked TOC (`test_an_unlinked_paragraph_toc_is_skipped_by_text_weight_alone`). USB is saved in
    `data/fixture_candidates/sample/` (gitignored) if you want a real one.
11. **2 of 50 sampled filings parse `partial`** (`partial_causes`): PHM FY2020 is missing Item 1; NEMCL FY2021 (Newmont,
    a single registrant: an earlier version of this report wrongly called it a multi-registrant filing) is missing
    Item 7. Both are in-scope parser failures, not fixed. The 150-filing blast-radius pass below supersedes this sample.
12. Id styles of the item TOC links in the sample: 38 opaque, 9 named, 3 none (`toc_id_style`).

**Blast radius: 150 S&P 500 10-Ks, FY2020-2025 (`docs/milestones/M02-blast-radius.json`, `python -m eval.blast_radius`, read-only)**

Sample: `summary.n` 150 filings (the 50 of the earlier fixture survey plus a stratified top-up; utilities `by_group.utilities.n` 26,
financials `by_group.financials.n` 43, other 79, unknown 2). `sgml_wrapper`: 0 filings. Required items are 1, 1A, 7, 7A, 8.

| What | Value | Key |
|---|---:|---|
| Filings missing at least one required item | 14 of 150 | `summary.n_missing_any_required` |
| ...missing a core item (1, 1A, 7, 8) | 13 of 150 | `summary.n_missing_any_core` |
| Missing items by code (1, 1A, 7, 7A, 8) | 10, 6, 5, 6, 3 (total 30 of 750 required items) | `summary.missing_by_item` |
| Financials / utilities / other that miss | 7 of 43 / 2 of 26 / 5 of 79 | `summary.by_group` |
| Multi-registrant covers (more than one `dei:EntityCentralIndexKey`) | 18 of 150 (utilities 13 of 26, financials 4, other 1) | `summary.n_multi_registrant` |
| **Multi-registrant filings that miss a required item** | **0 of 18** | `summary.multi_registrant_that_miss` |
| Failing filings that are multi-registrant | 0 of 14 | `summary.failing_that_are_multi_registrant` |
| Combined "Items 1 and 2" headings | 1 | `summary.n_combined_heading` |
| Missed items by cause | heading paragraph skipped by the chain 1; only a TOC or index row recognised 12; mentioned only 4; never written 13 | `summary.by_cause` |
| Parse status | ok 137, partial 12, no_sections 1 | `summary.parse_status` |
| Text before the first detected heading, complete filings: median / max | 2.5% / 29.5% | `summary.median_share_before_first_heading_complete`, `max_...` |

Per-failure list (all 14; the last column is the share of the document that is never chunked because it precedes the first detected heading):

| ticker | FY | accession | missing | cause (per item) | text before the first heading |
|---|---:|---|---|---|---:|
| AIG | 2023 | 0000005272-24-000023 | 1, 1A | 1: toc_or_index_only, 1A: toc_or_index_only | 18.0% |
| ALL-PB | 2025 | 0000899051-26-000031 | 1 | 1: toc_or_index_only | 9.8% |
| C-PR | 2021 | 0000831001-22-000036 | 1, 1A, 7, 7A, 8 | 1: no_mention, 1A: no_mention, 7: no_mention, 7A: no_mention, 8: no_mention | 100.0% |
| EG | 2022 | 0001095073-23-000007 | 1, 7, 7A | 1: mention_only, 7: mention_only, 7A: heading_paragraph_not_chosen | 21.9% |
| HIG-PG | 2024 | 0000874766-25-000023 | 1, 7, 7A, 8 | 1: mention_only, 7: mention_only, 7A: no_mention, 8: no_mention | 9.2% |
| SYF-PB | 2020 | 0001601712-21-000057 | 1, 1A, 7, 7A, 8 | 1: toc_or_index_only, 1A: toc_or_index_only, 7: toc_or_index_only, 7A: toc_or_index_only, 8: toc_or_index_only | 1.0% |
| TFC-PR | 2024 | 0000092230-25-000020 | 7A | 7A: toc_or_index_only | 3.7% |
| HAL | 2022 | 0000045012-23-000011 | 1A, 7A | 1A: no_mention, 7A: no_mention | 14.5% |
| NEMCL | 2021 | 0001164727-22-000007 | 7 | 7: toc_or_index_only | 2.0% |
| PHM | 2020 | 0000822416-21-000007 | 1 | 1: no_mention | 10.7% |
| ROL | 2020 | 0001171200-21-000076 | 1A | 1A: no_mention | 8.1% |
| VLO | 2023 | 0001035002-24-000007 | 1 | 1: no_mention | 14.6% |
| EP-PC | 2022 | 0001506307-23-000023 | 1 | 1: no_mention | 17.7% |
| NI | 2024 | 0001111711-25-000008 | 1, 1A | 1: toc_or_index_only, 1A: toc_or_index_only | 29.8% |

What the numbers say (the first three are measured; the rest is my reading, flagged):

- **Multi-registrant co-filings are not the problem.** 18 of 150 filings have more than one cover-page registrant (half of the utilities), and
  none of the 18 misses a required item. An earlier note in this report called Newmont (NEMCL) a multi-registrant filing; that was wrong
  (one registrant) and is corrected. Recommendation: no parser work and no ADR for multi-registrant filings.
- **Missing items are a financials story:** 7 of 43 financials miss an item against 5 of 79 others and 2 of 26 utilities.
- **A missed Item 1 or 1A costs more than a label.** Text before the first detected heading is not chunked, so when Item 1 or 1A is missed
  9.8% to 29.8% of the document (the business description or the risk factors) is absent from the index for NI, EG, AIG, EP, VLO, HAL, PHM and ALL, and 100% for Citigroup;
  a complete filing loses a median of 2.5% (cover page and TOC).
- Only 1 missed item is a heading paragraph the chain skipped (Everest 7A, written after Item 8). Most are filings with no
  recognisable body `Item N` heading. I read four of them: Allstate's Item 1 heading is fused with the section's first sentence
  (`Part I Item 1. Business The Allstate Corporation was incorporated...`, so the heading is not recognised); NiSource has a title-only `Business` heading;
  AIG and Newmont show no `Item N` body heading at all. The others I did not read.
- **Decision input (yours):** (a) multi-registrant: nothing to fix. (b) A heading fused with its first sentence and an out-of-order heading are
  small, local fixes (two registered cases). (c) Title-only and cross-reference formats (13 of the 14 failing filings, so 13 of 150 in the sample;
  every failing filing except Allstate misses at least one item with no recognisable body heading; concentrated in financials) need a title-based fallback or a scoped-out ADR; they already parse `partial`, so they are visible. If you scope (c) out, the ADR should say
  that Item 1 and 1A text of those filings is not retrievable. My recommendation: fix (b) in M2 only if the labeling shows it is cheap, and scope (c) out with that sentence in the ADR.
- **Regression cases:** `tests/fixtures/known_defects/section_failures.json` registers all 14 filings (accession, missing items, cause). Two items are asserted
  with strict xfail (Allstate Item 1, Everest Item 7A); the other 13 filings' items are listed as `needs_gold_label`, because nobody has shown where they start.
  The filings are cached by `python -m eval.blast_radius`, not committed, so those tests skip on a machine without the cache.

**FinanceBench company CSV (`data/financebench_companies.csv`, evidence `docs/milestones/M02-financebench-cik-check.json`)**

I did not have the earlier prompt you mentioned, so I did this the safe way: `python -m eval.financebench_ciks` finds candidate CIKs by name from SEC's own
files (company_tickers.json and the filer names loaded into the lake), and accepts a CIK only if the submissions API shows, for every 10-K and 10-Q that FinanceBench
cites for that company, a filing of that form reported in the cited year. Result: 32 of 32 companies. 31 are verified (72 cited 10-K/10-Q documents matched).
**Foot Locker is the exception**: all its cited documents are 8-Ks, so it is accepted on an exact, unique name match (`resolved_name_only`, CIK 850209, FOOT LOCKER, INC.). The CSV is
gitignored (it lives under `data/`, as the runbook says); please look it over, especially Foot Locker, Amcor (Amcor plc, CIK 1748790) and Block (formerly Square, CIK 1512673).

**Labeling the gold set (`eval/labeling_harness.py`, template `eval/sets/drafts/sections_gold_template.jsonl`)**

10 filings, 50 rows (5 required items each), chosen by rule (smallest first, one per company) from the blast-radius sample: 2 no-anchor TOC (USB, Progressive), 2 named-id
TOC (Ulta, Generac), 2 opaque-id TOC (IBM, BNY), 2 multi-registrant (United Airlines, Federal Realty) and 2 with items the parser misses (NiSource, Rollins). Strata are in
`sections_gold_template.strata.json`; the rows hold no parser output, the harness imports nothing from the parser (a test enforces it), and candidates are only
line-start regex hits of `Item N` with character offsets in the plain-text file `data/fixture_candidates/labeling/<ticker>_<accession>.txt` (gitignored scratch).

1. For each row open the text file. Set `present` (true, or false if the filing has no such section) and `true_start_offset`, the offset of the line where the section really starts
   (copy one of `candidates[].offset` or use any other offset; Rollins has no `Item 1A` text, so you will choose by title).
2. `python -m eval.labeling_harness fill` proposes `first_sentence` from your offset (confirm or retype it); put your name in `labeled_by`.
3. `python -m eval.labeling_harness check` lists rows that are not ready (unset `present`, bad offset, a sentence that is not in the text, a blank `labeled_by`).
4. Drop rows with `present: false`, copy the file to `eval/sets/sections_gold.jsonl` yourself (a hook blocks me from writing there), then `make gold-checksums`, `python -m eval.sections_eval`, `/dod M2`.

Heads-up for labeling: USB's Item 1A says it is incorporated by reference to the annual report (visible in its candidate context), so that section is one short sentence, which is still a valid label. IBM and BNY are also short documents (101,835 and 63,381 characters in `sections_gold_template.strata.json`); I did not check whether they do the same. 50 rows is half of the 100
the runbook asks for; run `prepare` with other `--multi/--none/--named/--opaque` counts for more, or label 10-Qs separately. `sections_eval` now refuses a blank `first_sentence` (it would match every start).

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

1. ~~Three real fixture filings~~ **Done** (chosen by you, promoted 2026-11-17): Apple 10-K FY2024 (opaque-id anchor
   TOC), Ulta 10-K FY2024 (named-id anchor TOC, label offset -1) and Apple 10-Q Q3 FY2026, each with a verified sidecar.
   Still open: a fixture for the in-scope case where the TOC is not linked on the item text (see Open issues 10).
2. **`eval/sets/sections_gold.jsonl`** (100 labels): a labeling template is ready, see "Labeling the gold set" below.
   Then `make gold-checksums`, `python -m eval.sections_eval --min-accuracy 0.9`, and `/dod M2`.
3. ~~`data/financebench_companies.csv`~~ **Done, please review** (see "FinanceBench company CSV" below).
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
