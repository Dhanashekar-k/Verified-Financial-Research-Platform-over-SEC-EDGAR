# M10: Incremental ingestion (the platform stays current without you)

**Time:** about 8 h build + a 5-day measurement clock (fast track: one EventBridge-scheduled Lambda, about 5 h) · **Roles:** data engineering, SDE (idempotency, retries), cloud · **Depends on:** M2 (parsing library), M3 (loader), M9 (serving box)

## 1. Goal

New 10-K/10-Q filings from tracked companies become searchable within 24 h, with no manual steps. Rerunning a day changes nothing, missed days can be backfilled, and failures alert you.

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| Daily index `master.{YYYYMMDD}.idx` | DAG `filings_daily_incremental` (local Airflow → MWAA Serverless; Step Functions fallback) |
| submissions + companyfacts APIs | Lambdas `discover`, `process_filing`, `merge`, `companyfacts_refresh` (arm64 container images) |
| `pipelines/parsing`, the M1 Python fiscal reference | SQS `filings` + DLQ; SQS `serving-loads`; SNS email alarms |
| Loader on EC2 | `loader-incremental` systemd service (long-polls `serving-loads`) |
| — | `ingest_log` (per accession: `accepted_at`, `processed_at`, `merged_at`, `searchable_at`, status) and a **T10 freshness report over 5+ days** |

## 3. Design

```
EventBridge Scheduler (daily, time chosen from measurement, see 3.6)
  └─> DAG run for logical date D  (catchup=True → missed days backfill automatically)
       1. discover(D)        Lambda: read master.D.idx (404 → no filings, success) → filter forms
                             {10-K,10-K/A,10-Q,10-Q/A} ∩ tracked CIKs → resolve primary doc via the
                             submissions API → manifest s3://…/manifests/D.json → SQS messages
       2. wait_for_drain     sensor (reschedule mode): queue empty AND staging outputs + DLQ count == manifest
       3. merge(D)           Lambda (PyIceberg + Glue catalog): upsert staging → curated.filings,
                             curated.chunks, curated.chunk_embeddings; write a delta export; send
                             a message to serving-loads
       4. companyfacts(D)    Lambda: companyfacts JSON for CIKs that filed on D → your M1 Python
                             fiscal/dedup reference → upsert curated.facts_history / facts → delta export
SQS filings → process_filing  Lambda (reserved concurrency 2, ≤ 4 req/s each → ≤ 8 req/s total):
                             fetch HTML → S3 raw → parse/section/chunk (pipelines/parsing) → fastembed
                             (model baked into the image) → staging Parquet. Idempotent by accession + sha256.
                             3 attempts → DLQ → alarm.
EC2 loader-incremental        long-polls serving-loads → INSERT … ON CONFLICT DO NOTHING / DO UPDATE into
                             the live version tables → bump corpus_state.minor → the cache key includes
                             it → stale cached answers age out → record searchable_at
```

Why this shape: SQS gives per-filing retries and a DLQ. Reserved concurrency enforces SEC's rate limit across workers. The Airflow logical date makes "process day D" idempotent and back-fillable. **No Kafka:** about 50–300 filings/day in earnings season [Guessing] is well within SQS + daily batch (ADR-004).

## 4. Steps

