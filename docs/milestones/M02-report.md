# M2 Definition-of-Done report (full corpus measured on AWS Glue, 2026-11-21; owner checks outstanding)

Updated 2026-11-21 by following `.claude/skills/dod/SKILL.md`. Numbers are copied from the files named below; a number
with no file is marked `NOT MEASURED`. **Full-corpus results (13,239 filings, 500 companies, 84 FinanceBench PDFs) are in
the section "Full corpus on AWS Glue" and in the checklist rows marked "full corpus".** The older measurements on the 135
filings fetched locally (5 CIKs: Apple, Microsoft, Amazon, JPMorgan, Berkshire) stay in the text, labelled "local set".

## Verdict

**M2 is NOT done.** What is complete: the fetch (13,239 of 13,239, 0 failed), the load (`raw.filings_html`, 13,239 rows), the
parse of the whole corpus into the real `curated.filings` / `curated.chunks`, FinanceBench in the real `curated`, the 50/50 URL
check, and `curated.companies.in_financebench`. What blocks "done":

1. **Parse success FAILS the 95% target on the full corpus, out-of-sample. After the ADR-031 parser fix and a full re-parse: 11,720 of 13,239 filings `ok` = 88.5% strict** (before the fix: 11,178 = 84.4%; `ok` + `partial` 99.06%; 125 `no_sections`, 0 `error`). The remaining gap is documented, not fixed (ADR-031 is the last parser fix for M2).
2. **The Part I miss was far bigger than JPMorgan and mostly not JPMorgan's format.** Before the fix, 78 of the 500 companies (15.6%) had 10-Qs missing both `P1-1` and `P1-2` (1,226 filings). ADR-031 (contents table no longer moves the part; `P1-1` implied when the body has no `Item 1` heading) recovered 542 filings to `ok` (421 of the 1,226), with **0 downgrades and 0 previously-ok filings changed on the whole corpus**. 14 of the 78 companies are fully recovered and 36 have at least one filing recovered. **Residual: 1,135 10-Qs in 83 companies are still `partial`**: 554 have no body heading for the missing item (the ADR-028 format; JPMorgan and Colgate 21 of 21 each, unchanged) and 581 have a heading the chain did not choose (409 of them a one-row contents-table chain). See "After ADR-031: the re-parse".
3. The section-boundary accuracy (item 2b) is n=50 and in-sample (see that row), and the 5-in-a-browser fragment check is NOT RUN (Shekar).
4. **`curated.chunks` is not clean: the ADR-031 re-parse timed out at the 90-minute cap (5,404 s, 54,048 DPU-seconds) in the last statement of `merge_curated`, so 51,523 stale chunks remain in 440 filings** (details and the cause below). `curated.filings` was fully updated. Not retried, as instructed.
5. Draft ADR Consequences (026-031) await your review; ADR-028's revisit trigger has fired (noted in ADR-028) and ADR-031 is written.

Earlier points, kept: the three real fixture filings exist (`tests/fixtures/filings/`, `test_real_fixtures.py`, 10 passed);
`data/financebench_companies.csv` exists (gitignored; evidence committed; please review).

## Checklist (M02 runbook section 6)

