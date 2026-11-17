# 01: Review of the original plan and the changes I made

This follows your review format: what's defensible, what's weak, what a skeptic would attack, what to change, and the stronger version. Every change shows up in the milestone runbooks. If you reject one, record why in `docs/decisions.md`.

---

## 1. What's defensible (keep it)

- **Measurable targets table with "only measured numbers go on the resume."** [Certain] This is the single best thing in the plan. It protects you in interviews.
- **Eval set before the engine (M4 before M5).** [Certain] It's the right order, and most portfolio RAG projects get it backwards.
- **Numbers come from structured XBRL, not from text.** [Likely] It's the strongest technical idea in the project, and it's what makes "verified" more than a marketing word.
- **The verifier is separate from the generator, with an ablation.** [Certain] Ablations are what separate an engineering claim from a demo.
- **An explicit out-of-scope list and the reasoning for not using Kafka.** [Likely] "I chose not to use X because…" answers score well.
- **Cost guardrails and `make destroy`.** [Certain] Necessary.
- **"Write the verifier, router and SQL guardrails yourself."** [Certain] Correct. I extended it with a hook that enforces it.

## 2. What's weak, unclear or overclaimed

### 2.1 No machine learning model is trained anywhere

[Certain] The plan uses pretrained embeddings, a pretrained reranker and API LLMs. That's ML usage, not ML engineering. A recruiter who reads "machine learning" and asks "what did you train, on what data, how did you evaluate it, and how is it served?" gets no answer.

**I disagree with leaving it out because** you explicitly want ML on the resume, and there's a natural place for it that improves the product. **Here's what I would do instead:** add **M7, a learned query router**: a small classifier distilled from LLM-labeled and template-labeled questions, evaluated on a hand-labeled test set of real questions, exported to ONNX, served in-process, and placed in a cascade (low-confidence questions fall back to the LLM router). Measure macro-F1, calibration, latency, cost saved and end-to-end accuracy change. The stretch **S1** adds Spark MLlib anomaly detection over all XBRL facts, which is big-data ML that feeds the verifier. **The risk in your approach** is that the "ML" keyword invites exactly the questions you can't answer.

### 2.2 "Big data" is a stretch at this size

[Likely] The Financial Statement Data Sets cover XBRL filings since April 2009 [Certain] and, after the SEC's December 2024 reprocessing, contain only numeric data from the primary financial statements [Certain]. My estimate is tens of GB uncompressed and about 10^8 `num` rows [Guessing]; measure it in M1. That fits in DuckDB on a 16 GB laptop.

**I disagree with presenting Spark/EMR as necessary because** an interviewer can disprove it in one question. **Here's what I would do instead:**
- Run the same `normalize_facts` logic in DuckDB and in Spark (local and EMR Serverless). Report wall time and cost for both. Write an ADR about where the crossover is.
- Put Spark where parallelism genuinely pays off: HTML parsing of about 12k filings (CPU-bound and embarrassingly parallel), and distributed embedding of about 0.5–1M chunks with pandas UDFs (M3 option A).
- Use exact numbers on the resume ("N M XBRL facts, X GB, Y min on EMR Serverless for $Z"), not "big data."

**The risk in your approach** is that an overclaim discovered in an interview discounts every other claim you make.

### 2.3 Free-form text-to-SQL is the wrong primary path

**I disagree with LLM-written SQL as the main numeric path because** it's the least verifiable part of the system and most of the risk sits there: wrong tags, wrong periods, unit mix-ups and injection-shaped failures. **Here's what I would do instead:** the LLM produces a **typed query plan** (JSON with an enum-constrained `metric` from your metric catalog, resolved companies, fiscal periods, and an `operation` of value/growth/ratio/compare/rank). Your **plan compiler** turns it into parameterized SQL and returns values together with a **provenance expression** (for example `f123 / f456`). Guarded text-to-SQL (sqlglot AST allow-list, read-only role, row limit, statement timeout) stays as a flagged fallback, and you report its share of traffic and accuracy separately. **The risk in your approach** is accuracy that's hard to debug and a verifier that has to reverse-engineer what the SQL meant.

### 2.4 The chunk size conflicts with the embedding model

[Certain] The plan uses 500–800-token chunks. Common small open embedders, such as the BGE-small/base family, have a 512-token limit, and anything longer is truncated silently [Likely for the specific model; check the model card]. **Fix:** target about 400 tokens *as measured by the embedder's own tokenizer*, about 15% overlap, never cross section boundaries, and serialize tables separately. Alternatively, choose a long-context embedder and pay for it in memory.

### 2.5 The evaluation has circularity you haven't addressed

