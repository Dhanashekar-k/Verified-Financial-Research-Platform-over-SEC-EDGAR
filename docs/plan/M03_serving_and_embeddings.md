# M3: Embeddings and the serving store

**Time:** about 12 h (fast track about 8 h) · **Roles:** big data + ML (distributed inference), SDE (atomic loads, indexing) · **Depends on:** M1, M2

> **Amended 2026-11-21 (see ADR-032 to ADR-035 and `05_data_contracts.md`).** Where this runbook says EMR, use **AWS Glue 5.0** (ADR-027): no custom image, the model files are staged in S3 and pinned by sha256 (ADR-032). `query_embed` adds **no** BGE prefix for this model, so queries go through our `embed_queries` wrapper (ADR-033). The real load, index build and bench run on a **cloud** Postgres (EC2 `m7i.large` + Docker `pgvector/pgvector:pg17`, ADR-034), not locally; Testcontainers is for tests only. Degenerate chunks (`n_tokens <= 5`) are embedded but not exported (ADR-035). EDGAR 10-Q and 10-Q/A chunks are deliberately not embedded (ADR-037), so "`chunk_embeddings` complete (missing = 0)" means complete over the embedding scope (10-K, 10-K/A, FinanceBench). Swap/rollback/retention follow the `corpus_state` semantics in the contract (keep `live` and `previous`, not "N-1/N-2").

## 1. Goal

Fast lookups for numbers and text: chunk embeddings computed at scale, a versioned Parquet export from the lake, and a Postgres serving store loaded with an **atomic version swap**, with HNSW recall and latency measured.

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| `curated.chunks`, `curated.facts`, `curated.companies` | `curated.chunk_embeddings` (384-d, `BAAI/bge-small-en-v1.5`) |
| Custom EMR image from M2 (+ fastembed and the model baked in) | `s3://<lake>/exports/serving/corpus_version=N/…` + `_MANIFEST.json` |
| — | Postgres `facts_vN`, `chunks_vN`, views `facts`/`chunks`, `corpus_state` |
| — | `docs/milestones/M03-report.md`: embedding throughput and $, row counts, index build time, HNSW recall@10 vs exact, lookup p50/p95 |

## 3. Steps

### 3.1 Embedding job: option A, distributed on Spark (recommended for the big-data story; about 4 h)
- `pipelines/spark/embed_chunks.py`: `mapInPandas` over `curated.chunks` rows whose `chunk_id` is missing from `curated.chunk_embeddings` (**incremental by design**).
- Inside each partition: load the fastembed `TextEmbedding("BAAI/bge-small-en-v1.5")` **once per Python worker** (a module-level cache), set threads to 1 per task to avoid CPU oversubscription, and embed in batches of 256 with `passage_embed`. Output `array<float>` with L2 normalization.
- Bake the model weights into the custom EMR image (no internet download at runtime).
- Measure **T2b**: chunks/minute, vCPU-hours, $ per 100k chunks.
- **Fallback (option B, fast track):** run the same function on your Mac over a Parquet export of `curated.chunks`, then write `chunk_embeddings` with Spark locally. Same model and code, so the vectors are identical.
- **Queries must use `query_embed`** (BGE uses a query instruction prefix for retrieval [Likely]; fastembed applies it). Write a unit test that the API uses `query_embed` and the pipeline uses `passage_embed`.

### 3.2 Export job (2 h)
`pipelines/spark/export_serving.py --version N`:
- **Facts:** companies in S&P 500 ∪ FinanceBench ∪ any eval-set company; `taxonomy ∈ {us-gaap, dei, srt}`; `uom ∈ {USD, shares, USD/shares, pure}`; rows with `is_latest OR is_original`. Record the row count (expect about 10^7 [Guessing]).
- **Chunks:** join chunks with embeddings (inner join; report chunks missing embeddings, which should be 0).
- Write Parquet (zstd) + `_MANIFEST.json` with row counts, embedding model and Iceberg snapshot IDs (`05_data_contracts.md` §2.3).
- `corpus_version` = previous max + 1 (kept in a tiny Iceberg table `curated.corpus_versions`).