| # | Item | Status | Evidence |
|---|---|---|---|
| 1a | `filings.csv` counts recorded | PASS | `make list-filings` style run: `python -m pipelines.fetch.list_filings --keep-going --summary-out docs/milestones/M02-targets.json` → "wrote 13239 targets", 500 of 500 CIKs listed, 0 failed. Counts in the numbers table. |
| 1b | fetch complete | **PASS (full corpus)** | `python -m pipelines.fetch.fetch_filings --dest s3://edgarqa-lake-019593222039/raw/filings --workers 6` (local process, refreshable AWS credentials, SEC rate limit 8 req/s through `SecClient`): `fetched 13104, skipped 135, failed 0; 2695779692 bytes stored this run`. S3 listing of `raw/filings/`: 13,239 `.htm.gz` objects (plus `_manifest.jsonl`), 2,741,761,733 bytes; compared with `data/targets/filings.csv`: 13,239 targets, 0 missing, 0 extra. Manifest: 13,239 rows, all `http_status` 200, 0 with zero bytes. (Local set, 2026-11-17: 135 of 13,239.) |
| 1c | `raw.filings_html` has one row per target | **PASS (full corpus)** | `edgarqa-load-filings-html` (Glue, SUCCEEDED). Athena: `select count(*), count(distinct accession), count_if(http_status=200), count(distinct cik) from raw.filings_html` -> 13239, 13239, 13239, 500. |
| 2a | Parse success ≥ 95% (or documented) | **FAIL (documented), full corpus** | **After ADR-031 and the full re-parse (`docs/milestones/M02-adr031-reparse.json`): 11,720 `ok`, 1,394 `partial`, 125 `no_sections` of 13,239 -> strict 88.5% (`html.strict_after` 0.8853), `ok`+`partial` 99.06%; still below 95%.** The figures below are the pre-fix measurement (84.4%). **Full corpus (`curated.filings`, `edgar_html`, `docs/milestones/M02-fullcorpus-metrics.json`): 11,178 `ok`, 1,935 `partial`, 126 `no_sections`, 0 `error` of 13,239 -> strict 84.4% (`html.strict_success` 0.8443), `ok`+`partial` 99.05%.** By form: 10-K+10-K/A 2,814 ok of 3,098 (90.8%); 10-Q+10-Q/A 8,364 ok of 10,141 (82.5%). `no_sections`: 24 10-K, 1 10-K/A, 91 10-Q, 10 10-Q/A; 162 filings have no chunk at all. 95% is not met and not fixed; the cause is the Part I miss on 1,676 10-Qs (all of them `partial`), see the full-corpus section. *Local set (135 filings, 2026-11-20): 114 ok + 21 partial = 84.4%; all 21 partial were JPMorgan 10-Qs (ADR-028); the 21 Berkshire 10-Qs fixed earlier (`html_blocks._drop_noise`, `test_berkshire_item2_regression.py`).* |
| 2b | Section-boundary accuracy ≥ 90% on the 100 labels | **PASS at n=50 (49/50 = 98.0%, filing-level CI lower bound 94.0%); in-sample, because the parser fix that raised it targeted this set's misses** | `python -m eval.sections_eval --min-accuracy 0.9` (`docs/milestones/M02-sections-eval.json`). **n=50, half the runbook's 100: a deliberate deviation (speed run); read every number below as n=50.** **As first scored: 45/50 = 90.0%** (filing-level 95% CI 82.0% to 98.0%; label-level 80.0% to 98.0%; item 7 was 7/10). **After correcting two gold-anchor errors: 47/50 = 94.0%** (filing-level 95% CI 86.0% to 100.0%; label-level 86.0% to 100.0%; 10,000 resamples; items 1 9/10, 1A 9/10, 7 9/10, 7A 10/10, 8 10/10). The two corrections, made after the first score and from the raw text only (the parser's output was not consulted for the new anchors), both construction bugs in my anchors, not parser behaviour: PGR 7's anchor was a mid-sentence fragment (the raw text breaks `Incorporated by reference from / Management's Discussion and Analysis… / in our Annual Report.` across three text nodes); ULTA 7's anchor was the 85-character heading title `Management's Discussion and Analysis…`, which belongs to the `Item 7.` heading and should have been skipped. **After the dotted-spelling parser fix (ADR-029; gold unchanged, the score rose because the parser improved): 49/50 = 98.0%** (filing-level 95% CI 94.0% to 100.0%; label-level 94.0% to 100.0%; items 1, 1A, 7A, 8 at 10/10, item 7 at 9/10); both lower CI bounds of the earlier scores stay in the table above as first measured. The one remaining miss, NI 7, is ADR-030. Before the fix the 3 misses were genuine parser errors: NI 7 (the parser's first chunk starts about 4,850 characters after the labeled start, after a `(continued)` page header), ROL 1 (parser's Item 1 chunk starts at the risk-factor text) and ROL 1A (`section not found`); the last two are on Rollins' dotted `Item 1.A.` spelling (cause not verified). **At n=50 the first two scores could not confirm ≥ 90% (lower CI bounds 82.0% first scored, 86.0% corrected, both under the target). The 49/50 score's lower bound (94.0%) clears it, but only in-sample: the dotted-spelling fix (ADR-029) was written after seeing ROL 1 and ROL 1A fail on this set, and the sample is stratified, not random.** The sample is 10 filings x 5 items (1, 1A, 7, 7A, 8) from the blast-radius cache of S&P 500 10-Ks, not the 135 locally fetched filings: 2 opaque-id TOC (IBM, BNY-PK), 2 named-id TOC (ULTA, GNRC), 2 no-anchor TOC (USB-PS, PGR), 2 multi-registrant (UAL, FRT-PC) and 2 hard filings chosen because the parser misses an item (NI, ROL) (`eval/sets/drafts/sections_gold_template.strata.json`). It is a stratified sample, so its accuracy is NOT an estimate for the whole corpus, and 2 of the 10 filings were picked because the parser fails on them. The template held only regex hits, never parser output (`tests/unit/test_labeling_harness.py` enforces it); Rollins' `Item 1.A.` heading is not matched by that regex, so ROL 1A was labeled by hand at offset 20353. Labeled by hand by Shekar; I (Claude) displayed the raw text, rebuilt 7 line-wrapped first sentences from the raw text and, afterwards, the two corrections above. |
| 3a | Chunk count and token histogram recorded | **PASS (full corpus)** | *Measured before the ADR-031 re-parse; not re-measured: `curated.chunks` now holds 51,523 stale chunks (see the re-parse section).* `chunks` in `M02-fullcorpus-metrics.json`: `edgar_html` 2,794,994 chunks in 13,077 filings (951,757 table chunks, 892,778 with an element anchor), `financebench_pdf` 29,620 in 84; `max_tokens` 400, `over_400` 0 for both. HTML histogram (bins 1-50 ... 351-400): 306677, 333529, 269585, 204364, 161506, 139437, 203262, 1176634 (`token_histogram`). Cross-check with Athena `select max(n_tokens), count_if(n_tokens>400) from curated.chunks` -> 400, 0. (Local set: 38922 chunks, `M02-parse-metrics.json`.) |
| 3b | Property tests green | PASS | `pytest tests/unit/parsing/test_chunker.py` → 19 passed (no chunk over 400 tokens, overlap ≤ 80, chunks reproduce the text, every chunk adds text). `make test` → 1292 passed, 0 skipped. |
| 4 | 20 random chunks read cleanly (notes in the parse report) | **PASS on garbling (0 of 20); 1 section mislabel and 4 minor notes** | *Sample drawn before the ADR-031 re-parse.* Full corpus, seed 20261121, chunks ordered by md5(chunk_id+seed) (`chunks_20` in `M02-fullcorpus-metrics.json`), all 20 read in full by Claude, not Shekar. **No hidden XBRL / number soup, no broken tables (pipe rows keep header and values), no repeated page headers.** Notes: #12 (`0001396009-23-000042#P2-6#0056`, a 10-Q) is environmental-remediation text under `P2-6` (Exhibits): the whole filing, 164 chunks / 168,300 characters, is under `P2-6` (the Part I miss below); #7, #8, #13 are tables whose caption is in another chunk (no label for what they show); #20 ends with a sentence introducing a table that is in the next chunk; #2 ends with the next statement's title. #19 is JPMorgan exhibit text and is correctly under `P2-6`. Shekar should re-read. (Local set: 7 of 20 mislabelled; see Open issues.) |
| 5 | 50/50 source URLs return 200 | **PASS (documents, full corpus) / NOT RUN (anchors)** | `python -m eval.check_source_urls --metrics docs/milestones/M02-fullcorpus-metrics.json --rate 2 --out docs/milestones/M02-fullcorpus-url-check.json` -> "50/50 documents returned 200 (50 sampled chunk URLs)" (`ok` 50, `failed` [], `rate_per_second` 2.0). The fragment (`#id` / `#:~:text=`) is never seen by a server: the 5-in-a-browser check is **NOT RUN** (Shekar). (Local set: 32/32.) |
| 6a | All FinanceBench PDFs parsed with page numbers (extraction) | PASS | `financebench.parse_status` `{"ok": 84}`, `pages_with_text` 11948, every chunk has `page` (`docs/milestones/M02-parse-metrics.json`, measured with a placeholder CIK; superseded by 6b). |
| 6b | FinanceBench rows in `curated.chunks` | **PASS (real `curated`, Glue)** | Full-parse run `jr_af891577...` wrote the 84 PDFs into the real `curated` (not smoke). Athena: `curated.filings` `financebench_pdf` 84 `ok`; `curated.chunks` `financebench_pdf` 29,620 chunks, 84 docs, `with_page` 29,620 (no NULL), max 400 tokens, 0 over, 0 table chunks. Identical to the local result (`M02-parse-metrics.json` `financebench`). `curated.companies.in_financebench`: 32 true (= 32 distinct ciks of the FinanceBench documents), 0 NULL; 30 of them are in the S&P 500 (the other two, Foot Locker and Activision Blizzard, are not in the CSV). |

### Other checks run

| Check | Status | Evidence |
|---|---|---|
| Spark vs pure-Python equivalence (mandatory) | PASS | `pytest tests/integration/test_parse_filings.py …` → 14 passed. Mutating the Spark join (`period_of_report` without the `sub.period` fallback) made `test_the_spark_output_equals_the_pure_python_parse_for_every_filing` fail; restoring it passed. On the real data both paths give 38922 chunks (`html.n_chunks`; `lake.curated.chunks`). |
| Text and facts agree on fiscal labels (ADR-023) | PASS (35 of 135 comparable) | `docs/milestones/M02-fiscal-agreement.json`: `with_facts_compared` 35, `agree` 35, `mismatches` `[]`. 100 filings have no facts in the lake (only 7 FSDS quarters are loaded), so they are not compared. |
| `make test` / `make test-integration` / `make lint` | PASS (lint: 2 known) | 2026-11-21 at HEAD: `make test` 1418 passed; `make test-integration` 371 passed; `make lint`: 2 `I001` import-order errors in `tests/integration/test_differential_duckdb.py` and `tests/unit/test_fiscal_duckdb.py` (older files, not touched here). |
| `make check-gold` | PASS | "check-gold: 1 gold file(s) match CHECKSUMS" (2026-11-21). |
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
| Section-boundary accuracy (n=50, in-sample) | 49/50 = 98.0% | `docs/milestones/M02-sections-eval.json` | see item 2b |
| **Full corpus (2026-11-21)** | | | |
| Fetched / skipped / failed; bytes stored this run | 13104 / 135 / 0; 2695779692 | fetch command output (log in the session scratchpad) | `fetched`, `skipped`, `failed` |
| S3 `raw/filings` `.htm.gz` objects / targets / missing / extra | 13239 / 13239 / 0 / 0 | S3 listing vs `data/targets/filings.csv` | n/a (command output) |
| `raw.filings_html` rows / distinct accession / HTTP 200 / ciks | 13239 / 13239 / 13239 / 500 | Athena | n/a (command output) |
| HTML filings `ok` / `partial` / `no_sections` / `error` | 11178 / 1935 / 126 / 0 | `docs/milestones/M02-fullcorpus-metrics.json` | `html.ok`, `html.partial`, `html.no_sections` |
| Strict parse success / `ok`+`partial` | 0.8443 / 0.9905 | same | `html.strict_success`, `html.lenient_success_ok_plus_partial` |
| Chunks `edgar_html` / `financebench_pdf` | 2794994 / 29620 | same | `chunks[].chunks` |
| Max tokens / chunks over 400 | 400 / 0 | same | `chunks[].max_tokens`, `over_400` |
| 10-Qs missing both `P1-1` and `P1-2`: filings / companies / companies with every 10-Q affected | 1226 / 78 / 46 | same | `cross_reference_10q_both_p1_missing.filings`, `.companies`; 46 = count of `per_company` rows with `n_both_missing == n_10q` |
| 10-Qs with >80% of text under `P2-6`: filings / companies | 690 / 50 | same | `p2_6_dominant_10q.n_over_80pct_p2_6`, `.companies` |
| 10-Ks with >50% / >80% of text under Item 15 (of 3062 with chunks) | 54 / 41 | same | `item15_dominant_10k` |
| Source URLs returning 200 | 50 of 50 | `docs/milestones/M02-fullcorpus-url-check.json` | `ok`, `distinct_documents` |
| DPU-seconds: manifest load / full parse / stray parse (stopped) / companies reruns | 153 / 8178 / 9327 / 222 + 208 | `docs/milestones/job-metrics/*.json` (5 new files) | `dpu_seconds` |
| Full parse execution time (10 x G.1X) | 817 s | `edgarqa-parse-filings-jr_af891577...json` | `execution_seconds` |
| **After ADR-031 (re-parse 2026-11-21)** | | | |
| HTML filings `ok` before / after; strict success before / after | 11178 / 11720; 0.8443 / 0.8853 | `docs/milestones/M02-adr031-reparse.json` | `html.ok_before`, `html.ok_after`, `html.strict_before`, `html.strict_after` |
| `partial` / `no_sections` after | 1394 / 125 | same | `html.partial_after`, `html.no_sections_after` |
| Status downgrades / previously-ok no longer ok / previously-ok changed | 0 / 0 / 0 | same | `gate.status_downgrades`, `gate.previously_ok_not_ok`, `gate.previously_ok_changed` |
| `partial`->`ok` / `no_sections`->`partial` | 542 / 1 | same | `transitions` |
| 10-Qs missing both Part I items: before / after / recovered to ok | 1226 / 755 / 421 | same | `ten_q_missing_both_p1` |
| Companies (of 78): at least one filing ok / every affected filing ok / still a filing missing both | 36 / 14 / 60 | same | `companies_with_both_missing_before` |
| Residual 10-Q `partial` / companies | 1135 / 83 | same | `ten_q_partial.after`, `residual_10q_partial_companies` |
| Residual: no body heading / heading not chosen | 554 / 581 | `docs/milestones/M02-residual-causes.json` | `summary.by_cause` |
| Pre-check set: previously-ok changed / body characters lost / gained | 0 / 0 / 23191035 | `docs/milestones/M02-part-state-diff.json` | `summary.by_group[*].old_ok_changed`, `body_chars_lost`, `chars_gained` |
| Re-parse: execution seconds / DPU-seconds / state | 5404 / 54048 / TIMEOUT | `docs/milestones/job-metrics/edgarqa-parse-filings-jr_13f897c4....json` | `execution_seconds`, `dpu_seconds`, `state` |
| Stale chunks in `curated.chunks` / filings affected | 51523 / 440 | Athena (`count` of `curated.chunks` minus `sum(n_chunks)` of `curated.filings`) | n/a (command output) |
| Cost in USD (fetch, parse, all runs) | NOT MEASURED | job-metrics `cost_usd` is null; Cost Explorer returned $0 unblended and a Glue usage figure with no stated unit | n/a |
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
5. **Cloud sprint, in order** (superseded 2026-11-21: the full fetch, load and parse were run on Glue, not EMR, which the SCP denies; see "Full corpus on AWS Glue"): `terraform plan/apply` (new `ecr` and `emr` modules);
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
- **ADR-031 is written** (`docs/decisions.md`, status `proposed`, Consequences drafted for your review) and ADR-028 carries a note that its revisit trigger fired. Open: the `merge_curated` `NOT IN` delete (above) needs a decision and a test; no ADR needed unless you want one for it.
- `eval/lake_parse_report.py`, `eval/parser_diff.py`, `eval/reparse_compare.py` and `eval/residual_causes.py` are measurement tooling (read-only); no ADR needed.

## Full corpus on AWS Glue (2026-11-21)

**What ran** (job-metrics files in `docs/milestones/job-metrics/`; all Glue 5.0, G.1X):

| Step | Where | Result | Execution | DPU-seconds |
|---|---|---|---:|---:|
| Fetch 13,239 filings -> `s3://edgarqa-lake-019593222039/raw/filings` | local process, refreshable AWS credentials | 13,104 fetched + 135 already there, 0 failed, 0 warnings | not timed (rate-limited to 8 req/s, no DPU) | n/a |
| `edgarqa-load-filings-html` (2 workers) | Glue | `raw.filings_html` 13,239 rows | 76 s | 153 |
| `edgarqa-parse-filings` (10 workers, 90 min cap, 0 retries, `--repartition 80`, FinanceBench PDFs included) | Glue | real `curated.filings` 13,239 `edgar_html` + 84 `financebench_pdf`; `curated.chunks` 2,824,614 | 817 s | 8,178 |
| `edgarqa-build-companies` rerun 1 (2 workers) | Glue | `in_financebench` still NULL: the column was hard-coded NULL | 111 s | 222 |
| `edgarqa-build-companies` rerun 2, after `9c6d8c9` (2 workers) | Glue | `in_financebench` true for 32 ciks, 0 NULL | 104 s | 208 |

**Mistake, disclosed.** I started a parse run (`jr_8b041efd...`) by mistake before the arguments were set: no `--repartition`, no
FinanceBench inputs. I stopped it, but not for about 16 minutes (it had run 932 s, 9,327 DPU-seconds, more than the real parse).
It left `curated.filings` and `curated.chunks` created and empty (0 rows, checked in Athena); the real run then filled them. Its
cost is in the DPU total above (18,088 DPU-seconds over the five Glue runs) and in the `NOT MEASURED` dollar figure.

**Gate, as you required.** The fetch authenticated (the `edgarqa` profile needs `botocore[crt]`; I ran `uv run --with
"botocore[crt]"`, nothing added to the project), SEC returned no error and no retry warnings, and the S3 object count equalled the
target count with nothing missing before anything was parsed.

**Parse status by form** (Athena on `curated.filings`, `doc_source='edgar_html'`; `html.status_by_form`):

| Form | ok | partial | no_sections |
|---|---:|---:|---:|
| 10-K | 2806 | 182 | 24 |
| 10-K/A | 8 | 77 | 1 |
| 10-Q | 8350 | 1668 | 91 |
| 10-Q/A | 14 | 8 | 10 |

The 10-Q `partial` rows are 1,226 missing `P1-1` and `P1-2`, 257 missing only `P1-2`, and 193 missing only `P1-1` (`html.non_ok_errors`): every `partial` 10-Q is a Part I miss. Of the 259 `partial` 10-K and 10-K/A filings, 206 are missing item 1 and 104 are missing 1, 1A, 7 and 8 together (`html.non_ok_errors`); why is NOT MEASURED (the 77 10-K/A are probably amendments without those items, not checked).

**JPMorgan-style blast radius (ADR-028), measured as a symptom.** `cross_reference_10q_both_p1_missing` in the metrics file:

- 1,226 10-Qs with both Part I items missing, in **78 of the 500 companies (15.6%)**; in **46 of those 78 every 10-Q the company filed is affected** (46 companies, 1,606 10-Qs in the 78 companies, 1,226 affected). JPMorgan (CIK 19617) is 21 of 21, 2020-05-07 to 2026-11-03, as in ADR-028. Others at or near 100% include Kimco (22 of 22), Colgate (21 of 21), Humana, Moderna, Delta, UDR, Public Storage, Netflix, FIS, PSEG, Vulcan (`per_company`).
- The text is not lost, it sits under the wrong label: **690 10-Qs in 50 companies have more than 80% of their text under `P2-6`** (Exhibits) (`p2_6_dominant_10q`); for those, a Part I section filter returns nothing and an Item 6 filter returns the whole filing. For 10-Ks the same cross-reference symptom is smaller: 54 of 3,062 10-Ks have more than half of their text under Item 15 (41 more than 80%) (`item15_dominant_10k`).
- **Only some of this is JPMorgan's format, and I cannot say how much.** I read the raw text of the latest affected 10-Q of six companies (JPM, Colgate, Humana, Moderna, Delta, Netflix): JPM and Colgate have no Part I `Item 1`/`Item 2` headings in the body (contents-table rows only, the ADR-028 format). For Humana and Moderna I ran the parser's own block and heading functions: the contents table has a `Part I` row and a `Part II` row (blocks 37 and 74), the body has no `Part I` line of its own, and the first `Part` line after the table is `Part II` (blocks 478 and 412). `_candidates` (`pipelines/parsing/sections.py`) takes the current part from those lines, so Part I's `Item 1.` / `Item 2.` headings are coded `P2-...` or dropped (Moderna's `Item 1. Financial Statements`, block 75, ends up as `P2-1`; its Part I text is under `P2-1`, 130,120 characters). This is consistent with the Part-state defect, **not verified on the other 70-odd companies**. Delta and Netflix have a body `Item 2` heading but no body `Item 1` heading: a third shape. I tried two automatic classifiers on the affected 10-Qs and both mis-classified JPMorgan itself (a contents-table position threshold, then a row-spacing rule), so I discarded them: **the share of the 78 companies that is truly JPMorgan's format is NOT MEASURED.**
- What this means for ADR-028: its premise (5 of 500 CIKs fetched, so the corpus-wide share is unknown) is now measured as a symptom, and its "if JPMorgan-like filers are more than a handful, build the detector" trigger has fired. The cheapest fix is probably not the title-based detector but the Part state (reset it at a Part I heading in the contents table, or infer Part I when Item 1 and Item 2 appear before the first Part II): NOT tested.

