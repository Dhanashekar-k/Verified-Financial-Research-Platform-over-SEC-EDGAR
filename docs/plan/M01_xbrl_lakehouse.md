# M1: XBRL lakehouse (big data)

**Time:** about 16 h · **Roles:** SDE + data engineering (big data), FDE (messy data) · **Depends on:** M0

## 1. Goal

Every XBRL fact since 2009, in clean Iceberg tables with correct fiscal periods, deduplicated with history kept. The same logic runs in Spark (locally and on EMR Serverless) and in DuckDB, with an honest benchmark.

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| FSDS ZIPs 2009q2 → latest (`05_data_contracts.md` §1.1) | `raw.fsds_{sub,num,tag,pre}` partitioned by `src_quarter` |
| companyfacts JSON for 10 spot-check companies (cross-check only; never written to `facts_history` in M1) | `curated.companies`, `curated.facts_history`, `curated.facts`, `curated.facts_dimensional` |
| `company_tickers.json`, S&P 500 snapshot CSV | `docs/milestones/M01-dq-report.md` (row counts, null rates, unit mix, custom-tag share, fiscal-derivation flags, spot checks) |
| — | ADR-007 (DuckDB vs Spark) with a benchmark table |
| — | T1 and T2 recorded: exact rows and GB; EMR minutes and $ |

## 3. Steps

### 3.1 Shared SEC client (1.5 h; Claude writes it, you review)
`pipelines/fetch/sec_client.py`:
- One process-wide token bucket at 8 req/s (configurable). The User-Agent comes from `SEC_USER_AGENT`; `Accept-Encoding: gzip, deflate`.
- Retries on 429/5xx/connection errors: exponential backoff with full jitter, honoring `Retry-After`, max 6 attempts, then raise.
- `get(url) -> bytes`, `get_json(url) -> dict`, `download(url, dest, sha256=None)` with an atomic write (temp file + rename).
- Unit tests with `respx`: rate is respected (time 30 calls), retry on 429 with Retry-After, the User-Agent header is always present.

### 3.2 FSDS downloader (1 h)
`pipelines/fetch/download_fsds.py --from 2009q2 --to latest --dest data/raw/fsds`:
- Skip files that exist with a matching size, and record SHA-256 in `data/raw/fsds/_manifest.json`.
- Extract each ZIP to `data/raw/fsds/{q}/{sub,num,tag,pre}.txt`, then gzip each `.txt`. Spark reads gzip directly; files aren't splittable, but parallelism comes from about 70 quarters × 4 files.
- **S3 destination: deferred.** The downloader supports local destinations only (`boto3` is not a dependency yet); it is needed before the EMR run (§3.8).
- For AWS, the same script runs on a temporary EC2 instance (or the serving box later) with `--dest s3://<lake>/raw/fsds/`. Data transfer into AWS is free and much faster than uploading from home [Likely].

### 3.3 Spark session factory (0.5 h)
`pipelines/common/spark_session.py`, `spark_session(env: Literal["local","emr"])`:
```text
# local
spark.sql.extensions                 = org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions
spark.sql.catalog.lake               = org.apache.iceberg.spark.SparkCatalog
spark.sql.catalog.lake.type          = hadoop
spark.sql.catalog.lake.warehouse     = ./warehouse
spark.jars.packages                  = org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:<pinned>
spark.driver.memory                  = 8g
spark.sql.shuffle.partitions         = 64
# emr (Glue catalog) [Likely; check the EMR Serverless Iceberg docs for your release]
spark.sql.catalog.lake.catalog-impl  = org.apache.iceberg.aws.glue.GlueCatalog
spark.sql.catalog.lake.io-impl       = org.apache.iceberg.aws.s3.S3FileIO
spark.sql.catalog.lake.warehouse     = s3://<lake-bucket>/warehouse
spark.jars                           = <Iceberg runtime jar bundled with the EMR release>
```

