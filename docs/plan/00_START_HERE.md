# Project 1 Manual: Verified Financial Research Platform over SEC EDGAR

**Start here.** This folder expands `Project1_Verified_Financial_Research_Platform.md` (your original plan, left unchanged) into a step-by-step build manual. It is written to be used with Claude Code: one milestone per session, with ready-made prompts, a `CLAUDE.md`, and `.claude/` config you copy into the repo.

Written 2026-11-16. Prices and AWS rules were checked that day. Re-check anything marked [Unclear] before you rely on it.

**Labels used throughout**
- [Certain]: directly supported by a source or by logic.
- [Likely]: strong inference.
- [Unclear]: not enough information; verify before relying on it.
- [Guessing]: an estimate to replace with a measurement.

---

## 1. The five things that matter most (read these even if you skip the rest)

1. **Your AWS "free tier" assumption is partly wrong.** [Certain] Accounts opened since July 15, 2025 get $100 in credits, plus up to $100 more for completing activities, on either a Free plan or a Paid plan. **You are not eligible if you have, or ever had, an AWS account.** Amazon EMR is listed only under the Paid plan in AWS's new sign-up experience. Classic MWAA (managed Airflow) isn't supported there at all. Day 0 (section 3) has the decision tree. Expect roughly **$45–80 of real AWS spend** over the project [Guessing]. Credits cover it if you're eligible; otherwise you pay it yourself.
2. **Your plan has no trained ML model, so "machine learning" on the resume would be hollow.** [Certain] I added **M7, a learned query router** (a classifier distilled from LLM labels, served with ONNX, with a cascade fallback to the LLM). There's also an optional stretch, **S1, Spark MLlib anomaly detection on XBRL facts**. Both are measurable and both feed the product.
3. **"Big data" will be challenged.** [Likely] The XBRL facts are on the order of 10^8 rows, which DuckDB on a laptop can handle. Don't oversell it. Benchmark DuckDB against Spark, report both, and put Spark where it earns its place: full-history normalization with window functions, HTML parsing, and distributed embedding of roughly a million chunks. "I measured where the crossover is" is a stronger interview answer than "I used Spark."
4. **Replace free-form text-to-SQL with a typed query plan.** [Likely] The LLM fills a JSON plan (company, period, metric, operation) and your compiler turns it into parameterized SQL. The LLM never writes SQL. That makes it safer, more accurate and easier to verify, and it's a clear thing to own in an interview. Guarded text-to-SQL stays as a measured fallback behind a flag.
5. **"Finish soon" realistically means 5–6 weeks at ~45 h/week for the full version, or about 4 weeks on the fast track** (section 5). The bottleneck isn't coding speed. It's the real-user phase (M11), which needs calendar time, and December is a bad month to recruit testers. **Send tester invitations in week 2,** before the product exists.

---

## 2. Files in this pack

| File | What it's for | When to read |
|---|---|---|
| `00_START_HERE.md` | This file: priorities, Day 0, calendar, how to run sessions | Now |
| `01_review_and_changes.md` | Rigorous review of the original plan: what's defensible, what a skeptic attacks, every change I made and why | Now (30 min) |
| `02_role_coverage.md` | FDE / SDE / AI-engineer coverage matrix, big data / cloud / ML / LLM keyword map, resume-bullet templates, interview question bank | Before M0, again before applying |
| `03_architecture_and_stack.md` | Final architecture, every technology with version and reason, data flow | Before M0 |
| `04_aws_and_cost_plan.md` | Account decision tree, service-by-service AWS cost, Claude API budget, guardrails, teardown | Day 0 |
| `05_data_contracts.md` | Every input with sample rows, every table schema (Iceberg + Postgres DDL), API JSON, eval-set formats | Keep open while building |
| `06_claude_code_playbook.md` | Claude Code setup, the billing trap, session workflow, skills/agents/hooks, what you write yourself | Before M0 |
| `milestones/M00…M12, S1, S2` | One runbook per milestone: inputs → outputs, steps, commands, Claude Code prompts, tests, DoD, pitfalls | One per session |
| `repo_starter/` | `CLAUDE.md`, `dot-claude/` (rename to `.claude/`: settings, hooks, rules, skills, agents), `docs/decisions.md`, `.env.example`, `.gitignore`, `Makefile.starter` (rename to `Makefile`): copy into the repo in M0 | M0 |

