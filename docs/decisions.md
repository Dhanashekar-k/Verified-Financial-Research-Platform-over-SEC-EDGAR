# Architecture Decision Records

Rewrite each seeded ADR in your own words before M1. Add new ones with `/adr "<title>"`. Record disagreements with the manual here too; a recorded "I chose differently because…" is worth more in an interview than a silent default.

Planned ADRs (fill in as you reach them): 005 one EC2 box vs Lambda + Aurora (M9) · 006 no NAT gateway (M9) · 007 DuckDB vs Spark (M1) · 008 chunk size and the embedder limit (M2) · 009 hybrid retrieval components (M5) · 010 verifier operating point (M6) · 011 router cascade threshold (M7) · 012 composer model tier (M5) · 013 daily orchestration choice (M10) · 014 embedding model and halfvec (M3) · 015 public-demo abuse protection (M8/M9) · 016 no MinIO; file-based local catalog (M0) · 017 as-originally-reported vs restated values (M1) · 018 versioned tables + view swap (M3) · 019 spend-guard concurrency (M8)

---

## ADR-001: Iceberg lake is the source of truth; Postgres is a rebuildable serving copy (2026-11-16)
**Status:** accepted
**Context:** Heavy batch processing (all XBRL facts since 2009, parsing and embedding about a million chunks) needs cheap storage and distributed compute. Interactive questions need tens-of-milliseconds lookups. Athena takes seconds and charges per scan.
**Options considered:** (a) everything in Postgres: simple, but no lake semantics and painful full rebuilds; (b) everything in the lake with Athena serving: too slow and pay-per-query; (c) lake for truth plus a versioned export loaded into Postgres: two systems, but each does what it's good at.
**Decision:** (c). Spark writes Iceberg; `export_serving` writes versioned Parquet; the loader builds `*_vN` tables and swaps views atomically.
**Consequences:** DRAFT (Claude; rewrite in your own words): Rebuilding Postgres from the lake is always possible, so the serving copy can be dropped, reloaded or re-versioned without data loss, and evals can pin a snapshot. The cost is two systems to operate, an export/load step that must stay idempotent, and a freshness lag equal to export plus swap time. Every serving table needs a version and a view swap, and nothing may write to Postgres as if it were authoritative.
**How to revisit:** if the serving subset outgrows one Postgres box, or the load and swap time exceeds the freshness target.

## ADR-002: Numbers come from a typed query plan compiled to SQL; the LLM never writes SQL on the main path (2026-11-16)
**Status:** accepted
**Context:** Free-form text-to-SQL is hard to verify and risky (wrong tags and periods, injection-shaped failures). The verifier needs to know exactly what a number means.
**Options considered:** (a) free-form text-to-SQL with guardrails; (b) an LLM-filled typed `QueryPlan` (enum-constrained metrics) + a deterministic compiler producing parameterized SQL and provenance expressions; (c) hard-coded intents only.
**Decision:** (b), with (a) as a guarded fallback behind a flag, measured separately.
**Consequences:** DRAFT (Claude; rewrite in your own words): The verifier can check each number against a plan and provenance expression, and the model's output space is constrained to enum metrics, which makes failures auditable and injection-shaped SQL off the main path. The cost is coverage: any metric, period rule or comparison the catalog and compiler don't express gets a refusal or falls to the guarded fallback, so the metric catalog and compiler become the bottleneck and need steady extension. The fallback must stay behind its flag and be reported separately in evals.
**How to revisit:** if plan coverage on real user questions stays below an acceptable level after extending the catalog.

## ADR-003: Apache Iceberg rather than plain Parquet for the lake (2026-11-16)
**Status:** accepted
**Context:** Restatements need upserts; evals must be reproducible against a past corpus; daily appends must be atomic.
**Options considered:** (a) plain Parquet + Glue partitions; (b) Iceberg; (c) Delta Lake.
**Decision:** Iceberg (Glue catalog support on AWS, EMR and Athena integration, MERGE, snapshots, time travel).
**Consequences:** DRAFT (Claude; rewrite in your own words): Restatements can be applied as MERGE upserts, daily appends are atomic, and evals can read a past snapshot via time travel. The cost is catalog and tooling friction (Glue catalog, EMR and Athena compatibility, local file-based catalog for dev) and table maintenance such as compaction and snapshot expiry. Spark, DuckDB and pyiceberg versions must stay compatible with each other.
**How to revisit:** if the tooling friction on EMR or Athena costs more than it saves.

