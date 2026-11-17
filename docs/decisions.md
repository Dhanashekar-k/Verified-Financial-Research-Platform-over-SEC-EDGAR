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
**Context:** TODO(Shekar). Prompts: what `fact_key` has to do (group every reported version of one fact across filings); what changes in `version` between an original filing and a later restatement (`us-gaap/2022` vs `us-gaap/2023`); what happens to `facts_history` and `n_versions` if the year is part of the key.
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