**Other counts.** 126 filings are `no_sections` (24 10-K, 1 10-K/A, 91 10-Q, 10 10-Q/A) and 162 filings have no chunk at all (`select count(*) from curated.filings where n_chunks = 0`). Whether 10-K `(continued)` headings (ADR-030) are common is NOT MEASURED in this pass.

**`curated.companies`:** 12,321 rows, 500 `in_sp500`, 32 `in_financebench` (30 of them also in the S&P 500), 0 NULL. This needed a code change (`9c6d8c9`, test `test_run_reads_financebench_ciks_from_curated_filings`): the column was hard-coded NULL, so rerunning on the FinanceBench load could not populate it.

**Not verified / still there.** The dated snapshot table properties of `curated.companies` cannot be read through Athena (NOT VERIFIED). The Glue database `corpus_smoke` and its FinanceBench tables still exist (no longer needed: the PDFs are in the real `curated`). Dollar cost is `NOT MEASURED`. The three orphaned smoke prefixes under `warehouse/` are still there. `eval/lake_parse_report.py` (new) reproduces every number in `M02-fullcorpus-metrics.json` from Athena.

## After ADR-031: the re-parse (2026-11-21; code `6f528ea82a1a`)

**What changed.** ADR-031 (`pipelines/parsing/sections.py`, tests `tests/unit/parsing/test_part_state.py`): a table of contents that names two Parts no longer moves the 10-Q part for the body, a 10-Q item before any Part line is a Part I item, and a 10-Q whose first heading is `P1-2` or later gets `P1-1` from the block after the contents table (at least 5,000 characters). 10-Ks are untouched.