## ADR-004: No Kafka in this project (2026-11-16)
**Status:** accepted
**Context:** Tracked companies file roughly tens to a few hundred 10-K/10-Qs per day in earnings season [estimate; replace with the M10 measurement]. Freshness target ≤ 24 h. One consumer.
**Options considered:** (a) Kafka/MSK streaming; (b) a daily scheduler + SQS fan-out with a DLQ.
**Decision:** (b). Streaming is shown in Project 2.
**Consequences:** DRAFT (Claude; rewrite in your own words): The design stays simple: one scheduler, SQS fan-out with a DLQ, and one consumer, which is cheap and easy to explain. The cost is no replayable event log and no sub-hour freshness, so reprocessing means re-running from the lake and the DLQ needs monitoring. If requirements change (many consumers, tighter freshness), this ADR has to be revisited, and the earnings-season volume estimate should be replaced with the M10 measurement.
**How to revisit:** sub-hour freshness requirements, many independent consumers, or a need for replayable event history.

## ADR-020: `fact_key` excludes the taxonomy year (2026-11-17)
**Status:** draft by Claude from the decisions on record (2026-11-17); Shekar to review and finalize the wording
**Context:** `fact_key` must group every reported version of one fact across filings, because `facts_history`, `n_versions`, `is_restated` and version selection all work per `fact_key`. A restatement changes the taxonomy year in `version` (`us-gaap/2022` in the original filing, `us-gaap/2023` in a later one). Measured on the 7 local quarters (`docs/milestones/M01-dq-report.md`, "Curated facts"): 8,229,693 `curated.facts` rows cover 8,053,335 distinct facts, 5,420,567 with one version, 1,795,462 with two, 181,400 restated. These counts are with the year stripped; they were not recomputed with the year in the key, so how many facts the year would have split is NOT MEASURED.
**Options considered:** (a) key over the full `version` string; (b) key over the year-stripped taxonomy family (`us-gaap`, `dei`, `custom`); (c) key over a normalized concept ID from a standard-taxonomy mapping.
**Decision:** (b). `fact_key = xxhash64(cik, tag, taxonomy, period_end, qtrs, uom)` with `taxonomy` the year-stripped family (`docs/plan/05_data_contracts.md` section 2.2). Including the year would put a fact and its own restatement under different keys, so `facts_history` would never group them and no restatement would be detected.
**Consequences:** A tag whose meaning changes between taxonomy years now shares a key across the years. Custom tags are keyed by `custom` plus the tag and CIK. The Spark and DuckDB twins must derive `taxonomy` identically; the differential test covers that. Option (c) is not built.
**How to revisit:** if a tag redefinition across taxonomy years shows up as a false restatement, move to (c) for the affected tags.

> Numbering note: ADR-005 is already reserved for "one EC2 box vs Lambda + Aurora" (`docs/plan/03_architecture_and_stack.md`, planned list above), so ADR-020 is numbered 020. Renumber if you prefer; the only other reference is the `taxonomy` bullet in `docs/plan/M01_xbrl_lakehouse.md` section 3.5.

## ADR-021: The Spark fiscal and version twins are Claude-implemented against the differential test (2026-11-17)
**Status:** draft by Claude from the decisions on record (2026-11-17); Shekar to review and finalize the wording
**Context:** M1 planned `pipelines/spark/fiscal.py` and `versions.py` as Shekar-owned (CLAUDE.md rule 3, hook-protected). Shekar wrote the reference `pipelines/common/fiscal_ref.py` (180 unit and property tests, commit `6b5fa07`), then authorized Claude to implement the twins. Gate: `tests/integration/test_differential.py` asserts Spark == reference on seeded synthetic data.
**Options considered:** (a) Shekar writes both: most ownership, slowest. (b) Claude writes both natively: independent implementations, but more code. (c) `select_versions` native; `derive_fiscal_columns` runs the reference per company via `applyInPandas`: the rules cannot diverge, but its differential test only checks the plumbing, and executors need `pyarrow`, `setuptools` and `pandas<3`.
**Decision:** (c). `fiscal_ref.py` is human-written, owned by Shekar and hook-protected. `versions.py` is native window functions, an independent port of the rules. `fiscal.py` calls `derive_fiscal` per cik; `fact_key`, `fact_id`, `derived_from` and pass-through columns stay native (`xxhash64`). Both twins were implemented by Claude as a port and validated row for row by the differential test plus an independent per-company reference check on real data (`docs/milestones/M01-differential-analysis.json`, `reference_agreement`: the reference run directly over 8,193 companies and 12,201,161 `facts_history` rows differs from the DuckDB output in 0 rows).
**Consequences:** For `fiscal.py` the differential test proves the plumbing (row mapping, Q4 row construction, hashing), not the rules; for `versions.py` it is an independent check of the rules. The DuckDB twin is a second, separate implementation of both and is the check that found the two flaws behind ADR-022. One company's facts must fit one pandas group: skew on large filers is NOT MEASURED until the first EMR run. The earlier note that the label-offset mode tie "had no data hit" was wrong: on 7 real quarters it did (ADR-022).
**How to revisit:** if a company's pandas group exceeds executor memory on EMR, or the `applyInPandas` stage dominates the T2 run time, port `fiscal.py` to native Spark and keep the reference as the oracle.

