# M1 Definition-of-Done report (local half)

Generated 2026-11-17 by following the `/dod` steps by hand, from the result files named below.
Numbers are copied from files only; where no file holds a number it says `NOT MEASURED`, and where
a number comes from a command run for this report the command is shown. Provenance: the benchmark
file was produced at commit `26b509d`, the difference analysis at `f4daa7e` (the
engines' code is identical between the two; only the explainer changed).

## Verdict

**The local half of M1 is done, with the open issues below. M1 as a whole is NOT complete.**
Deferred to the cloud phase, and not started: the full-history ingest to S3/Glue, the EMR run and
its T2 numbers, the EMR column of the benchmark, and the demo link.

## Checklist (M01 runbook section 7)

| # | Item | Status | Evidence |
|---|---|---|---|
| 1a | `raw.*` and `curated.*` (including `facts_dimensional`) exist locally | PASS | Command (Spark, `SHOW TABLES` + `count(*)`): `lake.raw.fsds_{num,pre,sub,tag}` and `lake.curated.{facts_history,facts,facts_dimensional,companies}` exist; raw holds 7 quarters (2022q1, 2022q3, 2022q4, 2023q1, 2023q3, 2023q4, 2024q1), more than the 2 the runbook asks for locally. |
| 1b | the same in Glue/S3 (full history) | DEFERRED (cloud phase) | Not started. The downloader and Spark session support local destinations only. |
| 2 | T1 recorded: exact `raw.fsds_num` rows, `curated.facts` rows, bytes in S3 | PARTIAL | Rows: see the numbers table (local, 7 quarters). Bytes in S3: DEFERRED, `NOT MEASURED`. |
| 3 | T2 recorded: EMR minutes and $ | DEFERRED (cloud phase) | `NOT MEASURED`. |
| 4 | Second-source spot check: 60/60 or every mismatch explained | PASS (accepted by Shekar) | **58/60 exact; the 2 non-matches are JPMorgan revenue, correctly stored under RevenuesNetOfInterestExpense (banks omit the Revenues tag) — a metric-catalog item for M5, not an ingestion error.** `M01-spotcheck.json` keeps them with `classification: null` (M5's job). The note is captured in `docs/plan/05_data_contracts.md` section 4.2. |
| 5 | Apple FY2023 revenue matches the 10-K | PARTIAL | `curated.facts` holds 383285000000.0000 USD for accession 0000320193-23-000106 and it matches companyfacts exactly (spot check). A hand comparison with the 10-K income statement itself was NOT RUN: the DQ report states it asserts no match. |
| 6a | ADR-007 benchmark table filled in | PARTIAL | DuckDB and local-Spark columns filled for 2 and 7 quarters; EMR column `pending — cloud phase`; the 20-quarter and all-quarter rows `NOT MEASURED` (only 7 quarters are local). ADR-007's decision and consequences are Shekar's TODO stub. |
| 6b | Differential test green | PASS for the tests; real-data output NOT identical | Tests (command output, this session): `make test-integration` 263 passed, `make test` 695 passed, `make lint` clean. They include Spark == reference, DuckDB == reference, and Spark == DuckDB on a hand-built fixture. On real data the engines differ in a small fraction of rows, all but 6 attributed to named causes, see open issues 1 to 3. |
| 7 | `/dod M1` report written | PASS | This file. |

## Numbers

| Number | Value | Source file | Key or section |
|---|---:|---|---|
| `raw.fsds_num` rows, 7 quarters | 24,166,097 | `docs/milestones/M01-dq-report.md` | "Null rates, `raw.fsds_num`" |
| `curated.facts` rows | 8,250,114 | `docs/milestones/M01-dq-report.md` | "Curated facts" |
| distinct facts (`fact_key`) | 8,071,662 | `docs/milestones/M01-dq-report.md` | "Curated facts" |
| derived Q4 rows in `curated.facts` | 784,359 | `docs/milestones/M01-dq-report.md` | "Curated facts" |
| restated facts | 183,653 | `docs/milestones/M01-dq-report.md` | "Curated facts" |
| `curated.facts_history` rows | 12,221,771 | `docs/milestones/M01-benchmark.json` | `comparisons.7q.facts_history.rows_a` (Spark) |
| `curated.facts_dimensional` rows | 7,801,086 | `docs/milestones/M01-benchmark.json` | `comparisons.7q.facts_dimensional.rows_a` (Spark) |
| `curated.companies` rows | 11,090 | command, this session (`count(*)` on `lake.curated.companies`) | not in a result file |
| raw rows dropped, any cause (each once) | 1,050,644 (4.35%) | `docs/milestones/M01-dq-report.md` | "Raw `num` rows that `normalize_facts` drops" |
| dropped: `value` NULL | 1,033,878 | same | same |
| dropped: unparseable `ddate` / no `sub` row | 0 / 0 | same | same |
| dropped: no fiscal year end | 19,300 | same | same |
| companies with a changed FYE | 105 of 8,200 | `docs/milestones/M01-dq-report.md` | "Companies whose fiscal-year end changed" |
| label offsets (rule 3) | -1: 173, 0: 6,315, 1: 1; non-constant: 6 (6,489 companies) | `docs/milestones/M01-dq-report.md` | "Label-offset distribution" |
| spot check matches | 58 of 60 | `docs/milestones/M01-spotcheck.json` | `summary.matched`, `summary.n_comparisons` |
| spot check outcomes | {'match': 58, 'missing_in_curated': 2} | `docs/milestones/M01-spotcheck.json` | `summary.outcomes` |
| spot check frames compared / agree | 1 / 1 | `docs/milestones/M01-spotcheck.json` | `summary.frames_compared`, `summary.frames_agree` |
| Apple FY2023 revenue (curated) | 383285000000.0000 USD | `docs/milestones/M01-dq-report.md` | "Apple FY2023 revenue" |
| DuckDB, 2 quarters: wall / peak memory | 7.21 s / 12,016 MB | `docs/milestones/M01-benchmark.json` | `runs[label=2q, engine=duckdb]` |
| local Spark, 2 quarters: raw load ; normalize | 38.36 s / 1,090 MB ; 101.93 s / 10,792 MB | same | `runs[label=2q, engine=spark_local]` |
| DuckDB, 7 quarters | 140.6 s / 11,980 MB | same | `runs[label=7q, engine=duckdb]` |
| local Spark, 7 quarters: raw load ; normalize | 122.94 s / 1,193 MB ; 274.19 s / 11,597 MB | same | `runs[label=7q, engine=spark_local]` |
| engine disagreement, 2 quarters (rows, both sides) | facts_history 50, facts 44, facts_dimensional 2 | `docs/milestones/M01-differential-analysis.json` | `cells.2q.classification.*.total` |
| engine disagreement, 7 quarters (rows, both sides) | facts_history 5,130, facts 3,955, facts_dimensional 1,264 | same | `cells.7q.classification.*.total` |
| causes summed over the 3 tables, 2 quarters | identity_collision 6, fy_tie 90 | same | `cells.2q.classification` |
| causes summed over the 3 tables, 7 quarters | identity_collision 6, label_offset_tie 9,792, fy_tie 494, fy_tie_knock_on 51, unexplained 6 | same | `cells.7q.classification` |
| unexplained rows: series / reference outputs per series / both engines among them | 6 rows, 3 series / 2 / True | same | `cells.7q.unexplained_order_check` |
| Spark run-to-run difference, 7 quarters: facts_history / facts / facts_dimensional | 0 / 0 / 0 rows | same | `cells.7q.spark_repeatability.differences` (two runs) |
| `fact_id`s shared by several `facts_history` rows (Spark) | 2 quarters 248 (496 rows); 7 quarters 175 (350 rows) | same | `cells.*.identity_collisions.spark` |
| derived Q4 rows in Spark `facts_history`, 7 quarters: all / from forms other than 10-K, 10-K/A, 10-KT | 784,934 / 28,185 | same | `cells.7q.identity_collisions.spark` |
| T2 (EMR minutes, $), S3 bytes, 20-quarter and all-quarter benchmark cells | NOT MEASURED | n/a | deferred to the cloud phase |

## Open issues

1. **Derived Q4 rows can share a `fact_id` with a real quarter row.** A derived Q4 takes its FY
   parent's accession and form. Where that parent is a `qtrs = 4` row in a 10-Q or a registration
   statement (it looks like a trailing-twelve-month value; seen on Amazon's Q3 2023 10-Q), the
   derived row has the same `fact_key` and accession as the real quarter row. `facts_history` then
   holds several rows per `fact_id` (numbers table), which breaks the contract's uniqueness of
   `fact_id` per version. `curated.facts` keeps one of the rows; both engines now keep the real one
   (repeatable, but a stopgap, not a fix). A fix belongs in `fiscal_ref.py` (Shekar's file), for
   example requiring the FY row's period end to be a fiscal year end and its form to be annual;
   I have not proposed or tested one. Whether the derived Q4s from non-annual forms (numbers table)
   should exist at all is a decision.
2. **Two ties in the reference are decided by input order.** (a) The mode of a company's label
   offsets when two offsets occur equally often (known; left as is); (b) the choice between two FY
   (or two 9M) rows of one accession and filing date that differ in period end, which a fiscal-year
   change produces. For three dimensional series this was shown directly: the reference gives
   2 different outputs over 60 random input orders, and Spark's and
   DuckDB's outputs are each one of them (`unexplained_order_check`). Spark feeds the reference rows
   in whatever order Spark delivers them; DuckDB orders by `fact_id`. This is the source of the
   `label_offset_tie`, `fy_tie` and knock-on rows. Two Spark runs of the same 7 quarters are
   identical (numbers table), but that is two runs, not a guarantee. A deterministic policy (sort
   the reference's input, or give it explicit tie-break keys, in both engines) is the way to remove
   the disagreement; it is not done.
3. **Spark and DuckDB are not identical on real data.** The differing rows are attributed to the
   causes above by rules in `pipelines/duckdb/explain_differences.py` (evidence per row, not proof
   for every row); 6 rows in `facts_dimensional` fit no rule and were checked series by series
   instead (issue 2). The rules were tightened after review so that a cause only excuses rows it can
   explain, and each rule has a test that it does not excuse a changed value.
4. **Outlier fiscal years.** The DQ report's "Rows by fiscal year" lists years from 1932 to 2923,
   from `ddate` values in the source filings (for example a period end in the year 2923). They are
   small in number and were not altered.
5. **The DuckDB twin's time grows faster than the data** between 2 and 7 quarters (benchmark runs
   above). Not investigated.
6. **Spot check.** Only 1 calendar-frame comparison was possible (companyfacts
   attaches `frame` to one filing's entry per period). The 2 non-matches stay unclassified for M5.
7. **Reference data is a fetch-date snapshot.** `curated.companies` records the dates and SHA-256 of
   `data/company_tickers_2026-11-17.json` and `data/sp500_2026-11-17.csv` as table properties;
   `data/` is gitignored, so a fresh clone must refetch them. The S&P list is a community dataset
   whose own as-of date is `NOT MEASURED`.
8. **Local environment.** A macOS `hidden` flag on the venv's `.pth` files hid `distutils` from
   PySpark three times; `fiscal.py` now imports `setuptools` itself when `distutils` is missing
   (`tests/unit/test_distutils_guard.py`).
9. **Not measured on EMR:** the memory of one company's facts in a pandas group (`applyInPandas`),
   and the executor requirements recorded in `docs/plan/M01_xbrl_lakehouse.md` section 3.8.

## Decisions that need an ADR

- Derived-Q4 eligibility and identity (open issue 1): which FY parents may be used, and what keeps
  `fact_id` unique.
- A deterministic policy for the two ties (open issue 2), applied the same way in Spark and DuckDB.
- ADR-017 (as-originally-reported vs restated values) is on the runbook's end-of-milestone list and
  is not written.
- Existing stubs for Shekar's wording: ADR-007, ADR-020, ADR-021.