**Gate, before any cloud run** (`eval/parser_diff.py`, `docs/milestones/M02-part-state-diff.json`; 2,661 stored filings: the 1,226 affected 10-Qs, 1,000 seeded previously-ok 10-Qs, 150 single-miss `partial` 10-Qs, 150 blast-cache 10-Ks, the local 135): previously-ok filings changed **0**; status downgrades **0**; body characters lost **0**, gained 23,191,035. **The coverage gate was relaxed and this is where it is recorded:** as first worded ("no filing loses block coverage") it failed literally: 417 filings lose 66,245 characters in total (at most 0.53% of a filing), all of them heading or title lines of headings that are now recognised and whose text now lives in `section_title` (1,702 heading blocks, 65,615 characters, longest 113; 630 characters of second-line titles in 9 filings). Shekar approved "no filing loses BODY text" instead. An earlier version of the fix (without the implied `P1-1`) made 40 filings lose about 48% of their text from the index and was not shipped (ADR-031 option (d)).

**Gate on the whole corpus after the re-parse** (`eval/reparse_compare.py` on `curated.filings` before and after, `docs/milestones/data/curated_filings_{before,after}_adr031.json.gz`): same 13,323 accessions; **status downgrades 0; previously-ok filings no longer ok 0; previously-ok filings with a changed status, section count or chunk count 0; 10-K and 10-K/A rows changed 0; FinanceBench rows changed 0.** Transitions: `partial`->`ok` 542, `no_sections`->`partial` 1, all others unchanged.

