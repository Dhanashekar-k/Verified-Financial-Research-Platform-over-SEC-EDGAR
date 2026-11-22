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
