# M2 Definition-of-Done report (local half measured; owner inputs and cloud half outstanding)

Generated 2026-11-17 by following `.claude/skills/dod/SKILL.md`. Numbers are copied from the files named
below; a number with no file is marked `NOT MEASURED`. Where a number comes from a command run for this
report, the command is shown. Everything measured here describes the **135 real filings fetched
locally** (5 CIKs: Apple, Microsoft, Amazon, JPMorgan, Berkshire; `data/raw/filings`), not the 13,239-filing
target list, and the 84 FinanceBench PDFs.

## Verdict

**M2 is NOT done.** Three items FAIL or cannot be run, for reasons that are not code defects:

1. **Section-boundary accuracy is measured on n=50, not the runbook's 100 (a conscious speed-run deviation): 47/50 = 94.0% after
   correcting two gold-anchor errors; 45/50 = 90.0% as first scored.** Both numbers, the reason and the CIs are in item 2b. With n=50 the
   result cannot confirm ≥ 90% (filing-level 95% CI 86.0% to 100.0%; lower bound under the target). The 3 remaining misses are parser errors.
2. ~~The three real fixture filings do not exist~~ Resolved: three real filings are in `tests/fixtures/filings/` and
   `tests/unit/parsing/test_real_fixtures.py` runs on them (10 passed): core sections found, chunks clean, sidecars equal the
   filings' own `dei` tags, and the parsed fiscal labels equal the company's own (Ulta needs offset -1).
3. ~~The FinanceBench rows were not produced~~ Resolved: `data/financebench_companies.csv` now exists (resolved and
   verified against the SEC submissions, see below) and `make parse-filings FB=1` loaded the PDFs into the local lake.
   Please review the CSV (it is gitignored under `data/`; the evidence is committed).
4. The full fetch and parse of the 13,239 targets is the cloud sprint (nothing was applied, pushed or run in AWS).

Two checks FAIL on what was measured: strict parse success is 114 of 135 filings `ok` (84.4%; target ≥ 95%; it was 93 of 135, 68.9%,
before the Berkshire fix), and the 20 random chunks did not all read cleanly. Causes are listed under "Open issues".

## Checklist (M02 runbook section 6)