## ADR-007: DuckDB vs Spark for XBRL normalization (2026-11-17)
**Status:** draft by Claude from the decisions on record (2026-11-17); Shekar to review and finalize the wording
**Context:** Same rules, two engines, run on Apple M4 Pro (12 cores, 24 GiB), one run per cell, benchmark at commit `546c0a8` (`docs/milestones/M01-benchmark.json`). Spark = `fsds_to_iceberg` (sub, num, tag, pre) then `normalize_facts`; DuckDB reads sub and num from the gzipped TSVs. Memory is the sampled peak over the process tree: indicative, not exact.

| Quarters | `facts_history` rows | DuckDB | Spark local: raw load | Spark local: normalize | EMR Serverless |
|---|---:|---|---|---|---|
| 2 (2023q3, 2023q4) | 3,320,915 | 7.98 s / 9,833 MB | 38.19 s / 1,072 MB | 112.07 s / 10,194 MB | pending — cloud phase |
| 7 (2022q1 to 2024q1, gaps) | 12,201,161 | 113.25 s / 9,930 MB | 125.21 s / 1,158 MB | 277.07 s / 11,734 MB | pending — cloud phase |
| 20 quarters, all quarters | NOT MEASURED | NOT MEASURED | NOT MEASURED | NOT MEASURED | pending — cloud phase |

DuckDB's time grows faster than the data (2 to 7 quarters); not investigated. Agreement on real data (`M01-differential-analysis.json`): the two engines differ in 0 rows in `facts_history`, `facts` and `facts_dimensional` on both 2 and 7 quarters, and the reference run directly over 8,193 companies differs from DuckDB's `facts_history` in 0 of 12,201,161 rows. Before ADR-022 (commit `bcb8cb5`, 7 quarters) the engines differed in 5,130 / 3,955 / 1,264 rows.
**Options considered:** (a) Spark only; (b) DuckDB only; (c) Spark as the pipeline, DuckDB as the benchmark twin and local check.
**Decision:** (c). Both were benchmarked. At this scale DuckDB is far faster (normalize alone: 7.98 s vs 112.07 s on 2 quarters, 113.25 s vs 277.07 s on 7), and its lead shrinks as the data grows, so Spark earns its place only at full-history, distributed scale. Spark stays the pipeline (Iceberg lake, EMR); DuckDB is the independent twin that checks it.
**Consequences:** The second engine found two flaws in the reference that its own tests had missed (ADR-022). Running both costs a second implementation of the rules to maintain; the differential keeps them in step. Whether a cluster pays off is not shown by these numbers: the EMR column, and the 20-quarter and all-quarter rows, are NOT MEASURED and pending the cloud phase.
**How to revisit:** when the EMR run and the all-quarter benchmark exist; if DuckDB's wall time on all quarters stays below the EMR run, reconsider (b).