1. **Lambda images (2 h):** one arm64 image with `pipelines/` + fastembed + model weights, plus separate handlers. Terraform: functions (memory 2–3 GB for `process_filing`; timeout 5 min), SQS + DLQ (`maxReceiveCount=3`), event source mapping (batch size 1, max concurrency 2), IAM least privilege, log retention 7 days. **The Lambdas run outside the VPC (no NAT).**
2. **Handlers (2 h):** the four above. Each is idempotent: it checks its output before working and writes atomically (temp key → final key).
3. **DAG (1.5 h):**
   - Write it as **YAML in dag-factory format**. MWAA Serverless uses YAML workflow definitions [Likely; confirm in the MWAA Serverless docs], and the same YAML runs locally with the `dag-factory` package under `airflow standalone`.
   - Operators: Lambda invoke and an SQS sensor in **reschedule mode** (so you aren't billed while waiting [Likely]).
   - `catchup: true`, `start_date` = the day you deploy, `max_active_runs: 1`.
   - **Decision gate:** time-box MWAA Serverless to 2 h. If operator support, IAM or regional availability blocks you, switch to **Step Functions** (discover → wait loop with a Choice state → merge → companyfacts) triggered by EventBridge Scheduler, and record the switch in ADR-013.
4. **Loader-incremental (1 h):** systemd service on EC2; long-polls `serving-loads`; applies deltas in one transaction per accession; HNSW accepts inserts; bumps `corpus_state.minor` and writes `searchable_at`.
5. **Alarms (0.5 h):** CloudWatch alarm on DLQ `ApproximateNumberOfMessagesVisible > 0` → SNS email; DAG/state-machine failure → EventBridge rule → SNS.
6. **Weekly lake maintenance (0.5 h):** compact the small files from daily appends and expire old snapshots. Use Athena `OPTIMIZE … REWRITE DATA USING BIN_PACK` / `VACUUM` on the Iceberg tables [Likely; check that Athena supports both for your table settings] or a small weekly EMR Serverless job.
7. **Measure when the daily index appears (start on day 1):** a tiny scheduled Lambda logs the first time `master.D.idx` returns 200. Choose the DAG schedule from that data. If the measured lag would exceed 24 h for early-day filings, add an intraday poll of the submissions API for tracked CIKs (every 4 h) [Unclear: depends on SEC's publish time; that's why you measure it].

## 5. Tests

- Handlers: unit tests with recorded SEC responses (respx) and moto for S3/SQS.
- **Idempotency:** process day D twice → identical row counts and no duplicate `chunk_id`s. Run catch-up over D−3…D → the same final state as running each day once.
- **Poison message:** a filing that fails parsing goes to the DLQ after 3 attempts, and the DAG still completes for the others (the drain condition counts the DLQ).

## 6. Claude Code session plan

```
/milestone M10
Step 1: Lambda image + handlers discover/process_filing/merge/companyfacts per M10 §3–4, reusing
pipelines/parsing and my fiscal reference implementation. moto + respx tests including idempotency
and the poison-message case. Plan first.
```
```
Step 2: Terraform for Lambdas, SQS+DLQ, event source mapping with max concurrency 2, alarms, SNS,
EventBridge Scheduler. fmt/validate only.
```
```
Step 3: The DAG in dag-factory YAML + a local runner under airflow standalone; then the MWAA
Serverless deployment (Terraform or CLI). If you hit a blocker that needs more than small changes,
stop and tell me. We'll switch to the Step Functions fallback.
```
```
Step 4: loader-incremental systemd service + ingest_log + make freshness-report (p50/p95/max lag by day).
```

## 7. Definition of done

- [ ] Five consecutive days of automated runs with no manual intervention (in M11's window).
- [ ] **T10** freshness report: p50/p95/max `accepted_at → searchable_at`, with any > 24 h explained.
- [ ] Idempotency and catch-up tests green; one deliberate failure produced an alarm email.
- [ ] Cost of the incremental path for the 5 days, from Cost Explorer by tag (expected ≈ $0) [Likely].

## 8. Pitfalls

- Concurrency across Lambdas breaking SEC's 10 req/s. Reserved concurrency × per-function rate must stay ≤ 8.
- Daily-index file names and timing: weekends and holidays have no index (handle 404 as success).
- Iceberg commit conflicts if multiple writers commit at once. Only `merge` commits; workers write staging only.
- Upserts into the live HNSW table during peak traffic are fine at this volume, but say so in an ADR.

## 9. Interview prep

- Why Airflow's data-interval semantics matter (idempotent "process day D" and catch-up).
- How you enforce a global rate limit across distributed workers.
- Why the workers write staging and a single merger commits (Iceberg optimistic concurrency).
- Why not Kafka; what volume would change your mind (continuous intraday streams, many consumers, replay needs).
