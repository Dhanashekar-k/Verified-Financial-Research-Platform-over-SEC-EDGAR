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
**Status:** proposed (stub: Shekar fills in the body)
**Context:** TODO(Shekar). Prompts: what `fact_key` has to do (group every reported version of one fact across filings); what changes in `version` between an original filing and a later restatement (`us-gaap/2022` vs `us-gaap/2023`); what happens to `facts_history` and `n_versions` if the year is part of the key. Measured on the 7 local quarters (`docs/milestones/M01-dq-report.md`, section Curated facts): 8,071,662 distinct facts (`fact_key`) in 8,250,114 rows; 5,436,692 of them have one version, 1,797,588 have two; 183,653 are restated. These counts are with the year stripped; they were not recomputed with the year in the key, so how many facts the year would have split is NOT MEASURED.
**Options considered:** TODO(Shekar). (a) key over the full `version` string; (b) key over the year-stripped taxonomy family (`us-gaap`, `dei`, `custom`); (c) key over a normalized concept ID from a standard-taxonomy mapping.
**Decision:** TODO(Shekar). Current contract: `fact_key = xxhash64(cik, tag, taxonomy, period_end, qtrs, uom)` with `taxonomy` year-stripped (`docs/plan/05_data_contracts.md` §2.2).
**Consequences:** TODO(Shekar). Prompts: a tag whose meaning changes between taxonomy years now shares a key; custom tags are keyed by `custom` plus the tag and CIK; the Spark and DuckDB twins must derive `taxonomy` identically.
**How to revisit:** TODO(Shekar).

> Numbering note: ADR-005 is already reserved for "one EC2 box vs Lambda + Aurora" (`docs/plan/03_architecture_and_stack.md`, planned list above), so this one is ADR-020. Renumber if you prefer; the only other reference is the `taxonomy` bullet in `docs/plan/M01_xbrl_lakehouse.md` §3.5.

## ADR-021: The Spark fiscal and version twins are Claude-implemented against the differential test (2026-11-17)
**Status:** proposed (stub: Shekar writes the Consequences)
**Context:** M1 planned `pipelines/spark/fiscal.py` and `versions.py` as Shekar-owned (CLAUDE.md rule 3, hook-protected). Shekar wrote the reference `pipelines/common/fiscal_ref.py` (180 unit and property tests, commit `6b5fa07`), then authorized Claude to implement the twins. Gate: `tests/integration/test_differential.py` asserts Spark == reference on seeded synthetic data. Result: 141 integration and 440 unit and property tests passed.
**Options considered:** (a) Shekar writes both: most ownership, slowest. (b) Claude writes both natively: independent implementations, but more code. (c) `select_versions` native; `derive_fiscal_columns` runs the reference per company via `applyInPandas`: rules cannot diverge, but its differential test only checks the plumbing, and executors need `pyarrow`, `setuptools` and `pandas<3`.
**Decision:** (c). `versions.py` is native window functions, an independent check of the rules. `fiscal.py` calls `derive_fiscal` per cik; `fact_key`, `fact_id`, `derived_from` and pass-through columns stay native (`xxhash64`). Only `fiscal_ref.py` stays Shekar-owned and hook-protected.
**Consequences:** TODO(Shekar): write this in my own words. Prompts: what the differential test proves for each twin; one company's facts must fit one pandas group (skew on large filers: NOT MEASURED until the first EMR run); the `label_offsets` mode tie follows input order in the reference (known, no data hit).
**How to revisit:** if a company's pandas group exceeds executor memory on EMR, or the `applyInPandas` stage dominates the T2 run time, port `fiscal.py` to native Spark and keep the reference as the oracle.

## ADR-007: DuckDB vs Spark for XBRL normalization (2026-11-17)
**Status:** proposed (stub: Shekar writes the conclusion)
**Context:** Same rules, two engines, run on Apple M4 Pro (12 cores, 24 GiB), one run per cell, benchmark at commit `546c0a8` (`docs/milestones/M01-benchmark.json`), difference analysis at `f86aaf6`. Spark = `fsds_to_iceberg` (sub, num, tag, pre) then `normalize_facts`; DuckDB reads sub and num from the gzipped TSVs. Memory is the sampled peak over the process tree: indicative, not exact.

| Quarters | `facts_history` rows | DuckDB | Spark local: raw load | Spark local: normalize | EMR Serverless |
|---|---:|---|---|---|---|
| 2 (2023q3, 2023q4) | 3,320,915 | 7.98 s / 9,833 MB | 38.19 s / 1,072 MB | 112.07 s / 10,194 MB | pending — cloud phase |
| 7 (2022q1 to 2024q1, gaps) | 12,201,161 | 113.25 s / 9,930 MB | 125.21 s / 1,158 MB | 277.07 s / 11,734 MB | pending — cloud phase |
| 20 quarters, all quarters | NOT MEASURED | NOT MEASURED | NOT MEASURED | NOT MEASURED | pending — cloud phase |