## ADR-022: Deterministic ties and derived-Q4 eligibility and identity (2026-11-17)
**Status:** draft by Claude from the decisions on record (2026-11-17); Shekar to review and finalize the wording
**Context:** On 7 real quarters the Spark and DuckDB twins differed in 5,130 / 3,955 / 1,264 rows (`facts_history` / `facts` / `facts_dimensional`, both sides counted; `docs/milestones/M01-differential-analysis.json` at commit `f4daa7e`). By cause, summed over the tables: label-offset ties 9,792, fiscal-year ties 494 (+ 51 knock-ons), identity collisions 6. Two causes, both in the reference: (1) it broke ties (the label-offset mode, two FY rows of one filing after a fiscal-year change, 9M period-end groups) by list position, so its answer depended on input order; for three series it gave 2 different outputs over 60 random orders; (2) it derived Q4 from an FY row of any form, so a 10-Q's trailing-twelve-month value became a parent: 28,185 of 784,934 derived Q4 rows in the 7-quarter Spark `facts_history` came from forms other than 10-K, 10-K/A and 10-KT, and 175 `fact_id`s were shared by several rows, all involving a derived row.
**Options considered:** sort the reference's input vs give every `min`/`max` an explicit tie key vs leave ties undefined; restrict FY parents by form vs by whether the FY period end is a fiscal year end; put the 9M accession in the derived `fact_id` vs a flag column vs dropping derived rows from `facts_history`.
**Decision:** (1) Every input is sorted by (accession, value NULLs last, period end, quarters, cik, tag, taxonomy, uom) before any tie-sensitive step. The label-offset tie goes to the offset of the submission with the smallest accession; two FY rows go to the smaller value. These tie-breaks are stable, not business rules. (2) Derive Q4 only from an FY parent whose form is 10-K, 10-KT or 10-K/A, never from a 10-Q comparative. 20-F and 40-F are a deliberate scope exclusion, not an open question. (3) A derived Q4's `fact_id` is `xxhash64(fact_key, accession, nine_month_accession)`, so it cannot collide with a real fact. Rules 1 and 2 live in `pipelines/common/fiscal_ref.py` (applied by Shekar from `docs/proposals/ADR-022-fiscal_ref.diff`); rule 3 is in the twins and contract section 2.2.
**Consequences:** Derived Q4 rows in the 7-quarter Spark `facts_history` fell from 784,934 to 764,324, none from a non-annual form (was 28,185). 0 `fact_id`s are shared (was 175). Spark, DuckDB and the reference differ in 0 rows in all three tables on 2 and 7 quarters, with 0 unexplained differences; the reference run directly over 8,193 companies and 12,201,161 `facts_history` rows differs from DuckDB in 0 rows (2 quarters: 6,721 companies, 3,320,915 rows, 0). Q4 values that came from S-1s, 10-Qs, 20-Fs and 40-Fs no longer exist. Anything that stored a derived row's old `fact_id` must be rebuilt.
**How to revisit:** if Q4 from foreign annual reports or registration statements becomes a product requirement, widen the form set in `ANNUAL_FORMS`; if the tie rules matter to a real answer, replace them with a business rule.