### 3.4 `fsds_to_iceberg` (2 h)
- Read with an **explicit schema** (no inference), `sep="\t"`, `header=True`, `quote='"'`, `escape='"'`, `mode=FAILFAST`, and header validation (`enforceSchema=False`). **Measured on 2023q3/q4:** the files are RFC-4180-style, so a field containing a quote or a tab is wrapped in `"` with inner quotes doubled (`"Vacation ownership interest (""VOI"") inventory"`). Disabling quote handling (this runbook's first guess) keeps the quotes in the text and splits the 12 `num` rows whose quoted `segments` contains a tab.
- Add `src_quarter`, `ingested_at`. Convert `value` to `decimal(28,4)`.
- Validate all four files of a quarter first (present, whole header line, every row and column parses, row count = lines minus header), then write with an atomic partition-scoped overwrite (`writeTo(...).overwrite(src_quarter == q)`), so rerunning a quarter replaces exactly it and a header-only file really empties it: **idempotent**. (`overwritePartitions()` cannot clear a partition when the new data is empty.)
- Local first: `make spark-raw Q=2023q3,2023q4`.

### 3.5 `normalize_facts` (6 h; **you write the fiscal/dedup core**)

**Filters for company-level consolidated facts:** `coreg IS NULL` and (`segments` IS NULL or empty). Dimensional facts (`segments` non-empty, `coreg IS NULL`) go to `curated.facts_dimensional` (keep them; S1 and future work may use them). Co-registrant rows (`coreg IS NOT NULL`) are not curated and stay in `raw.fsds_num`. In `raw.fsds_num` an absent `segments`/`coreg` may load as NULL or `''`; treat both as empty.

**Derived columns:**
- `taxonomy` = the **year-stripped** family of `version` (`us-gaap/2023` → `us-gaap`; also `dei`, `srt`, `ifrs-full`, …), or `custom` when `version == adsh`; `is_custom`. The year is dropped on purpose: a restatement filed under a newer taxonomy year must land on the same `fact_key` as the original (ADR-020).
- `period_end = to_date(ddate)`; `period_start` ≈ `period_end − qtrs quarters + 1 day` (approximate; companyfacts has exact `start` dates, so note this in the DQ report).

**Fiscal-period rules (the part interviewers probe):**
1. `fye_mmdd` comes from the `sub` row of the **same submission** (companies change fiscal year ends; 10-KT transition filings exist). Feb 29 → Feb 28.
2. `y_end(E)` = the smallest year `y` such that `E ≤ date(y, fye_mm, fye_dd) + 7 days`. The 7-day tolerance absorbs 52/53-week years. Apple's FY2022 ended 2022-09-24 and FY2023 ended 2023-09-30, both inside the window [Certain].
3. **Label offset per company.** Companies name fiscal years differently. Some name a year by the calendar year it ends in, others by the year it begins in (common among retailers whose year ends in late January or early February) [Likely]. So compute `label_offset(cik) = mode(sub.fy − y_end(sub.period))` over that company's 10-K submissions, and set `fiscal_year = y_end(E) + label_offset`. Flag companies whose offset isn't constant.
4. `fiscal_period`: `qtrs=4 → FY`; `qtrs=0` (an instant) gets the label of its quarter index too, `FY` at the year end and `Q1`–`Q3` otherwise, so balance sheets of one year stay distinguishable (qtrs 0 and the `I` suffix of `calendar_frame` mark it as an instant; `INSTANT` is not a value); `qtrs=1 → Q{n}`, where `n` = the number of whole quarters from fiscal-year start to `E` (with a tolerance); `qtrs=2 → H1`; `qtrs=3 → 9M`.
5. **Q4 is often never reported directly.** Derive `Q4 = FY − 9M` for **flow** concepts (same tag/uom) and mark it `dq_flags += 'q4_derived'`. Derive only when no directly reported `qtrs = 1` fact exists for that series and period end in the same accession (a direct row wins, which also keeps `(fact_key, accession)` unique). The derived row has `source = 'fsds'`, the form and accession of the 10-K-family filing that supplies the FY figure (normally `10-K`), `qtrs = 1`, `period_end` = the FY end, and `derived_from = [FY fact_id, 9M fact_id]` so the verifier (M6) can re-execute FY − 9M. **Never derive** Q4 for instants or for per-share, ratio and share-count concepts (`uom` containing `/`, or equal to `pure` or `shares`; none of these is additive). **Per provenance tier** (decision 2026-11-17): within (cik, tag, taxonomy, uom, fiscal_year), the original Q4 is the original FY − the original 9M and the latest Q4 is the latest FY − the latest 9M, using only parent rows that `curated.facts` retains; each derived row carries its FY parent's form and accession, and ties between candidate parents go to the latest filed.
6. `calendar_frame`: `CY{yyyy}` for about 12-month periods ending within ±30 days of Dec 31; `CY{yyyy}Q{n}` for quarters by calendar quarter of `E`; suffix `I` for instants. Cross-check against companyfacts `frame` on the spot-check companies.

**Dedup and restatements:**
- `fact_key = xxhash64(cik, tag, taxonomy, period_end, qtrs, uom)` (`facts_dimensional` also hashes `segments`), with `taxonomy` as defined above. Use Spark's built-in `xxhash64` (no Python dependency, which matters on EMR). DuckDB has no equivalent, so the Spark-vs-DuckDB differential test (§3.7) compares on the natural key and leaves the hash columns out; `derived_from` is compared by resolving each id back to its natural key.
- `facts_history`: every (fact_key, accession) row with `filed`, `form`, `is_amendment`.
- **Latest** version = max(`filed`), then max(`accession`). **Original** version = the version from the filing whose own reporting period covers the fact's fiscal period (or the earliest `filed` if none matches).
- `curated.facts` holds one row per fact_key **plus** a second row when the original value differs from the latest, with flags `is_latest` and `is_original`. `n_versions`; `is_restated` = more than one distinct value.
- **Product decision (write ADR-017):** by default, answers use the **as-originally-reported** value from the period's own filing (it matches the cited document). If the value was later restated, the answer says so and cites both. Finance users care about this distinction [Likely].

### 3.6 Data-quality checks (1.5 h)
`pipelines/spark/dq_checks.py` writes `docs/milestones/M01-dq-report.md`:
- Rows per `src_quarter` (no gaps after 2009q2), null rate per column, `uom` distribution, share of custom tags, share of facts with flags, distribution of `n_versions`, distribution of `label_offset` (most should be 0), companies with a changed FYE.
- **Second-source spot check:** for 10 companies × 3 metrics × 2 years, compare `curated.facts` with the companyfacts API (match on accession + tag + end date). Target 60/60; investigate every mismatch and classify it.
- **Manual check:** Apple FY2023 revenue against the 10-K income statement (≈ $383.3B [Likely]).

### 3.7 DuckDB twin and benchmark (2 h)
- `pipelines/duckdb/normalize_facts_duckdb.py`: the same rules in SQL over the gzipped TSVs, writing Parquet.
- **Differential test:** on 2 quarters, the Spark and DuckDB outputs must be identical (sort + hash, or EXCEPT both ways).
- **Benchmark table:** 2 quarters, 20 quarters, all quarters × {DuckDB on laptop, Spark local, EMR Serverless}. Columns: wall time, peak memory, $ (EMR from job metrics; laptop $0). Put it in ADR-007, with your honest conclusion.

### 3.8 EMR Serverless full-history run (1.5 h)
- Terraform module `emr`: Spark application, arm64, release label variable, `auto_stop` 5 min, `maximum_capacity` 64 vCPU / 256 GB, job role with S3 lake + Glue permissions only.
- Package: `make emr-package` zips `pipelines/` → `s3://<lake>/artifacts/pipelines-<git_sha>.zip`; entry points `pipelines/spark/*.py`.
- `make emr-run JOB=normalize_facts` → `aws emr-serverless start-job-run` with the conf above, then polls until done and prints billed vCPU-hours, memory GB-hours and the computed $.
- Record **T1** (row counts, bytes in S3) and **T2** (minutes, $, $/million facts).

## 4. Claude Code session plan

```
/milestone M1
Step 1: Implement pipelines/fetch/sec_client.py per M01 §3.1 with respx tests. Plan first.
```
```
Step 2: download_fsds.py per §3.2 (local and s3 destinations, manifest, idempotent).
Run it for 2023q3 and 2023q4 only. Show me head -n 3 of each extracted file.
```
```
Step 3: spark_session factory and fsds_to_iceberg per §3.3–3.4. Explicit schemas from the FSDS readme
(read data/raw/fsds/2023q4/readme.htm). Run locally on 2 quarters. Print row counts per table.
```
```
Step 4: Scaffold normalize_facts.py: reading, joins, filters, writing. It calls
derive_fiscal_columns(df) from pipelines/spark/fiscal.py and select_versions(df) from
pipelines/spark/versions.py. Create those two files, plus pipelines/common/fiscal_ref.py (the pure-Python
reference), as stubs that raise NotImplementedError. They're my files after that, and the hook will block edits.
Use the test-writer subagent to write tests/unit/test_fiscal.py from M01 §3.5 rules 1–6, with
cases: Apple 52/53-week years, a June FYE (Microsoft-like), a late-January FYE with label offset
0 and one with offset −1, Q4 derivation for a flow concept, no Q4 derivation for EPS or instants,
a 10-KT transition. Don't implement the stubs. I will.
```
You implement `derive_fiscal_columns` and `select_versions` (plus a pure-Python reference version used by the tests). Then:
```
Step 5: Write a differential test that runs my Spark functions and the Python reference on 10,000
sampled rows and asserts equality. Then dq_checks.py per §3.6 and the companyfacts spot check.
```
```
Step 6: DuckDB twin + benchmark harness per §3.7. Then Terraform emr module + make emr-run per §3.8.
fmt/validate only. I will apply and run.
```
End with `/dod M1`, `/adr "DuckDB vs Spark for XBRL normalization"`, `/adr "As-originally-reported vs restated values"`, and `/interview-me pipelines/spark/normalize_facts.py`.

## 5. You write

- `derive_fiscal_columns` (`pipelines/spark/fiscal.py`), `select_versions` (`pipelines/spark/versions.py`) and their pure-Python reference twins (`pipelines/common/fiscal_ref.py`, reused by the M10 companyfacts Lambda).
- The interpretation in ADR-007 and ADR-017.
- The classification of every spot-check mismatch.

## 6. Tests

- `test_sec_client.py`: rate limit, retries, User-Agent.
- `test_fiscal.py`: all cases above, plus **Hypothesis** property tests: (a) for any E and FYE, `y_end(E)` is the unique year whose FYE window contains E; (b) quarter index ∈ {1..4}; (c) derived Q4 + 9M == FY for random flow facts.
- `test_versions.py`: latest/original selection with amendments and recasts.
- `test_differential.py`: Spark == Python reference == DuckDB on a sample.

## 7. Definition of done

- [ ] `raw.*` and `curated.*` (including `facts_dimensional`) exist locally (2 quarters) and in Glue/S3 (full history).
- [ ] T1 recorded: exact `raw.fsds_num` rows, `curated.facts` rows, bytes in S3.
- [ ] T2 recorded: EMR minutes and $ for the full rebuild (from job metrics, not guessed).
- [ ] Second-source spot check: 60/60, or every mismatch explained in the DQ report.
- [ ] Apple FY2023 revenue matches the 10-K.
- [ ] ADR-007 benchmark table filled in; differential test green.
- [ ] `/dod M1` report written.

## 8. Pitfalls

- Treating `sub.fy` as the fact's fiscal year (the most common bug).
- Forgetting `coreg`/`segments` filters, which mixes segment numbers into company totals.
- Inferred schemas turning `value` into a double (precision loss on large numbers) or `adsh` into something odd. Always use explicit schemas.
- `2009q1.zip` is headers only.
- Running EMR jobs before the local 2-quarter run is clean: you pay to find bugs.
- Glue table names are lowercase; keep table and column names lowercase snake_case.

## 9. Interview prep from this milestone

- Walk through the fiscal-period derivation and the label-offset trick; why the 7-day tolerance.
- Why keep both original and latest values; what "restated" means; how answers present it.
- Your benchmark: at what size does Spark win, and why does it win for parsing (M2) but not here?
- Iceberg partitioning choice (`fiscal_year`) and what you'd change for per-company queries (bucketing by CIK, sort order).
