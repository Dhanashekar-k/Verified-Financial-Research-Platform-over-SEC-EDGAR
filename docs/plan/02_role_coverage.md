# 02: Role coverage (FDE, SDE, AI engineer) and the resume story

The conversation about why you weren't fitting FDE, SDE and AI-engineer roles happened in a different project, so I can't open it from here. This file maps the project to what those roles typically screen for, based on general hiring patterns [Likely]. If that conversation found specific gaps, check them against §2 and tell me what's missing.

---

## 1. What each role screens for, and the evidence this project produces

### Forward-deployed engineer (FDE)
They screen for: taking an ambiguous customer problem to a working deployment; integrating messy real-world data; shipping fast and iterating with real users; explaining technical trade-offs to non-engineers; and owning outcomes rather than tickets [Likely].

| Signal | Evidence in this project | Milestone |
|---|---|---|
| Messy real-world data integration | SEC FSDS + companyfacts + HTML filings + FinanceBench PDFs; fiscal-period and amendment rules | M1, M2 |
| Real users, feedback loop | ≥ 5 testers, logged questions, top-3 failure types fixed, before/after numbers | M11 |
| Customer onboarding | `docs/onboarding.md`; bring-your-own-documents tenant (stretch) | M11, S2 |
| Deployment ownership | Live demo, runbook, `make deploy/destroy`, spend guard | M9 |
| Communication | README written for a non-engineer, decisions log, 3-minute video | M12 |

### Software engineer (SDE / SWE)
They screen for: system design trade-offs; API design; testing discipline; reliability (idempotency, retries, rate limits); performance measured under load; CI/CD; and readable, maintainable code [Likely].

| Signal | Evidence | Milestone |
|---|---|---|
| API design | Versioned `/v1/ask`, OpenAPI, typed errors, idempotent ingestion keyed by accession | M8, M10 |
| Testing | Unit, property-based (Hypothesis) on the number normalizer, integration with Testcontainers, stubbed LLM | M5–M8 |
| Reliability | Token-bucket rate limit, spend circuit breaker, SQS DLQ, idempotent loads, atomic corpus swap | M3, M8, M10 |
| Performance | k6 load tests per route, OpenTelemetry latency breakdown, cache hit rate | M8 |
| Infra as code + CI/CD | Terraform, GitHub Actions with OIDC (no long-lived keys), arm64 images in ECR | M0, M9 |
| Design judgment | 15+ ADRs including "no Kafka" and "DuckDB vs Spark" | All |

### AI engineer (applied LLM)
They screen for: RAG beyond the tutorial level; evaluation rigor; cost/latency/quality trade-offs; structured outputs and tool use; guardrails and hallucination control; and some ML fundamentals [Likely].

| Signal | Evidence | Milestone |
|---|---|---|
| Eval rigor | Four eval sets, baseline-first, bootstrap CIs, judge calibrated with κ, ablations | M4, M6 |
| Hallucination control | Verifier: provenance re-execution, quote-substring check, entailment, refusals | M6 |
| Structured generation | Typed query plan via structured outputs; claims schema | M5 |
| Cost engineering | Model-tier Pareto, prompt caching, Batch API for evals, cascade router | M5, M7 |
| Retrieval quality | Hybrid lexical + dense + rerank, evidence-page recall@k on FinanceBench | M3–M5 |
| ML fundamentals | Router: data provenance, splits, calibration, ONNX serving; S1 anomaly detection | M7, S1 |

## 2. Keyword coverage: big data, cloud, ML, LLMs

Only list a keyword on your resume if the project actually exercises it and you can whiteboard it.

| Area | Technologies exercised | Where you can go deep |
|---|---|---|
| **Big data** | Apache Spark (PySpark 3.5), Apache Iceberg (partitioning, snapshots, time travel, MERGE), Parquet, AWS Glue Data Catalog, Amazon Athena, EMR Serverless, DuckDB (benchmark), Spark pandas UDFs, Spark MLlib (S1) | Fiscal-period derivation with window functions; dedup and amendment semantics; Iceberg snapshot rollback; the crossover point between DuckDB and Spark |
| **Cloud (AWS)** | S3, EMR Serverless, Glue Catalog, Athena, EC2 (Graviton), ECR, CloudFront, Lambda, SQS (+ DLQ), EventBridge Scheduler, MWAA Serverless or Step Functions, IAM (least privilege, OIDC), SSM Parameter Store and Session Manager, CloudWatch, Budgets, Terraform | Why no NAT gateway; why one EC2 box rather than Lambda + Aurora; cost per component; teardown |
| **ML** | scikit-learn, PyTorch / Hugging Face (optional fine-tune), sentence embeddings, cross-encoder reranking, MLflow tracking, ONNX Runtime serving, calibration (ECE), bootstrap statistics, Spark MLlib GBT (S1) | Router distillation and cascade threshold selection; label noise; why macro-F1; calibration |
| **LLMs** | Claude API (Haiku 5.5 / Sonnet 5.5), structured outputs, prompt caching, Message Batches, RAG, hybrid retrieval, LLM-as-judge with calibration, verification and refusal policy | Provenance expressions; claim-level verification; cost/latency budget per route |
| **SWE** | Python 3.12, FastAPI, Pydantic v2, Postgres 16/17 + pgvector (HNSW, halfvec, iterative scans), Redis, OpenTelemetry, k6, pytest/Hypothesis/Testcontainers, Docker, GitHub Actions, Next.js (TypeScript) | Rate limiter design, cache invalidation by corpus version, idempotent upserts |