### 3.3 Loader with atomic swap (4 h)
`serving/load/load_snapshot.py --version N --source s3://… | ./exports/…`:
1. Create `facts_vN` and `chunks_vN` from `serving/sql/templates/*.sql` (no indexes yet).
2. Bulk load with **psycopg COPY** (stream Parquet with pyarrow and write `halfvec` as `[x1,…,x384]` text). Alternatively use DuckDB's Postgres extension. Pick one and write the ADR.
3. Build indexes **after** loading: B-trees, the GIN index on `tsv`, then HNSW (`m=32, ef_construction=128` after measurement, ADR-040; the runbook's `m=16, ef_construction=64` is the starting point). Session settings for the build: `maintenance_work_mem='1GB'`, `max_parallel_maintenance_workers=2`. On a 4 GiB box, temporarily resizing to `t4g.large` for the build and back is a cheap trick [Likely].
4. **Validate** before swapping: row counts equal the manifest; 20 sample fact lookups; 20 sample text searches return rows.
5. **Swap in one transaction:** `CREATE OR REPLACE VIEW facts …; CREATE OR REPLACE VIEW chunks …; UPDATE corpus_state SET previous_version=live_version, live_version=N, swapped_at=now();`
6. Keep the live and the previous version for rollback (`load_snapshot.py --rollback`) and drop every other `*_vK` (not "N−2": after a rollback N−2 can be the live version).
- Postgres settings for 4 GiB (`serving/postgres.conf`): `shared_buffers=1GB`, `effective_cache_size=2560MB`, `work_mem=16MB`, `random_page_cost=1.1`, `max_connections=50` [Likely starting points; tune them].

### 3.4 Retrieval primitives and measurements (2 h)
`app/engine/retrieval/{lexical,dense,fuse}.py` (used fully in M5):
- **Lexical:** `websearch_to_tsquery('english', $q)` with `ts_rank_cd` over `chunks`, filtered by `cik`/`fiscal_year` when the plan has them; top 50.
- **Dense:** `SET LOCAL hnsw.ef_search = 100; SET LOCAL hnsw.iterative_scan = relaxed_order;` then `ORDER BY embedding <=> $qvec LIMIT 50` with the same filters. Iterative scans exist to prevent over-filtering with HNSW [Certain].
- **RRF fusion:** `score = Σ 1/(60 + rank)`.
- `serving/bench/retrieval_bench.py`:
  - **HNSW recall@10 vs exact:** 200 random queries (use chunk-derived pseudo-queries), comparing the HNSW top-10 with exact search (`SET enable_indexscan = off` or a brute-force scan). Report recall at ef_search ∈ {40, 100, 200} and the latency of each.
  - **Latency:** numeric lookup (one metric, one company, one period) p50/p95 over 200 queries; hybrid search (lexical + dense + fuse, no rerank) p50/p95 over 100 queries.

## 4. Claude Code session plan

```
/milestone M3
Step 1: embed_chunks.py per M03 §3.1 (mapInPandas, a per-worker model cache, threads=1, batch 256,
incremental on missing chunk_ids). Test locally on 2,000 chunks. Print throughput.
Then extend the custom EMR image Dockerfile from M2 to bake in fastembed + the model weights.
```
```
Step 2: export_serving.py per §3.2 and the manifest. Then serving/sql templates from
docs/plan/05_data_contracts.md §3 (parameterized by version).
```
```
Step 3: load_snapshot.py per §3.3 with validation, the atomic swap, rollback and drop of N-2.
Integration test with Testcontainers Postgres (pgvector image): load a tiny version 1, swap,
load version 2, swap, roll back to 1. Assert views and corpus_state each time.
```
```
Step 4: retrieval primitives + retrieval_bench.py per §3.4. Run on the local stack and
write the numbers into docs/milestones/M03-report.md from the bench's JSON output only.
```

## 5. You write

- The atomic-swap design (Claude implements it; you write ADR-018 "Versioned tables + view swap" and can explain the failure modes: a crash mid-load, a crash mid-swap, a long-running query during the swap).
- The interpretation of the HNSW recall and latency trade-off; pick the `ef_search` default.

## 6. Definition of done

- [ ] `curated.chunk_embeddings` complete (missing = 0); T2b recorded (option A) or option B used and noted.
- [ ] Export `corpus_version=1` with manifest; loader swaps it into local Postgres; rollback tested.
- [ ] HNSW recall@10 ≥ 0.95 at the chosen `ef_search` (or documented).
- [ ] Numeric lookup p95 < 50 ms; hybrid search (no rerank) p95 < 200 ms, measured on the cloud box with a co-located client (instance type recorded; re-measure on the final box in M9).
- [ ] Integration test for load/swap/rollback green in CI.

## 7. Pitfalls

- Using `passage_embed` for queries (silent quality loss).
- Building HNSW before bulk loading (much slower) [Likely].
- Filtered ANN returning too few rows without iterative scans.
- `halfvec` dimension mismatch between the export and the DDL (fail fast on the manifest `dim`).
- A loader that isn't idempotent: rerunning version N must drop and recreate `*_vN`, never append.

## 8. Interview prep

- Why `halfvec`: about 2× memory saving, with the recall you measured.
- HNSW parameters (`m`, `ef_construction`, `ef_search`) and the recall/latency curve you measured.
- Why version tables + a view swap instead of `TRUNCATE` + load (zero-downtime, rollback).
- How you'd scale the vector store 100× (partition by fiscal year or company, a dedicated vector DB, or pgvector on a bigger box with read replicas), with the trade-offs.