| | Before | After |
|---|---:|---:|
| HTML filings `ok` | 11,178 | 11,720 |
| `partial` | 1,935 | 1,394 |
| `no_sections` | 126 | 125 |
| **Strict parse success** | **84.43%** | **88.53%** |
| `ok` + `partial` | 99.05% | 99.06% |
| 10-Q `partial` | 1,676 | 1,135 |
| 10-Qs missing both `P1-1` and `P1-2` | 1,226 | 755 |

By form after: 10-K 2,806 ok / 182 partial / 24 no_sections; 10-K/A 8 / 77 / 1; 10-Q 8,891 / 1,127 / 91; 10-Q/A 15 / 8 / 9 (`by_form_after`).

**Companies and filings recovered.** Of the 1,226 filings missing both items, **421 are now `ok`**; including the filings that were missing only one item, **542 filings** went to `ok` in total. Of the 78 companies that had any filing missing both: **36 have at least one filing now `ok`, 14 have every affected filing `ok`, 12 have every 10-Q `ok`, and 60 still have a filing missing both** (`companies_with_both_missing_before`). Examples (all 21 10-Qs each): Corning 18 ok (3 partial), Moderna 12 ok (9 partial, `P1-2` missing), Netflix 11 ok, Delta 3 ok; JPMorgan, Colgate and Humana 0 ok.