## ADR-017: As-originally-reported vs restated values (2026-11-17)
**Status:** draft by Claude from the decisions on record (2026-11-17); Shekar to review and finalize the wording
**Context:** A company restates a figure in a later filing (a new 10-K, a 10-K/A, or the comparative column of the next year's report). Both values are in the data and finance users care which one an answer uses. Measured on the 7 local quarters (`docs/milestones/M01-dq-report.md`, "Curated facts"): 181,400 of 8,053,335 distinct facts are restated.
**Options considered:** (a) always the latest value; (b) always the original; (c) default to the as-originally-reported value, cite its native filing, and surface the restated value when the fact is restated.
**Decision:** (c). Answers default to the as-originally-reported value and cite the native filing, because that matches the cited document; when `is_restated` is true the restated value is surfaced next to it. `curated.facts` keeps both the latest and the original version per `fact_key` (one row when they have the same value, two otherwise); `facts_history` keeps every version (`docs/plan/05_data_contracts.md` section 2.2).
**Consequences:** The M4 gold set takes the original value for restated facts (`docs/plan/M04_eval_harness.md`). Answers about a restated fact carry two values and two citations.
**How to revisit:** if users ask for current values by default, flip the default and keep the same two columns.

## ADR-023: Filing fiscal labels reuse the fiscal_ref rules, so text and facts agree (2026-11-17)
**Status:** proposed
**Context:** `curated.filings` and `curated.chunks` need `fiscal_year` and `fiscal_period` (`docs/plan/05_data_contracts.md` section 2.2), and the engine joins text to numbers by those labels. `curated.facts` already labels facts with `fiscal_ref` (`y_end` plus a per-company label offset, M01 section 3.5 rules 1-4). A second, simpler rule for filings (for example `YEAR(period_of_report)`) would label a January-year-end retailer's 10-K one year off from its own facts. How many local filings would disagree: NOT MEASURED.
**Options considered:** (a) calendar year of `period_of_report`: simple, wrong for non-calendar fiscal years. (b) copy `fsds_sub.fy`/`fp`: matches the company's own label but exists only for filings in loaded FSDS quarters, so it cannot cover the full list. (c) apply the `fiscal_ref` functions to `period_of_report`, falling back to `fsds_sub.fy`/`fp` when the derivation is impossible.
**Decision:** (c). `fiscal_year = y_end(period_of_report, fye_mmdd) + label_offset(cik)`; `fiscal_period` is `FY` for 10-K and 10-K/A and the quarter label for 10-Q and 10-Q/A. The fallback and every disagreement are recorded in a new `curated.filings.dq_flags` column (contract section 2.2). A unit test asserts a filing's labels equal `derive_fiscal`'s labels for the same filing's facts.
**Consequences:** TODO(Shekar): write this in my own words
**How to revisit:** if the text-versus-facts agreement check finds filings whose labels differ from their facts' labels, or if companies with no 10-K in `fsds_sub` (offset assumed 0) turn out to be common, change the offset source.

## ADR-024: EMR Serverless runs a custom image with Python 3.12, pinned to the 7.14.0 base digest (2026-11-17)
**Status:** proposed
**Context:** The repo requires Python 3.12 (`pyproject.toml`), PySpark 3.5.x and Python code on the executors (`pipelines/spark/fiscal.py`, `parse_filings.py`). I ran the public base image `public.ecr.aws/emr-serverless/spark/emr-7.14.0` (arm64, digest `sha256:ee279ec3f6618c28cdf21c1b47cced8764ec87688c356041a5e53a4629e2d7ae`): default `python3` is 3.9.25, `python3.11` is 3.11.18, `python3.12` 3.12.15 is installable with `dnf`; it ships Spark 3.5.8-amzn-0 on Java 17.0.21 and Iceberg runtime 1.10.1-amzn-1 (`/usr/share/aws/iceberg/lib`, already on the driver and executor classpath; local runs pin 1.6.1). The AWS docs do not state the default Python per release (read 2026-11-17), so this is a measurement of one image, not a documented guarantee. Local PySpark is 3.5.9.
**Options considered:** (a) run on the default 3.9 and make `pipelines/` 3.9-compatible: rewrites annotations and Pydantic models and diverges from the tested runtime. (b) use the preinstalled 3.11: still not the tested minor. (c) a custom image that installs `python3.12` and the pinned dependencies, with `PYSPARK_PYTHON` pointing at it: one image reused by M1, M2 and M3, at the price of an ECR repo and a rebuild per release.
**Decision:** (c). `infra/emr/Dockerfile` starts from the base image digest above, installs `python3.12` and `infra/emr/requirements-emr.txt` (pins equal to `uv.lock`, checked by a test); driver and executors both use `/usr/bin/python3.12`, because a minor mismatch fails with `PYTHON_VERSION_MISMATCH`. The application's release label must match the image's.
**Consequences:** TODO(Shekar): write this in my own words
**How to revisit:** if a later EMR release ships Python 3.12 by default, drop the image; if the digest is withdrawn or Spark 3.5.8 behaves differently from local 3.5.9 in the first EMR run, rebuild on the new base and record it here.

## ADR-025: Pre-2020 SGML-wrapped primary documents are out of scope (2026-11-17)
**Status:** proposed
**Context:** The HTML corpus is S&P 500 10-K and 10-Q filings with report dates FY2020-2025; FinanceBench is the PDF path. Three 2011-2012 10-Ks (Apple, Walmart, 3M) parse to `no_sections` with 0 chunks (`docs/milestones/M02-fixture-survey.json`, `pre_2020_candidates`). Their primary documents start with an SGML wrapper (`<DOCUMENT><TYPE>10-K<SEQUENCE>...<TEXT><HTML>`); the unclosed wrapper tags nest the whole document, so `html_to_blocks` returns one block and no heading is seen. In the ingest window the same survey found 0 of 50 sampled S&P 500 10-Ks SGML-wrapped (`sgml_wrapped`) and 48 of 50 parsed `ok` (`parse_status`).
**Options considered:** (a) strip the wrapper or treat unknown block-containing tags as containers now: small, but code and a fixture for filings no question will cite. (b) scope it out and record the failure. (c) keep a 2011 filing as a permanently failing fixture: noise in CI.
**Decision:** (b). Known failure on pre-2020 SGML-wrapped primary documents; out of the FY2020-2025 ingest window; no fixture and no parser change. The three 2011-2012 candidates were not promoted to `tests/fixtures/`.
**Consequences:** TODO(Shekar): write this in my own words
**How to revisit:** if the corpus window is extended before about 2013, or any in-window filing parses to `no_sections` and starts with `<DOCUMENT>`; the fix is to strip the SGML header and trailer before parsing.
