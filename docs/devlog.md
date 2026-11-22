# Dev log

Running narrative, newest entry last. Heavy decisions live in `docs/decisions.md` (ADRs); this file holds
the day-to-day calls and progress. Append one dated entry at the end of each working session.

## 2026-11-21 — M2 complete
- **Done:** the full S&P 500 corpus (13,239 filings) is fetched and parsed on AWS Glue into the real `curated` tables (2.86M chunks, equal to `curated.filings.n_chunks`, 0 orphans). `curated.companies.in_financebench` now populates (32 companies).
- **Fixed:** the 10-Q part-state and implied-`P1-1` section bugs (ADR-031), and the O(n²) merge delete in `merge_curated` (anti-join instead of `NOT IN`), which had timed the first re-parse out.
- **Result:** parse success 88.5% strict / 99.06% lenient, closed on the DoD "or documented" branch (residual in ADR-028/031); section accuracy 49/50 = 98% (n=50, in-sample).
- **Decisions:** stopped chasing the parse-success residual and documented it instead; relaxed the coverage gate to "no filing loses body text" (heading lines moved into `section_title`), recorded in ADR-031.
- **Next:** M3 (embeddings + serving store), on Glue or local fastembed, since EMR is Org-blocked.

## 2026-11-21/22 — M3 stage A: embedding job built, smoke-tested on Glue
- **Done:** contract-first commit (corpus_versions, manifest fields, previous_version/retention rules, cloud pgvector >= 0.8.0), ADR-032..036; pinned quantized-bge embedder + sha-verified S3 model staging; `embed_chunks` Glue job (30 integration tests on the real model); Glue rate sourced ($0.44/DPU-hour, `infra/pricing/glue.yaml`); model staged in S3, bundle deployed, `edgarqa-embed-chunks` created (terraform plan 1 add / 0 change / 0 destroy).
- **Found:** `terraform.tfvars` had no `smoke_databases`, so the first plan would have destroyed `corpus_smoke`; added it. A 2,000-chunk smoke on 4 G.1X: SUCCEEDED, 1,861 DPU-s = $0.2275, pip install fine on Python 3.11, cross-arch min cosine 0.9999994.
- **Decision pending:** the embedding stage is real inference (353 s for 4 tasks of 500 chunks, about 1.4 chunks/s/core on Glue vs 11.4 on the Mac), so embedding is far dearer than the $10 gate for the unserved 10-Q chunks (estimate $26-51). Sorting by length with batch 32 is 1.56x faster locally with the same vectors. Full run NOT started.
- **Next:** decide the 10-Q gate, then (optionally) a second small smoke with the batching fix, then the served-subset run; stages B-D after.

## 2026-11-22 — M3 stage A: scope decision, batching fix measured on Glue
- **Decided (Shekar):** 10-Q / 10-Q/A are not embedded (ADR-037); "complete" is judged over 10-K, 10-K/A and FinanceBench (1,097,376 chunks); the manifest records the exclusion (1,759,310 chunks).
- **Done:** length-sorted batches of 32 in the embedder and job; `--scope served` is the Glue default; per-partition worker diagnostics. Three more 2,000-chunk smokes in `corpus_smoke` (A, B, C; total smoke spend with the first one $0.61).
- **Measured on Glue (12 cores busy):** sorted batch 32 = 2.40 chunks/s/core, unsorted batch 32 = 2.02 (sorting alone 1.19x), the first smoke's unsorted batch 256 = 1.42 (but only 4 cores busy). The sorted-32 vectors match the batch-256 ones to cosine 0.99999994. Estimated served run at 19 executors: about $1.34 per 100k chunks, about $14.7, about 1.7 h of compute.
- **Failure (control only):** smoke A (unsorted batch 256 with 12 tasks) lost all executors ("containers exceeding thresholds"; the replacements were refused by the run's own 4-worker cap). Batch 256 does not fit 4 concurrent workers per executor; batch 32 ran clean at the same load. Not the served configuration.
- **Left as a hardware characteristic (not rabbit-holed):** a Glue core is about 7.4x slower than an M-series core at the same settings (2.40 vs 17.8 chunks/s). The Glue executors are Xeon Platinum 8259CL at 2.5 GHz with 4 vCPUs each, no VNNI flag visible; hyperthreading and the int8 path are both plausible, not separated.
- **Next:** wait for the go-ahead on the served run (4 waves of 300,000 at 20 workers), then B-D.