DuckDB's time grows faster than the data (2 to 7 quarters); not investigated. Agreement on real data (`docs/milestones/M01-benchmark.json` `comparisons`, `M01-differential-analysis.json`): after ADR-022 the two engines differ in 0 rows in `facts_history`, `facts` and `facts_dimensional` on both 2 and 7 quarters; the reference (with the ADR-022 sort and eligibility around it) run directly over 8,193 companies differs from DuckDB's `facts_history` in 0 of 12,201,161 rows; two Spark runs of the same 7 quarters differ in 0 rows. Before ADR-022 (commit `bcb8cb5`, 7 quarters) they differed in 5,130 / 3,955 / 1,264 rows, from ties the reference broke by input order and from derived Q4 rows that shared a `fact_id` with real rows.
**Options considered:** TODO(Shekar). (a) Spark only; (b) DuckDB only; (c) Spark as the pipeline, DuckDB as the benchmark twin and local check.
**Decision:** TODO(Shekar).
**Consequences:** TODO(Shekar): write this in my own words. Prompts: what the numbers say about when a cluster pays off; what it means that the differential against a second engine found two flaws in the reference that its own tests had not.
**How to revisit:** TODO(Shekar). The EMR column and the 20-quarter and all-quarter rows are filled in the cloud phase.

## ADR-022: Deterministic ties and derived-Q4 eligibility and identity (2026-11-17)
**Status:** proposed (stub: Shekar writes the body)
**Context:** On 7 real quarters the Spark and DuckDB twins differed in 5,130 / 3,955 / 1,264 rows (`facts_history` / `facts` / `facts_dimensional`, both sides counted; `docs/milestones/M01-differential-analysis.json` at commit `f4daa7e`). By cause, summed over the tables: label-offset ties 9,792, fiscal-year ties 494 (+ 51 knock-ons), identity collisions 6. Two causes, both in the reference `pipelines/common/fiscal_ref.py`: (1) it breaks ties (the label-offset mode, two FY rows of one filing after a fiscal-year change, 9M period-end groups) by list position, so its answer depends on input order; for three series the reference itself gave 2 different outputs over 60 random orders; (2) it derives Q4 from an FY row of any form, so a 10-Q's trailing-twelve-month value became a parent: 28,185 of 784,934 derived Q4 rows in the 7-quarter Spark `facts_history` came from forms other than 10-K, 10-K/A and 10-KT, and 175 `fact_id`s were shared by several rows, all involving a derived row. After (benchmark `546c0a8`, analysis `f86aaf6`, the same files): the twins differ in 0 rows in `facts_history`, `facts` and `facts_dimensional` on both 2 and 7 quarters (7 quarters: 0 / 0 / 0 rows, unexplained 0); the reference with the two rules around it, run directly over 8,193 companies and 12,201,161 `facts_history` rows, differs from DuckDB in 0 rows (2 quarters: 6,721 companies, 3,320,915 rows, 0); 0 `fact_id`s are shared; derived Q4 rows in the 7-quarter Spark `facts_history` fell from 784,934 to 764,324, none from a non-annual form (was 28,185). The 17 strict-xfail tests in `tests/unit/test_fiscal_reference_proposed.py` flip when `docs/proposals/ADR-022-fiscal_ref.diff` is applied.
**Options considered:** TODO(Shekar). Prompts: sort the reference's input vs give every `min`/`max` an explicit tie key vs leave ties undefined and test only what is defined; restrict FY parents by form vs by whether the FY period end is a fiscal year end; put the 9M accession in the derived `fact_id` vs a flag column vs dropping derived rows from `facts_history`.
**Decision:** TODO(Shekar). Implemented in the twins so far (the reference is unchanged until Shekar applies `docs/proposals/ADR-022-fiscal_ref.diff`): (1) every input is sorted by (accession, value NULLs last, period end, quarters, cik, tag, taxonomy, uom) before any tie-sensitive step; the label-offset tie goes to the offset of the submission with the smallest accession; (2) an FY parent's form must be in 10-K, 10-KT, 10-K/A; (3) a derived Q4's `fact_id` is `xxhash64(fact_key, accession, nine_month_accession)`. Contract section 2.2 and the M1 runbook rules 3 and 5 were updated.
**Consequences:** TODO(Shekar): write this in my own words. Prompts: what the rule changes for users (Q4 values that existed before and no longer do); whether a fiscal-year change should pick the smaller value; what the `fact_id` change does to anything that stored old ids.
**How to revisit:** TODO(Shekar).