## 3. What this project deliberately doesn't cover (and where it goes instead)

- **Streaming (Kafka/Kinesis):** Project 2. The ADR explains why daily batch is enough here.
- **LLM fine-tuning or self-hosted serving:** your existing vLLM/QLoRA project already covers it. Out of scope here on purpose.
- **Kubernetes:** not needed at this scale [Likely]. "One box + serverless batch" is the honest design for one user group. Your earlier RAG project has the K8s story.

## 4. Resume bullet templates (fill braces only from `eval/results/*.json`)

Put this project under **Projects** with a live link and a GitHub link. Use 3–4 bullets per resume variant. Never put a target here; only measured values.

**Common header:** *Verified Financial Research Platform: Python, Spark, Iceberg, AWS (EMR Serverless, S3, Glue, Lambda, CloudFront), Postgres/pgvector, FastAPI, Claude API, Terraform.*

**SDE variant**
- Built a lakehouse on S3/Iceberg that processes **{N}M** XBRL facts from **{K}** SEC filers (2009–2026) with Spark on EMR Serverless in **{T} min** for **${C}**; benchmarked against DuckDB and documented the crossover (**{X}×** cost at this scale).
- Designed a FastAPI query service with hashed API keys, Redis token-bucket rate limiting, a versioned answer cache (**{H}%** hit rate) and a daily LLM-spend circuit breaker; **p95 {L} ms** cached and **{R} req/s** on one 2-vCPU Graviton instance (k6).
- Shipped idempotent daily ingestion (EventBridge → Lambda → SQS with DLQ → Iceberg → Postgres) keeping new filings searchable within **{F} h** over **{D}** consecutive days; Terraform plus GitHub Actions (OIDC) deploys.

**AI-engineer variant**
- Built a verified financial QA system where an LLM fills a typed query plan compiled to parameterized SQL over XBRL facts; numeric accuracy **{A}%** on **{n}** auto-generated questions and **{B}%** on FinanceBench (**+{Δ}** pts over a naive-RAG baseline, 95% CI ±{ci}).
- Designed a claim-level verifier (provenance re-execution, quote checks, entailment) that cut unsupported claims from **{u0}%** to **{u1}%** while keeping false refusals at **{fr}%**; LLM judge calibrated against human labels (κ = **{k}**).
- Distilled an LLM query router into an ONNX classifier (macro-F1 **{f1}**, **{ms} ms** CPU) that handles **{p}%** of traffic without an LLM call at equal end-to-end accuracy, cutting cost per query from **${c0}** to **${c1}**.

**FDE variant**
- Deployed a research assistant over SEC filings to **{U}** finance-student testers; ran **{S}** observed sessions, logged **{Q}** questions, and turned the top-3 failure modes into regression tests and fixes (**+{Δ2}** pts on affected question types).
- Integrated four messy public data sources (XBRL bulk data, companyfacts API, 10-K/10-Q HTML, FinanceBench PDFs), handling restatements, 52/53-week fiscal years and unit scaling; every number in an answer links to its source filing.
- Wrote the onboarding guide, runbook and decision log (**{n_adr}** ADRs); kept the live demo at **${m}/month** with scale-to-zero batch and a spend cap.

## 5. Interview question bank (answer each without notes before you apply)

Skeletons point to where the answer lives. Don't memorize; make sure the evidence exists.