### Milestone numbering (new → original)

| New | Name | Original | Change |
|---|---|---|---|
| M0 | Foundation and accounts | M0 | MinIO dropped; account decision added |
| M1 | XBRL lakehouse | M1 | Fiscal-period rules, dedup and DuckDB benchmark added |
| M2 | Filing text corpus | M2 | Chunk size fixed to fit the embedder; FinanceBench PDFs indexed whole |
| M3 | Embeddings and serving store | M3 | Distributed embedding on Spark; halfvec; atomic corpus swap |
| M4 | Eval sets and baseline | M4 | CIs, judge calibration, de-circularized numeric gold |
| M5 | Query engine | M5 | Typed plan compiler replaces free-form text-to-SQL |
| M6 | Verifier | M6 | Provenance expressions, quote-substring check, deterministic checks before the LLM |
| **M7** | **Learned query router (ML)** | new | Distilled classifier, cascade, MLflow, ONNX |
| M8 | API and engineering quality | M7 | Spend guard; T8 redefined |
| M9 | UI and cloud deployment | M8 | Concrete cheap topology (CloudFront + one Graviton box) |
| M10 | Incremental ingestion | M9 | Airflow semantics → MWAA Serverless, with a Step Functions fallback |
| M11 | Real users (FDE) | M10 | Feedback-to-regression-test flywheel |
| M12 | Scale run and packaging | M11 | Unchanged in spirit |
| S1 | Anomaly detection on facts (Spark MLlib) | new, stretch | Big-data ML |
| S2 | Bring-your-own-documents tenant | new, stretch | FDE onboarding story |

---

## 3. Day 0 (today/tomorrow, about 2 hours). Do these before writing code.

1. **Decide the AWS account path** (details in `04_aws_and_cost_plan.md` §1):
   - Ever had an AWS account (including for your SA certification)? → Use it on pay-as-you-go. No new-customer credits. Budget about $50–80 of cash.
   - Never had one? → Create a new account and choose the **Paid plan**. It still gets the $100 + up to $100 credits [Certain], and the Free plan blocks EMR [Certain] and caps Aurora at 1 GiB of storage [Certain]. A Free-plan account also closes after 6 months or when credits run out [Certain], which kills a demo link you want on your resume for months.
2. **Set guardrails before you create anything:** AWS Budgets at $20 / $50 / $80 with email alerts; Cost Anomaly Detection on; an MFA-protected root account; an admin IAM Identity Center user or IAM user for daily work; and `us-east-1` as your default region.
3. **Claude API:** create a Console workspace for this project, set a monthly spend limit in the Console, and create a key. **Name the variable `EDGARQA_ANTHROPIC_API_KEY`, never `ANTHROPIC_API_KEY`.** If `ANTHROPIC_API_KEY` is in your shell or `.env`, Claude Code bills your API credits instead of your Max subscription [Certain] (see `06_claude_code_playbook.md` §1).
4. **Draft the tester invitation now and send it by Mon Nov 23** (template in `milestones/M11_real_users.md` §3): OSU finance or investment clubs, Fisher College of Business master’s/MBA students, friends in finance. Ask for one 30-minute slot in the week of Dec 7 or Dec 14.
5. **Read** `01_review_and_changes.md` and decide whether you accept each change. Where you disagree, write an entry in `docs/decisions.md`. Interviewers value a recorded disagreement more than a silent default.

---

## 4. Full-track calendar (about 170 h core + stretch, ending around Dec 27)

Assumes ~45 h/week starting Tue Nov 17. Thanksgiving is Thu Nov 26.