**Residual by cause** (1,135 10-Qs `partial`, 83 companies; `eval/residual_causes.py`, `docs/milestones/M02-residual-causes.json`; parser-level: a missing item has a "body heading" when a candidate for it has at least 3,000 characters of text after it):

| Cause | Filings | Missing both | `P1-2` only | `P1-1` only |
|---|---:|---:|---:|---:|
| No body heading for the missing item (the ADR-028 format: JPMorgan, Colgate 21 of 21 each) | 554 | 322 | 187 | 45 |
| A body heading exists but the chain did not choose it | 581 | 433 | 98 | 50 |

Of the second group, **409 have a chain that is a single contents-table row (`P2-6`)**: the last row of a contents table is weighted by all the text up to the next candidate, so one row outweighs the body chain (ADR-028 option (a)). Humana (21 of 21), Delta (18 of 21) and Netflix (10 of 21) are in this group. The other 172 are chains where a neighbouring heading conflicts with the missing one (mechanism not examined). 37 companies have only no-body-heading filings. The residual not in the 10-Qs: 125 `no_sections` and 259 `partial` 10-K / 10-K/A filings (causes NOT MEASURED).

**The re-parse timed out; `curated.chunks` is inconsistent.** Run `jr_13f897c4...` (10 x G.1X, 90-minute cap, no retry) ended `TIMEOUT` at 5,404 s and **54,048 DPU-seconds**, against 817 s and 8,178 for the first full parse. The driver log shows the parse and both `MERGE` statements finished about 13 minutes in; the remaining 76 minutes were one stage (138, 26 tasks of about 70 to 75 minutes each, 25 finished at the cap) of the last statement in `merge_curated`: `DELETE FROM chunks WHERE accession IN (...) AND chunk_id NOT IN (SELECT chunk_id ...)`. `NOT IN` is null-aware and plans as a nested-loop anti-join over the 2.8 million chunk ids; on the first run the table was empty. Consequence, measured in Athena: `curated.filings` is fully updated (above), but **`curated.chunks` has 2,878,589 `edgar_html` chunks where `curated.filings.n_chunks` sums to 2,827,066: 51,523 stale chunks (the old section labels of re-parsed filings) in 440 filings**. FinanceBench is unaffected (29,620). Chunk-derived numbers in this report (chunk counts, token histogram, 20-chunk read, `P2-6`/Item 15 shares) were taken before this re-parse and are NOT re-measured. **Cleanup is not done**: the fix is to replace `NOT IN` by `NOT EXISTS` (or an anti-join `MERGE ... WHEN MATCHED THEN DELETE`) in `merge_curated`, with a test, and re-run or delete the orphans directly; it needs your decision (it changes real `curated` again).