### Data and big data
1. *Why Iceberg and not plain Parquet?* → Atomic commits, schema evolution, hidden partitioning, time travel for reproducing an eval against a past corpus version, and MERGE for restatements. Point to ADR-003.
2. *Why Postgres for serving and not Athena?* → Athena is seconds and pay-per-scan; you need tens of ms. "Batch truth in the lake, low-latency copy in the serving store, rebuilt by a load job with an atomic swap."
3. *How do you handle restated numbers and amended filings?* → `facts_history` keeps every version. `facts` keeps the latest version **and** the originally reported one when they differ (`is_latest`/`is_original`). Answers default to as-originally-reported, which matches the cited filing, and mention the restated value when `is_restated` (ADR-017).
4. *How do you derive a fact's fiscal year?* → M1 §3.5: find the fiscal-year end the period falls in, using the FYE from the same submission and a ±7-day window for 52/53-week years. Add a per-company label offset learned from its 10-K submissions (companies name fiscal years differently). `qtrs` gives the period type. Q4 = FY − 9M, for flow concepts only.
5. *Why Spark here, honestly?* → The benchmark ADR, plus parsing and embedding parallelism.
6. *What's your partitioning strategy, and what happens with many small files from the daily load?* → Partition by fiscal_year (facts) and doc_source/fiscal_year (chunks). Daily appends create small files, so a weekly compaction runs (Athena `OPTIMIZE` or Spark `rewrite_data_files`), plus snapshot expiry.

### Retrieval and LLM
7. *Why hybrid retrieval? How do you fuse?* → Lexical catches exact terms (tickers, line items); dense catches paraphrase. Reciprocal Rank Fusion, k=60. Recall@k numbers by component.
8. *How did you choose chunk size?* → The embedder's 512-token limit; evidence-page recall@k at 300/400/500 tokens.
9. *How does the verifier decide a number matches?* → Re-execute the provenance expression on fact IDs; compare with rounding-aware tolerance derived from the precision of the displayed number; check entity/period/unit metadata against the plan; parse the number in the text and compare it with the claimed value.
10. *Your false-refusal rate, and the trade-off against unsupported claims?* → The threshold sweep curve from M6; why you picked the operating point.
11. *What dominates latency, and what did you do about it?* → Trace breakdown: composer LLM time first; caching, a smaller composer for numeric routes, parallel per-claim verification, and the cascade router.
12. *Why not paste the whole 10-K into a 1M-context model?* → Measured comparison: cost per question, latency, accuracy; numbers still need XBRL verification.
13. *How do you know your LLM judge is right?* → Hand labels, κ, disagreement analysis, a judge model different from the verifier.

### ML
14. *Where did the router's training data come from? What about leakage?* → Templates plus LLM paraphrases for training; hand-labeled real questions (FinanceBench, narrative set, user logs) for test; no paraphrase of a test question appears in training (dedup by embedding similarity).
15. *Why macro-F1? What's the confusion pattern?* → Class imbalance; hybrid vs narrative confusion; the cost of each error type end to end.
16. *How did you pick the cascade threshold?* → Sweep τ on a validation set; the curve of end-to-end accuracy vs % handled locally; choose the knee.
17. *Is the classifier calibrated?* → ECE before/after temperature scaling.
18. (S1) *How do you evaluate anomaly detection without labels?* → Synthetic error injection for recall, a manual audit of the top-k for precision, and a rules-only baseline for comparison.

### Systems and cloud
19. *Walk me through a request.* → CloudFront → EC2 (gunicorn/uvicorn) → auth → rate limit → cache → router → plan compiler / retrieval → composer → verifier → response; spans in Jaeger.
20. *How does your rate limiter work, and what are its failure modes?* → Redis Lua token bucket per key; atomicity; what happens if Redis is down (fail closed for anonymous traffic, fail open for internal).
21. *Cache invalidation?* → The key includes `corpus_version`; a new load bumps the version; TTL as a backstop.
22. *What breaks at 100× users?* → LLM rate limits and cost, then HNSW memory and Postgres CPU; mitigations in order.
23. *Why no NAT gateway, and how do Lambdas reach SEC?* → Lambdas outside the VPC; the EC2 box in a public subnet locked down with SSM and no SSH; about $32+/month saved [Likely].
24. *How do you deploy without long-lived AWS keys?* → GitHub OIDC → an IAM role scoped to ECR push and SSM send-command.
25. *Why EC2 and not Lambda + Aurora Serverless for the API?* → Cold starts (model weights, DB resume), the always-free limits, and cost; the ADR shows the numbers.

### FDE
26. *How would you onboard a customer who brings private documents?* → S2: tenant-scoped ingestion, a namespace filter in retrieval, per-tenant eval set, data deletion path.
27. *Tell me about a time user feedback changed your design.* → From M11: the specific failure type, what you changed, the before/after numbers.
28. *A finance user says an answer is wrong. What do you do in the first hour?* → Reproduce from the trace ID; check provenance; classify the failure (data, mapping, retrieval, generation, verification); add a regression test; communicate an ETA.
29. *What did you choose not to build, and why?* → Kafka, fine-tuning, K8s, full-corpus embedding: the ADRs.
30. *What's the riskiest assumption still in the system?* → Have an honest one ready, for example concept-mapping coverage for custom tags.