- **Auto-generated numeric gold comes from the same `curated.facts` table the system queries.** [Certain] A score here measures routing, entity resolution, concept mapping and period logic, not ground truth. That's fine, but say so. Cross-check a random 30 gold answers against the companyfacts API (a second source) and 10 against the actual 10-K text. Report agreement.
- **An LLM verifier evaluated by an LLM judge** can share blind spots. [Likely] Use a different model and prompt for the judge than for the verifier, hand-audit 50 claims, and report judge-vs-human agreement (Cohen's κ).
- **FinanceBench has 150 open questions** [Certain]. Filtering to "questions whose documents you indexed" invites a cherry-picking critique, and at n≈150 one question is about 0.7 points. **Fix:** index all FinanceBench source documents (the PDFs ship with the dataset [Certain]), evaluate on all 150, and report a 95% bootstrap confidence interval.

### 2.6 Some targets are mis-specified

- **T2 (< 2 h, < $10):** [Likely] far too loose. At $0.052624/vCPU-hour and $0.0057785/GB-hour on EMR Serverless (us-east-1 list price [Likely]), the facts rebuild probably costs a few dollars and takes minutes. Report cost per million facts and time instead.
- **T3 (≥ 90% within 1%):** because gold comes from the same table, anything below about 98% is a bug in entity, period or concept resolution, not a model limitation [Likely]. Keep the metric and change how you interpret it.
- **T8 (≥ 200 req/s, p99 ≤ 150 ms on one small instance, LLM stubbed):** [Likely] not achievable for the *retrieval* path if a cross-encoder reranker runs on 2 CPU cores, since reranking 30–50 pairs alone takes hundreds of ms [Guessing]. **Fix:** define T8 separately for (a) the numeric path (plan → SQL), (b) the cached path, and (c) the retrieval path with the reranker stubbed, and report the reranker cost on its own.
- **T9 (≤ $0.02/query):** depends on model choice. With current prices a Sonnet 5.5 composer comes to about $0.02–0.03/query and a Haiku 5.5 composer about $0.002–0.003 [Guessing on token counts; prices Certain]. Report a **cost–accuracy Pareto curve** across model tiers instead of one number. It's more informative and a better AI-engineer artifact.
- **"BM25 + dense":** [Certain] Postgres full-text ranking (`ts_rank_cd`) is not BM25. Call it "lexical (Postgres FTS)," or use a real BM25 extension such as ParadeDB `pg_search` and say so.

### 2.7 AWS assumptions are out of date

[Certain] The 12-month free tier ended for accounts opened on or after July 15, 2025 and was replaced by credits. New-customer credits don't apply if you've had an account before. EMR is Paid-plan-only in the new sign-up experience. Classic MWAA isn't supported there, and it bills hourly for an always-on environment. NAT gateways and public IPv4 addresses ($0.005/hour each) are the classic surprise charges. Everything is in `04_aws_and_cost_plan.md`.

### 2.8 The FDE story starts too late and has no onboarding artifact

[Likely] Real users only at M10, in December, with exams and holidays, is a schedule risk. **Fixes:** invite testers in week 2; get an alpha deploy live by about Dec 10; add a feedback → triage → regression-test flywheel; and offer **S2 (bring-your-own-documents tenant)** as the concrete answer to your own interview question, "How would you onboard a customer with private documents?"

### 2.9 A public demo with LLM calls and no spend guard

[Likely] A public URL that calls a paid model gets scraped or abused within days. **Fix (in M8 and M9):** invite codes mapped to hashed API keys, a per-key token bucket, a **global daily LLM-spend circuit breaker** in Redis, a Console spend limit, and short answer-cache TTLs. This is also good SDE interview material.

### 2.10 XBRL semantics traps the plan doesn't mention

[Certain] These cost days if you find them late:
- `2009q1.zip` contains headers only; the data starts with filings from 2009-04-15.
- A 10-K for FY2023 also reports FY2022 and FY2021 comparatives. The submission's `fy`/`fp` describe the *filing*, not each fact. Derive each fact's fiscal period from `ddate` and `qtrs` (rules in M1).
- 52/53-week fiscal years (Apple, for example) mean period-end dates drift by days, so match fiscal-year ends with a tolerance.
- `qtrs` is 0 for instants, 1 for quarters, 2–3 for year-to-date, and 4 for annual. Q4 values are often never reported directly; they're FY minus 9-month YTD.
- `coreg` (co-registrant) and the new `segments` column (dimensional breakdowns, added Dec 2024 [Certain]) must be null for consolidated company-level facts.
- Revenue alone has several common tags across eras (for example `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet`), so the metric catalog needs ordered fallbacks.

## 3. What a skeptical reviewer would object to (and your answer after the changes)

| Objection | Who asks | Your answer after the changes |
|---|---|---|
| "This isn't big data." | Data/SWE interviewer | "Right, the facts table is N rows. I benchmarked DuckDB against Spark (ADR-007). Spark pays off for parsing and embedding about a million chunks; here's the cost per million." |
| "What did you actually train?" | ML interviewer | M7 router: data provenance, held-out real questions, macro-F1, calibration, cascade economics. |
| "Isn't your eval circular?" | AI interviewer | Second-source cross-check, judge ≠ verifier, κ against human labels, bootstrap CIs. |
| "How do you know the verifier works?" | AI interviewer | Ablation (unsupported-claim rate before/after) plus a hand audit of 50 claims plus adversarial cases (wrong year, wrong unit, scale). |
| "Why not just use a long-context model and paste the 10-K?" | Anyone | A measured comparison on 30 questions (cost, latency, accuracy): "long-context baseline" is an M4 extra. |
| "What happens at 100× users?" | SDE interviewer | Bottleneck analysis from traces: LLM latency dominates, so caching and the cascade router help; Postgres read replica; HNSW memory; move the reranker to a GPU or drop it. |
| "Did anyone use it?" | FDE interviewer | N testers, M logged questions, the top 3 failure types, fixes shipped, and before/after numbers. |

## 4. Summary of changes

| # | Change | Where | Hours (±) |
|---|---|---|---|
| C1 | Add M7 learned router (ML) | `milestones/M07_ml_router.md` | +10 |
| C2 | Add S1 Spark MLlib anomaly detection (stretch) | `milestones/S1_ml_anomaly_detection.md` | +14 (optional) |
| C3 | Add S2 bring-your-own-documents tenant (stretch) | `milestones/S2_byod_tenant.md` | +8 (optional) |
| C4 | Typed query plan + compiler; text-to-SQL becomes a fallback | M5 | ±0 |
| C5 | Provenance expressions + quote-substring check in the verifier | M5, M6 | +2 |
| C6 | Chunking fitted to the embedder; tables serialized separately | M2 | ±0 |
| C7 | DuckDB vs Spark benchmark ADR | M1 | +2 |
| C8 | Distributed embedding on Spark (option A) | M3 | +3 |
| C9 | Index all FinanceBench docs; bootstrap CIs; judge calibration; second-source gold check | M4 | +3 |
| C10 | Redefine T2, T3, T8, T9; rename "BM25" | §5 below, M8 | 0 |
| C11 | Drop MinIO; local Iceberg uses a file-based catalog | M0 | −3 |
| C12 | Paid-plan or legacy-account decision; concrete cheap topology; spend guard | `04_aws_and_cost_plan.md`, M8, M9 | +2 |
| C13 | Early tester recruiting, alpha deploy, feedback flywheel | M9, M11 | +2 |
| C14 | Daily DAG on MWAA Serverless, with a Step Functions fallback | M10 | ±0 |
| C15 | Claude Code enforcement: protected core paths, protected gold sets, billing trap | `06_claude_code_playbook.md`, `repo_starter/` | +1 |

## 5. Revised targets

| # | Metric | Target | Notes |
|---|---|---|---|
| T1 | XBRL facts processed | All FSDS quarters since 2009Q2; record exact rows and GB | Plus the DuckDB-vs-Spark ADR |
| T2 | Full facts rebuild on EMR Serverless | Record minutes and $; report $/million facts | Expect well under $10 [Likely] |
| T2b | Distributed embedding job | Record chunks/min and $/100k chunks | New |
| T3 | Numeric auto set (300) | ≥ 98% within 1% (failures are bugs) | Second-source agreement reported |
| T4 | FinanceBench (all 150) | ≥ naive baseline + 15 pts, with 95% CI | Judge calibrated, κ reported |
| T5 | Unsupported-claim rate after verifier | ≤ 2%, with the pre-verifier rate | Hand audit of 50 claims |
| T6 | Unanswerable refusals / false refusals | ≥ 80% / ≤ 10% | |
| T7 | Latency | Uncached p95 measured per route; cached p95 ≤ 100 ms | Report the latency breakdown from traces |
| T8a/b/c | Throughput (numeric path / cached / retrieval with reranker stubbed) | Set after the first k6 run, then improve | One t4g.medium |
| T9 | Cost per query | Pareto curve across Haiku/Sonnet tiers | Pick the operating point and justify it |
| T10 | Freshness | ≤ 24 h, measured over 5+ consecutive days | |
| T11 | Real users | ≥ 5 testers, ≥ 100 questions, ≥ 3 fixes with before/after | |
| T12 | ML router | Macro-F1 on hand-labeled real questions; % of traffic handled without the LLM at equal end-to-end accuracy | New |