| Week | Dates | Milestones | Hours | Calendar-sensitive items |
|---|---|---|---|---|
| 1 | Nov 17–22 | M0 (6), M1 (16), M2 start: fetcher + first downloads (10) | 32 + slack | Full-history EMR run once local runs pass |
| 2 | Nov 23–29 | M2 finish (8), M3 (12), M4 start (8) | 28 (holiday) | **Tester invites go out Mon Nov 23** |
| 3 | Nov 30–Dec 6 | M4 finish (6), M5 (22), M6 start (10) | 38 | Baseline numbers frozen |
| 4 | Dec 7–13 | M6 finish (6), M8-security subset (4), M9 alpha deploy (12), M7 router (10) | 32 | **Alpha link live by Thu Dec 10**; first 2–3 user sessions |
| 5 | Dec 14–20 | M10 incremental (8, five-day clock starts), M11 sessions + fixes (10), M8 rest (8) | 26 + sessions | Students leave around Dec 18–19 |
| 6 | Dec 21–27 | M11 re-bench (4), M12 scale run + README + video (10), S1 if ahead (14) | 14–28 | Final numbers come only from `eval/results/*.json` |

Total ≈ 170 h core (M0 6, M1 16, M2 18, M3 12, M4 14, M5 22, M6 16, M7 10, M8 12, M9 12, M10 8, M11 14, M12 10), plus 14 h for S1 and 8 h for S2 [Guessing]. That's your original estimate plus M7 and the extra rigor, minus MinIO and some boilerplate. Add 15–20% for things going wrong. They will.

## 5. Fast-track calendar (about 145 h, ending around Dec 13)

Use this if you need a defensible project on your resume sooner. Cuts, in priority order:

1. Corpus: **10-K only** for the S&P 500, FY2020–2025, plus all FinanceBench documents. No 10-Qs.
2. Embeddings: run fastembed on your Mac and skip the distributed-embedding Spark stage (M3 option B).
3. M7 router: logistic regression on frozen embeddings only, with no fine-tuned encoder (6 h instead of 10).
4. M10: one EventBridge-scheduled Lambda (no Airflow). Run the five-day freshness clock in the background during M11.
5. M9 UI: a single page (question box, answer, citation drawer, feedback). No auth UI; invite codes only.
6. Skip S1 and S2.

**Never cut:** M4 (eval before engine), M6 (verifier), M11 (real users), the spend guard in M8, and the decisions log. These carry the FDE and AI-engineer story.

---

## 6. How each working session runs (the loop)

1. `cd edgar-verified-qa && claude`, then run `/status` to confirm it says **subscription**, not API key.
2. `/milestone M5`: the skill reads `docs/plan/M05_query_engine.md`, summarizes the inputs, outputs and DoD, and proposes a plan in plan mode. You approve or edit.
3. Claude Code writes boilerplate and tests. **You write the core modules** named in each milestone; a hook blocks Claude from editing them (see `06_claude_code_playbook.md` §4).
4. Commit after every green test run. Use small commits with clear messages.
5. `/dod M5` runs the DoD commands and writes `docs/milestones/M05-report.md`, with numbers copied only from result files.
6. `/interview-me app/engine/plan`: Claude questions you like a skeptical interviewer. Anything you can't explain gets fixed or rewritten before you move on.
7. `/clear` before the next milestone.

Copy `manual/0*.md` and `manual/milestones/*.md` into the repo as `docs/plan/` so the skills can find them.

---

## 7. What "done" looks like (the evidence pack)

- A live demo URL (CloudFront) that works on a phone, with invite-code access and a spend cap.
- `make bench` reproduces every number in `docs/results.md` from `eval/results/*.json`.
- A results table: baseline vs engine vs engine+verifier vs engine+verifier+ML router, with 95% bootstrap CIs, cost per query and p50/p95 latency.
- `docs/decisions.md` with 15+ ADRs, including the ones where you disagreed with this manual.
- A 3-minute video: verified numeric answer → click citation → narrative answer → refusal → cost/latency trace.
- Resume bullets filled in **only** from measured numbers (templates in `02_role_coverage.md` §4).
- The ability to answer every question in `02_role_coverage.md` §5 without notes.