| # | Item | Status | Evidence |
|---|---|---|---|
| 1a | `filings.csv` counts recorded | PASS | `make list-filings` style run: `python -m pipelines.fetch.list_filings --keep-going --summary-out docs/milestones/M02-targets.json` → "wrote 13239 targets", 500 of 500 CIKs listed, 0 failed. Counts in the numbers table. |
| 1b | fetch complete | NOT RUN | Only 135 of 13,239 targets were fetched, locally, to test the fetcher (135 fetched, 0 failed; `docs/milestones/M02-parse-metrics.json` `html.n_filings`). The full fetch belongs on an AWS box (cloud sprint). |
| 1c | `raw.filings_html` has one row per target | NOT RUN | 135 rows for the 135 fetched (command: Spark `count()` on `lake.raw.filings_html`, all HTTP 200). One row per fetched filing holds; for the 13,239 targets it is not run. |
| 2a | Parse success ≥ 95% (or documented) | **FAIL** (documented) | 114 `ok` + 21 `partial` of 135 = 84.4% strict, 0 `error`, 0 `no_sections` (`html.status_by_form`: 10-K ok 32, 10-Q ok 82, 10-Q partial 21). All 21 `partial` are JPMorgan 10-Qs (CIK 19617), a cross-reference format with no Item 1 or Item 2 heading in the body: ADR-028, not fixed. The 21 Berkshire 10-Qs that were `partial` (P1-2 missing) are fixed: the running-header filter dropped the repeated `Item 2. Management's Discussion ...` page header together with the real heading (`html_blocks._drop_noise`; regression tests in `tests/unit/parsing/test_berkshire_item2_regression.py`). The 32 10-Ks all parsed `ok`. 95% is not reachable on this 5-CIK set without the JPMorgan format: 21 of 135 (15.6%) are JPMorgan 10-Qs. |
| 2b | Section-boundary accuracy ≥ 90% on the 100 labels | **PASS on the point estimate at n=50, not confirmed: the CI lower bound is under 90%** | `python -m eval.sections_eval --min-accuracy 0.9` (`docs/milestones/M02-sections-eval.json`). **n=50, half the runbook's 100: a deliberate deviation (speed run); read every number below as n=50.** **As first scored: 45/50 = 90.0%** (filing-level 95% CI 82.0% to 98.0%; label-level 80.0% to 98.0%; item 7 was 7/10). **After correcting two gold-anchor errors: 47/50 = 94.0%** (filing-level 95% CI 86.0% to 100.0%; label-level 86.0% to 100.0%; 10,000 resamples; items 1 9/10, 1A 9/10, 7 9/10, 7A 10/10, 8 10/10). The two corrections, made after the first score and from the raw text only (the parser's output was not consulted for the new anchors), both construction bugs in my anchors, not parser behaviour: PGR 7's anchor was a mid-sentence fragment (the raw text breaks `Incorporated by reference from / Management's Discussion and Analysis… / in our Annual Report.` across three text nodes); ULTA 7's anchor was the 85-character heading title `Management's Discussion and Analysis…`, which belongs to the `Item 7.` heading and should have been skipped. The 3 remaining misses are genuine parser errors: NI 7 (the parser's first chunk starts about 4,850 characters after the labeled start, after a `(continued)` page header), ROL 1 (parser's Item 1 chunk starts at the risk-factor text) and ROL 1A (`section not found`); the last two are on Rollins' dotted `Item 1.A.` spelling (cause not verified). **n=50 cannot confirm ≥ 90%: both lower CI bounds (82.0% first scored, 86.0% corrected) are under the target, so the sample cannot show ≥ 90% with confidence, whatever the point estimate says.** The sample is 10 filings x 5 items (1, 1A, 7, 7A, 8) from the blast-radius cache of S&P 500 10-Ks, not the 135 locally fetched filings: 2 opaque-id TOC (IBM, BNY-PK), 2 named-id TOC (ULTA, GNRC), 2 no-anchor TOC (USB-PS, PGR), 2 multi-registrant (UAL, FRT-PC) and 2 hard filings chosen because the parser misses an item (NI, ROL) (`eval/sets/drafts/sections_gold_template.strata.json`). It is a stratified sample, so its accuracy is NOT an estimate for the whole corpus, and 2 of the 10 filings were picked because the parser fails on them. The template held only regex hits, never parser output (`tests/unit/test_labeling_harness.py` enforces it); Rollins' `Item 1.A.` heading is not matched by that regex, so ROL 1A was labeled by hand at offset 20353. Labeled by hand by Shekar; I (Claude) displayed the raw text, rebuilt 7 line-wrapped first sentences from the raw text and, afterwards, the two corrections above. |
| 3a | Chunk count and token histogram recorded | PASS | `html.n_chunks`, `html.token_histogram`, `html.max_tokens` in `M02-parse-metrics.json`; the Spark job's table agrees: `lake.curated.chunks` 38922 rows (command output). |
| 3b | Property tests green | PASS | `pytest tests/unit/parsing/test_chunker.py` → 19 passed (no chunk over 400 tokens, overlap ≤ 80, chunks reproduce the text, every chunk adds text). `make test` → 1292 passed, 0 skipped. |
| 4 | 20 random chunks read cleanly (notes in the parse report) | **FAIL** | Read by Claude, not Shekar (seed 20261117, `html.random_chunks`). No hidden XBRL, no repeated page headers, tables keep their header and rows. But 7 of 20 sit under a section label that does not describe them, and 3 more have noise. Details under Open issues; Shekar should re-read. |
| 5 | 50/50 source URLs return 200 | PASS (documents) / NOT RUN (anchors) | `python -m eval.check_source_urls --rate 2` → "32/32 documents returned 200 (50 sampled chunk URLs)". The fragment (`#id` or `#:~:text=`) is never seen by a server: the 5-in-a-browser spot check is **NOT RUN** (Shekar). |
| 6a | All FinanceBench PDFs parsed with page numbers (extraction) | PASS | `financebench.parse_status` `{"ok": 84}`, `pages_with_text` 11948, every chunk has `page` (`docs/milestones/M02-parse-metrics.json`, measured with a placeholder CIK; superseded by 6b). |
| 6b | FinanceBench rows in `curated.chunks` | PASS | `make parse-filings FB=1` (command output): `curated.filings` 219 rows = 135 `edgar_html` + 84 `financebench_pdf` (all `ok`, 32 distinct CIKs, none <= 0); `curated.chunks` 68542 rows = 38922 + 29620, the PDF chunks all with `page` (1 to 549) and at most 400 tokens. A real relative-path bug in `parse_filings` surfaced on this run and was fixed with a regression test. |

### Other checks run

| Check | Status | Evidence |
|---|---|---|
| Spark vs pure-Python equivalence (mandatory) | PASS | `pytest tests/integration/test_parse_filings.py …` → 14 passed. Mutating the Spark join (`period_of_report` without the `sub.period` fallback) made `test_the_spark_output_equals_the_pure_python_parse_for_every_filing` fail; restoring it passed. On the real data both paths give 38922 chunks (`html.n_chunks`; `lake.curated.chunks`). |
| Text and facts agree on fiscal labels (ADR-023) | PASS (35 of 135 comparable) | `docs/milestones/M02-fiscal-agreement.json`: `with_facts_compared` 35, `agree` 35, `mismatches` `[]`. 100 filings have no facts in the lake (only 7 FSDS quarters are loaded), so they are not compared. |
| `make test` / `make test-integration` / `make lint` | PASS | `make test` 1292 passed; `make test-integration` 352 passed (both after the parser fixes); lint: "All checks passed", mypy clean. |
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
| `ok` 10-K / `ok` 10-Q / `partial` 10-Q | 32 / 82 / 21 | same | `html.status_by_form` |
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

1. **JPMorgan 10-Qs (21 filings) are `partial`** (`html.non_ok`): JPMorgan's 10-Q has no
   `Item 1/2` headings in the body (cross-reference format), so `P1-1` and `P1-2` are missing (ADR-028).
   **Berkshire's 21 10-Qs were `partial` too and are fixed** (2026-11-20): the MD&A heading `Item 2. Management's Discussion ...`
   is also the page header of every MD&A page (18 copies), and the repeated-line filter dropped all of them; the first copy of a
   repeated item heading that has a title is now kept. No company-specific rule was added. Consequence for JPMorgan: its whole
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

**Blast radius and the two parser fixes: 150 S&P 500 10-Ks, FY2020-2025 (`docs/milestones/M02-blast-radius.json` after, `M02-blast-radius-before-fix.json` before; `python -m eval.blast_radius`)**

Sample: `summary.n` 150 filings (the 50 of the earlier survey plus a stratified top-up: utilities 26, financials 43, other 79, unknown 2); `sgml_wrapper` 0. Required items: 1, 1A, 7, 7A, 8.

| What | Before the fixes | After the fixes | Key |
|---|---:|---:|---|
| Filings missing at least one required item | 14 of 150 | **13 of 150** | `summary.n_missing_any_required` |
| ...missing a core item (1, 1A, 7, 8) | 13 | 12 | `summary.n_missing_any_core` |
| Missing by item 1 / 1A / 7 / 7A / 8 | 10 / 6 / 5 / 6 / 3 | 9 / 6 / 5 / 5 / 4 | `summary.missing_by_item` |
| Financials / utilities / other that miss | 7 of 43 / 2 of 26 / 5 of 79 | 6 of 43 / 2 of 26 / 5 of 79 | `summary.by_group` |
| Causes: heading paragraph skipped / TOC or index row only / mentioned only / never written | 1 / 12 / 4 / 13 | 0 / 11 / 5 / 13 | `summary.by_cause` |
| Parse status ok / partial / no_sections | 137 / 12 / 1 | 138 / 11 / 1 | `summary.parse_status` |
| Multi-registrant covers / of them that miss an item | 18 / 0 | 18 / 0 | `summary.n_multi_registrant`, `multi_registrant_that_miss` |
| Text before the first heading, complete filings: median / max | 2.5% / 29.5% | 2.5% / 29.5% | `summary.median_share_before_first_heading_complete` |

**The two fixes** (`pipelines/parsing/sections.py`, `document.py`; each has unit tests, a test on the real filing, and a mutation check that fails without the fix):

1. **Allstate Item 1** (`0000899051-26-000031`): the heading is fused with the section's first sentence (`Part I Item 1. Business The Allstate Corporation was incorporated ...`). A known item title followed by a new sentence is now a heading, and the sentence becomes the section's first chunk text. The filing went from `partial` to `ok`.
2. **Everest Item 7A** (`0001095073-23-000007`): **my earlier description ("an out-of-order heading") was wrong.** The filing has `ITEM 8,` as the first fragment of a cross-reference inside Item 7 (`ITEM 8, "Financial Statements and Supplementary Data" - Notes 1 and 3`). The parser took it as the Item 8 heading, and since it sits before the real Item 7A that heading lost the section order. A heading that is only `ITEM N,` is now a cross-reference fragment, not a heading. Everest's 7A is found. Its old "Item 8" was that fragment, so the earlier "Item 8 found" was a false positive: Everest still misses 1, 7 and 8 (the real Item 8 heading is not recognised).

**A regression caught on the way:** the first version of the comma rule rejected every heading ending in a comma, which dropped PPL's Item 5 and Nasdaq's Item 13 (their titles wrap onto the next line, `ITEM 5. MARKET FOR THE REGISTRANT'S COMMON EQUITY,`). I found it by diffing the 150 filings before and after, narrowed the rule to "code and comma, no title", and added a test. After that, only Allstate and Everest changed in the whole sample, and the 135 local filings are identical in every number (`n_chunks` 38922, status counts, token histogram, chunks per section), so the committed parse metrics stay valid.

**Not touched, as instructed:** the title-only and cross-reference formats. They are scoped out in **ADR-026** (drafted, consequences left to you): their Item 1 and 1A text is not retrievable. Of the 13 remaining filings, the share of the document before the first detected heading (never chunked) is in the last column:

| ticker | FY | accession | missing | cause (per item) | text before the first heading |
|---|---:|---|---|---|---:|
| AIG | 2023 | 0000005272-24-000023 | 1, 1A | 1: toc_or_index_only, 1A: toc_or_index_only | 18.0% |
| C-PR | 2021 | 0000831001-22-000036 | 1, 1A, 7, 7A, 8 | 1: no_mention, 1A: no_mention, 7: no_mention, 7A: no_mention, 8: no_mention | 100.0% |
| EG | 2022 | 0001095073-23-000007 | 1, 7, 8 | 1: mention_only, 7: mention_only, 8: mention_only | 21.9% |
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

Sector skew: financials 6 of 43 after the fixes (7 of 43 before), against utilities 2 of 26 and other 5 of 79. Citigroup (`C-PR`) is the wholesale case: `no_sections`, 100% of the document unindexed. Multi-registrant co-filings are not the problem (0 of 18 miss an item); an earlier note that called Newmont multi-registrant was wrong and is corrected.

**Regression cases:** `tests/fixtures/known_defects/section_failures.json` has `fixed` (Allstate Item 1, Everest 7A; strict-xfail markers removed, now ordinary tests that run when the filings are cached by `eval.blast_radius`) and `cases` (13 filings, every missing item `needs_gold_label`: nobody has shown where it starts, so none is asserted).

**FinanceBench company CSV (`data/financebench_companies.csv`, evidence `docs/milestones/M02-financebench-cik-check.json`)**

I did not have the earlier prompt you mentioned, so I did this the safe way: `python -m eval.financebench_ciks` finds candidate CIKs by name from SEC's own files and accepts a CIK only if the submissions show, for every 10-K and 10-Q FinanceBench cites for that company, a filing of that form reported in the cited year. Result: `n_resolved` 32 of `n_companies` 32; 31 verified against 72 cited documents; Foot Locker accepted on an exact unique name (`resolved_name_only`) because it cites only 8-Ks. The CSV is gitignored (under `data/`, as the runbook says); the evidence is committed.

**Re-verification of the three flagged companies** (`python -m eval.financebench_recheck`, results under `recheck` in the evidence file). For each cited PDF it reads the cover page and checks it against that CIK's full submissions (all forms): the filing by the cover's EXACT period-end or event date, the SEC file number, and the registrant name against the name in force on the filing date (current name and `formerNames` with their from/to dates).

- **Block (CIK 1512673): confirmed.** Both cited 10-Ks (`0001628280-17-001754` filed 2017-02-27, `0001512673-21-000008` filed 2021-02-23) were filed under the name **Square, Inc.** (a former name in force 2011-03-02 to 2021-12-08; the entity is Block, Inc. since), exact period ends match, file number 001-37622 matches. The cover says SQUARE, INC., which is the correct name for those dates.
- **Amcor (CIK 1748790, Amcor plc): confirmed.** The SEC entity's only former name is `ARCTIC JERSEY Ltd` (to 2018-10-12); this CIK has the cited FY2020 and FY2023 10-Ks, the Dec 2022 10-Q and the 2022-07-01 8-K, each matched by exact date. I did not find an "Amcor Limited" registrant under this CIK and did not search for one: the cited documents are Amcor plc's. `AMCOR_2023Q4_EARNINGS` is an earnings release with no date on its cover and stays unchecked. **One real discrepancy, explained:** the FY2020 10-K's cover prints commission file number **001-36786**, while SEC's metadata for that filing says 001-38932. I read the original filing on sec.gov: it prints 001-36786 too, and the PDF's registrant, period end and file number equal the original's, so the PDF is a faithful copy and the difference is in the filing's own cover, not a wrong match (status `confirmed_pdf_matches_original_filing`).
- **Foot Locker (CIK 850209): confirmed, no longer name-only.** Both cited 8-Ks are found by exact filing date (`0001539497-22-001037` on 2022-05-20, `0001539497-22-001461` on 2022-08-19), the cover file number 1-10299 matches SEC's 001-10299, and the name in force is FOOT LOCKER, INC. The submissions also show 10-K filings for every fiscal year from `10k_report_dates` 2013-02-02 to 2025-02-01 (13 annual reports). The main evidence entry keeps its original `resolved_name_only` status; the `recheck` block is the upgrade.

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

Heads-up for labeling: USB's Item 1A says it is incorporated by reference to the annual report (visible in its candidate context), so that section is one short sentence, which is still a valid label. IBM and BNY are also short documents (101,835 and 63,381 characters in `sections_gold_template.strata.json`); I did not check whether they do the same. **50 rows is half of the 100 the runbook asks for: a conscious speed-run deviation**, recorded here and in the DoD checklist so the result is reported as n=50; run `prepare` with other `--multi/--none/--named/--opaque` counts for more, or label 10-Qs separately. `sections_eval` now refuses a blank `first_sentence` (it would match every start).

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
2. ~~`eval/sets/sections_gold.jsonl`~~ **Done** (n=50, half the runbook's 100: a conscious deviation): 47/50 = 94.0% (45/50 = 90.0% as first scored), see item 2b.
3. ~~`data/financebench_companies.csv`~~ **Done, please review** (see "FinanceBench company CSV" below).
4. **The 5-in-a-browser spot check** of citation fragments (items from `html.url_sample`).
5. **Cloud sprint, in order** (all yours; nothing was run): `terraform plan/apply` (new `ecr` and `emr` modules);
   `make emr-image-build`, `make emr-image-push ECR_URL=… YES=1`; set `emr_image_uri` and apply again; `make emr-package
   UPLOAD=s3://<lake>/artifacts` (from a clean tree); fetch the 13,239 filings from an AWS box
   (`make fetch-filings DEST=s3://<lake>/raw/filings`); `make emr-run JOB=load_filings_html …` then `JOB=parse_filings`.

## Decisions that need an ADR (or your sign-off)

- **ADR-023, ADR-024, ADR-025 and ADR-026 are drafted** (`docs/decisions.md`, status `proposed`, consequences left to you): fiscal labels, the EMR runtime, pre-2020 SGML filings out of scope, and title-only/cross-reference headings out of scope.
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
