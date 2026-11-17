# M1 Definition-of-Done report (local half)

Generated 2026-11-17 by following the `/dod` steps by hand, from the result files named below.
Numbers are copied from files only (the DQ numbers are parsed from `M01-dq-report.md` by the
generator); where no file holds a number it says `NOT MEASURED`, and where a number comes from a
command run for this report the command is shown. Provenance: benchmark at commit `546c0a8`,
difference analysis at `f86aaf6`; the results were regenerated after ADR-022 and the lake,
the DQ report and the spot check were rebuilt on the same code.

## Verdict

**The local half of M1 is done, and Spark, DuckDB and the reference now agree on real data with
0 differing rows and 0 unexplained differences. M1 as a whole is NOT complete.** Deferred to the
cloud phase, and not started: the full-history ingest to S3/Glue, the EMR run and its T2 numbers,
the EMR column of the benchmark, and the demo link. One thing is pending on Shekar: the proposed
`fiscal_ref.py` diff (`docs/proposals/ADR-022-fiscal_ref.diff`) has not been applied, so "the
reference" below means `derive_fiscal` with the ADR-022 sort and eligibility applied around it.

## Checklist (M01 runbook section 7)

| # | Item | Status | Evidence |
|---|---|---|---|
| 1a | `raw.*` and `curated.*` (including `facts_dimensional`) exist locally | PASS | Command (Spark, `SHOW TABLES` + `count(*)`, run earlier this session; the lake was since rebuilt by `make spark-facts`): `lake.raw.fsds_{num,pre,sub,tag}` and `lake.curated.{facts_history,facts,facts_dimensional,companies}` exist; raw holds 7 quarters (2022q1, 2022q3, 2022q4, 2023q1, 2023q3, 2023q4, 2024q1), more than the 2 the runbook asks for locally. |
| 1b | the same in Glue/S3 (full history) | DEFERRED (cloud phase) | Not started. |
| 2 | T1 recorded: exact `raw.fsds_num` rows, `curated.facts` rows, bytes in S3 | PARTIAL | Rows: numbers table (local, 7 quarters). Bytes in S3: DEFERRED, `NOT MEASURED`. |
| 3 | T2 recorded: EMR minutes and $ | DEFERRED (cloud phase) | `NOT MEASURED`. |
| 4 | Second-source spot check: 60/60 or every mismatch explained | PASS (accepted by Shekar) | **58/60 exact; the 2 non-matches are JPMorgan revenue, correctly stored under RevenuesNetOfInterestExpense (banks omit the Revenues tag) — a metric-catalog item for M5, not an ingestion error.** Rerun on the rebuilt lake: still 58/60, the same two rows, `classification: null` (M5's job). Note in `docs/plan/05_data_contracts.md` section 4.2. |
| 5 | Apple FY2023 revenue matches the 10-K | PARTIAL | `curated.facts` holds 383285000000.0000 USD for accession 0000320193-23-000106 (DQ report) and it matches companyfacts exactly (spot check). A hand comparison with the 10-K income statement itself was NOT RUN. |
| 6a | ADR-007 benchmark table filled in | PARTIAL | DuckDB and local-Spark columns filled for 2 and 7 quarters; EMR column `pending — cloud phase`; 20-quarter and all-quarter rows `NOT MEASURED`. ADR-007's decision and consequences are Shekar's TODO stub. |
| 6b | Differential test green | PASS | Tests (command output, this session): `make test-integration` 334 passed; `make test` 845 passed and 17 xfailed (the strict-xfail tests that state the proposed reference change); `make lint` clean. Real data: Spark == DuckDB with 0 differing rows in all three tables on 2 and 7 quarters; the reference run directly differs from DuckDB's `facts_history` in 0 of 12,201,161 rows. |
| 7 | `/dod M1` report written | PASS | This file. |

## Numbers

| Number | Value | Source file | Key or section |
|---|---:|---|---|
| `raw.fsds_num` rows, 7 quarters | 24,166,097 | `docs/milestones/M01-dq-report.md` | "Null rates, `raw.fsds_num`" |
| `curated.facts` rows | 8,229,693 | same | "Curated facts" |
| distinct facts (`fact_key`) | 8,053,335 | same | "Curated facts" |
| derived Q4 rows in `curated.facts` | 763,936 | same | "Curated facts" |
| restated facts | 181,400 | same | "Curated facts" |
| `curated.facts_history` rows | 12,201,161 | `docs/milestones/M01-benchmark.json` | `comparisons.7q.facts_history.rows_a` |
| `curated.facts_dimensional` rows | 7,790,722 | same | `comparisons.7q.facts_dimensional.rows_a` |
| `curated.companies` rows | 11,090 | command, this session (`count(*)`; companies are not affected by ADR-022) | not in a result file |
| raw rows dropped, any cause (each once) | 1,050,644 (4.35%) | `docs/milestones/M01-dq-report.md` | "Raw `num` rows that `normalize_facts` drops" |
| dropped: `value` NULL / no fiscal year end | 1,033,878 / 19,300 | same | same |
| companies with a changed FYE | 105 of 8,200 | same | "Companies whose fiscal-year end changed" |
| label offsets (rule 3) | 6,489 companies; offsets: {-1: 173, 0: 6315, 1: 1}; non-constant: 6. | same | "Label-offset distribution" |
| spot check matches | 58 of 60 | `docs/milestones/M01-spotcheck.json` | `summary.matched`, `summary.n_comparisons` |
| spot check outcomes | {'match': 58, 'missing_in_curated': 2} | same | `summary.outcomes` |
| DuckDB, 2 quarters: wall / peak memory | 7.98 s / 9,833 MB | `docs/milestones/M01-benchmark.json` | `runs[label=2q, engine=duckdb]` |
| local Spark, 2 quarters: raw load ; normalize | 38.19 s / 1,072 MB ; 112.07 s / 10,194 MB | same | `runs[label=2q, engine=spark_local]` |
| DuckDB, 7 quarters | 113.25 s / 9,930 MB | same | `runs[label=7q, engine=duckdb]` |
| local Spark, 7 quarters: raw load ; normalize | 125.21 s / 1,158 MB ; 277.07 s / 11,734 MB | same | `runs[label=7q, engine=spark_local]` |
| Spark vs DuckDB differing rows, 2 and 7 quarters, all three tables | 0 | `docs/milestones/M01-benchmark.json` | `comparisons.*.*.only_a`, `only_b` |
| unexplained differences (classifier), 2 and 7 quarters | 0 | `docs/milestones/M01-differential-analysis.json` | `cells.*.classification.*.unexplained` |
| reference run directly vs DuckDB `facts_history`: companies / rows / differences, 2 quarters | 6,721 / 3,320,915 / 0 | same | `cells.2q.reference_agreement` |
| same, 7 quarters | 8,193 / 12,201,161 / 0 | same | `cells.7q.reference_agreement` |
| Spark run-to-run difference, 7 quarters (bench run vs rebuilt lake) | 0 / 0 / 0 rows | same | `cells.7q.spark_repeatability.differences` |
| `fact_id`s shared by several `facts_history` rows (Spark), 7 quarters: before / after ADR-022 | 175 / 0 | `docs/milestones/M01-differential-analysis.json` (before: at commit `bcb8cb5`) | `cells.7q.identity_collisions.spark` |
| derived Q4 rows in Spark `facts_history`, 7 quarters: before / after | 784,934 / 764,324 | same | same |
| of which from forms other than 10-K, 10-K/A, 10-KT: before / after | 28,185 / 0 | same | same |
| Spark vs DuckDB differing rows, 7 quarters, before ADR-022 (facts_history, facts, facts_dimensional) | 5,130, 3,955, 1,264 | same (at commit `bcb8cb5`) | `cells.7q.classification.*.total` |
| T2 (EMR minutes, $), S3 bytes, 20-quarter and all-quarter benchmark cells | NOT MEASURED | n/a | deferred to the cloud phase |

## Open issues

1. **The reference file is not changed yet.** `fiscal_ref.py` still breaks ties by list position and
   derives Q4 from FY rows of any form. The twins implement the ADR-022 rules around it
   (`pipelines/spark/fiscal.py`, the DuckDB SQL), so every number above holds, but the three
   implementations agree only because the twins add the sort and the eligibility check themselves.
   To finish: apply `docs/proposals/ADR-022-fiscal_ref.diff` (verified on a scratch copy: it applies
   cleanly, and afterwards only the 17 strict-xfail tests in
   `tests/unit/test_fiscal_reference_proposed.py` turn red, the signal to delete their markers; all
   unit and property tests pass with `--runxfail`). Then `pipelines/duckdb/oracle.py` and
   `tests/proposed_rules.py` equal `derive_fiscal` and can be deleted.
2. **The tie rules are stable, not necessarily right.** A label-offset tie goes to the submission
   with the smallest accession; two FY rows of one filing after a fiscal-year change go to the
   smaller value. Either is an arbitrary but repeatable choice; the business rule is Shekar's.
3. **Rule 2 removed derived Q4 rows.** In the 7-quarter Spark `facts_history` they fell from
   784,934 to 764,324 (numbers table): Q4 values that came from
   forms other than 10-K, 10-K/A and 10-KT (registration statements, 10-Qs, 20-F and 40-F filings and
   so on) no longer exist. Whether a Q4 from an S-1 or a foreign annual report should be derived is a
   product decision; the rule as given excludes it.
4. **Outlier fiscal years.** The DQ report's "Rows by fiscal year" lists years from 1932 to 2923,
   from `ddate` values in the source filings. They are small in number and were not altered.
5. **The DuckDB twin's time grows faster than the data** between 2 and 7 quarters. Not investigated.
6. **Spot check.** Only 1 calendar-frame comparison was possible. The 2 non-matches
   stay unclassified for M5.
7. **Reference data is a fetch-date snapshot.** `curated.companies` records the dates and SHA-256 of
   the two frozen files as table properties; `data/` is gitignored; the S&P list's own as-of date is
   `NOT MEASURED`.
8. **Local environment.** A macOS `hidden` flag on the venv's `.pth` files hid `distutils` from
   PySpark three times; `fiscal.py` now imports `setuptools` itself when it is missing.
9. **Not measured on EMR:** the memory of one company's facts in a pandas group (`applyInPandas`),
   and the executor requirements in `docs/plan/M01_xbrl_lakehouse.md` section 3.8.

## Decisions that need an ADR

- ADR-022 (this change: deterministic ties, annual-form Q4 parents, unique derived `fact_id`) is
  written as a stub with measured numbers; its body is Shekar's.
- ADR-017 (as-originally-reported vs restated values) is on the runbook's end-of-milestone list
  and is not written.
- Existing stubs for Shekar's wording: ADR-007, ADR-020, ADR-021, ADR-022.