**Cost.** DPU-seconds of this phase: 54,048 (re-parse, timed out). Dollar cost NOT MEASURED (no price recorded; Cost Explorer showed $0 unblended). Session total of Glue runs since the full-corpus fetch: 153 + 9,327 + 8,178 + 222 + 208 + 54,048 = 72,136 DPU-seconds (`docs/milestones/job-metrics/*.json`).

**`sections_eval` after ADR-031:** 49/50 = 98.0%, filing-level 95% CI 94.0% to 100.0%, the same single miss (NI 7, ADR-030); the output is identical to the committed `M02-sections-eval.json` (the evaluation covers 10-Ks and ADR-031 does not touch the 10-K path).

## M2 on AWS Glue: build_companies and FinanceBench PDFs (2026-11-20, code `0d4799648c38`)

Job metrics: `docs/milestones/job-metrics/edgarqa-build-companies-*.json` and the `edgarqa-parse-filings` run whose note says "FinanceBench only". Both ran with 2 G.1X workers; DPU-seconds are in those files, and the dollar cost is `NOT MEASURED` (no price recorded).

- **`curated.companies` on Glue** (`edgarqa-build-companies`, reads `raw.fsds_sub` and the ticker map and S&P 500 CSV staged under `s3://<lake>/reference/`): Athena shows 12,321 rows, one per cik that files in `raw.fsds_sub` or is in the SEC ticker map (not only the S&P 500, by design), of which 500 have `in_sp500` (the CSV has 503 rows: share classes of one cik collapse), 7,996 a ticker, 10,313 a SIC and 10,520 a fiscal year end; `in_financebench` is NULL in every row (not yet loaded). Columns match `COMPANIES` (cik, name, ticker, sic, fye_mmdd, in_sp500, in_financebench, aliases). The dated snapshot properties on the table could not be read through Athena (`SHOW TBLPROPERTIES` lists only `write_compression`); NOT VERIFIED on Glue.
- **FinanceBench PDFs on Glue** (`edgarqa-parse-filings --skip-html true`, PDFs, question file and company CSV read from `s3://<lake>/financebench/`, written to `lake.corpus_smoke`): Athena shows 84 `ok` documents (0 errors), 29,620 chunks, 11,948 distinct (document, page) pairs with a chunk, no chunk with a NULL or non-positive `page`, `max n_tokens` 400, 0 over 400, 0 table chunks (PDF tables are not detected), no NULL `cik`. This equals the local result (84 ok, 29,620 chunks, 11,948 pages with text, `docs/milestones/M02-parse-metrics.json` `financebench`). PyMuPDF 2.1 works on Glue 5.0.
- Superseded on 2026-11-21: the full fetch, the full parse and the real `curated` namespace are done (section "Full corpus on AWS Glue" above); the PDFs in `corpus_smoke` are now redundant.
