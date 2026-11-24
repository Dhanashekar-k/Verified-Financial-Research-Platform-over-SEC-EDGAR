# edgar-verified-qa: instructions for Claude Code

## What this project is
A question-answering platform over SEC filings. Numbers come from XBRL facts (Iceberg lake → Postgres) through a **typed query plan compiled to parameterized SQL**. Text comes from hybrid retrieval over 10-K/10-Q chunks. A **verifier** checks every claim, and the system **refuses** when filings don't support an answer. It's a portfolio project. Interview-defensibility beats feature count.

Plans and contracts live in `docs/plan/`:
- `00_START_HERE.md`: priorities and calendar
- `03_architecture_and_stack.md`: architecture, stack, repo layout
- `05_data_contracts.md`: **every schema and JSON contract (the source of truth)**
- `M00_…` to `M12_…`, `S1_…`, `S2_…`: milestone runbooks (one per session)

## Hard rules (never break these)
1. **Never invent numbers.** Every metric in docs, the README or reports must come from `eval/results/*.json`, `load/results/*.json`, job-metrics JSON or a milestone report input. If it isn't measured, write `NOT MEASURED`.
2. **Never edit gold data:** `eval/sets/*.jsonl` (except `eval/sets/drafts/`), `eval/sets/CHECKSUMS`, `eval/judges/calibration.jsonl`, `eval/sets/sections_gold.jsonl`. A hook blocks Edit/Write; don't work around it with shell commands. `make check-gold` verifies checksums in CI.
3. **Don't implement Shekar-owned modules** (listed below). You may create an initial stub file once, write tests for them, review them and explain them. Propose changes as diffs in chat only.
4. **Tests never call real external services.** Only the LLM boundary (`app/llm`) and SEC HTTP (`pipelines/fetch/sec_client.py`) may be stubbed. Don't mock the unit under test. `EDGARQA_LLM_MODE=stub` is the default in tests; sockets to `api.anthropic.com` and `sec.gov` are blocked unless a test is marked `@pytest.mark.live`.
5. **The API key variable is `EDGARQA_ANTHROPIC_API_KEY`.** Never create or reference `ANTHROPIC_API_KEY`.
6. **The LLM never writes SQL on the main path and never does arithmetic.** Numbers come from the plan compiler with provenance expressions. Text-to-SQL exists only as the guarded fallback (`app/engine/sql_guard`), off by default.
7. **Contracts first:** if a schema or JSON shape must change, stop and ask. Then update `docs/plan/05_data_contracts.md`, then the Pydantic models, then the code, then add an ADR.
8. **Cloud safety:** never run `terraform apply/destroy`, mutating `aws` commands, `make emr-run`, `make deploy` or `make destroy*` unless Shekar explicitly asks in this session. `fmt`/`validate` are fine.
9. **SEC access:** always go through `sec_client` (User-Agent from `SEC_USER_AGENT`, ≤ 8 req/s, retries with backoff).

## Shekar-owned modules (create the stub once if missing; otherwise read/test/review only)
- `pipelines/common/fiscal_ref.py`
- `app/engine/plan/compiler.py`, `app/engine/plan/periods.py`, `app/engine/plan/metric_catalog.yaml`
- `app/engine/verify/numeric.py`, `app/engine/verify/quotes.py`, `app/engine/verify/policy.py`
- `app/engine/sql_guard/guard.py`, `app/engine/router/cascade.py`
- `ml/router/model_fns.py`, `eval/metrics.py`

`pipelines/spark/fiscal.py` and `pipelines/spark/versions.py` are **not** Shekar-owned: Shekar authorized Claude to implement them (2026-11-17, see ADR-021), written against `tests/integration/test_differential.py` with `fiscal_ref.py` as the oracle. `fiscal.py` runs the reference per company (`applyInPandas`), so its differential test checks the plumbing, not the rules. `fiscal_ref.py` stays hook-protected.

## Conventions
- Python 3.12 (check the EMR Python version for code under `pipelines/spark` and `pipelines/parsing`), managed with **uv**. Formatting and linting via **ruff**; **mypy --strict** on `app/engine`.
- Pydantic v2 models are the single source of contracts in code; no untyped dicts across module boundaries.
- Money and XBRL values use `Decimal`, never float. Values are stored in base units (USD, not millions).
- Spark: one `spark_session(env)` factory (`pipelines/common/spark_session.py`); explicit schemas; idempotent writes (`overwritePartitions` / `MERGE`); lowercase snake_case table and column names.
- Every pipeline step is idempotent and safe to rerun.
- Tests: `tests/unit`, `tests/integration` (Testcontainers), `tests/property` (Hypothesis). New behavior gets a test first when the runbook says so.
- Commits: small, one concern each, imperative subject ≤ 72 chars, body says why.
- Observability: every engine stage is an OpenTelemetry span with `model`, `input_tokens`, `output_tokens`, `cost_usd` where relevant.

## Do not add (without an ADR approved by Shekar)
Kafka/Kinesis, Kubernetes, LangChain/LlamaIndex, a second vector database, ORMs for the hot path, new AWS services not in `03_architecture_and_stack.md`, fine-tuning of LLMs, NAT gateways, ALBs.

## Commands
- `make up` / `make down`: local stack (Postgres+pgvector, Redis, Jaeger)
- `make test`, `make lint`, `make fmt`, `make check-gold`
- `make bench SYSTEM=engine SPLIT=dev MODE=replay`: eval harness (MODE: replay|live|batch)
- `make status`, `make cost-today`, `make llm-spend`: what's running and what it costs
- Milestone skills: `/milestone M5`, `/dod M5`, `/interview-me <path>`, `/adr "<title>"`, `/results-audit`

## How to work in this repo (execution mode — speed priority, 2026-11-24)
Deadline reality: a working 80% shipped soon beats a perfect 100% weeks late. Optimize for a finished, defensible end-to-end system, not polish on any one part.
- **Batch aggressively.** One prompt may carry many steps. Plan the whole batch once (plan mode for the batch, not per micro-step), take one approval, then execute to completion. Do not stop between reversible, local, $0 steps to ask.
- **Stop and ask ONLY at the gates** (already encoded as Hard rules / "Explain before consequential actions", still binding): spending money (starting the box, any paid `live`/`batch` LLM run), writing or promoting gold (Hard rule 2, hook-gated), mutating cloud state or deploying (Hard rule 8), changing a contract/schema (Hard rule 7), and anything irreversible or hard to undo. State the expected cost first at a money gate.
- **Never trade these for speed:** Hard rule 1 (every number measured, never invented) and gold integrity (Hard rule 2). They are the project's credibility; batching does not relax them.
- **Don't gold-plate.** When a part is good enough to be interview-defensible, stop and move on; log deferred polish as an ADR or devlog TODO rather than chasing it. (Revenue resolver: correct to defer, costly to keep polishing.)
- **Finish the runbook's intent**, not the narrowest edit. On ambiguity below a gate, take the most reasonable reading, state the assumption, and continue; only gate-level ambiguities are worth a stop.
- When reporting results, quote the file and key each number came from.
- Use the `reviewer` subagent on staged diffs before commits, and `test-writer` for test-first work.

## Explain before consequential actions
Before any command that mutates cloud state (AWS, `terraform apply`, Glue/EMR runs), deploys, spends money, deletes or overwrites data, or pushes to git: first write ONE short plain-English paragraph (what it does, what it changes, the risk), then run it. Do not explain read-only commands (ls, grep, cat, Athena SELECTs).

## Current status (2026-11-21)
- **Milestones:** M0, M1, M2 complete. M2 parse success 88.5% strict / 99.06% lenient, closed on the DoD "or documented" branch (residual in ADR-028/031); section accuracy 49/50 = 98% (n=50, in-sample). Next: M3 (embeddings + serving store).
- **Cloud:** AWS Free plan, account 019593222039, us-east-2, in an Org whose SCP denies EMR Serverless, so ALL Spark runs on AWS Glue 5.0, never EMR. M3's distributed embedding is therefore Glue or local fastembed.
- **Auth:** `aws login --profile edgarqa` (session expires ~hourly). Terraform/boto3 need exported creds + `botocore[crt]`; us-east-1 only for Budgets/Cost Explorer APIs.
- **Conventions:** smoke namespaces (`lake.*_smoke`) for first cloud runs; Glue jobs `max_retries 0`; commit AND push each step; decisions go to `docs/decisions.md` ADRs; gold eval stays parser-independent and is never edited to raise a score after seeing it.
- **Gotchas:** zsh does not word-split `$A` (use a shell function); merge deletes use an anti-join / `NOT EXISTS`, never `NOT IN` (O(n²) on the full table; it timed out the M2 re-parse).
- **Open loose ends (Shekar):** ADR Consequences review (026-031), 5-in-browser fragment check, `corpus_smoke` DB + orphaned `warehouse/` prefixes, Aurora `database-1`, dollar cost NOT MEASURED.

## Decision log + end-of-session commit
- Keep a running dev log in `docs/devlog.md`. At the END of each working session append a dated entry: what was done, the key decisions and why, what's next. A few bullet lines, the narrative that complements the ADRs, not a transcript.
- Weighty or architectural decisions still get a full ADR in `docs/decisions.md` (context + consequences); the devlog holds the lighter day-to-day calls and progress.
- At the end of a session confirm nothing is left uncommitted, then commit AND push, so the day's decisions and changes land on GitHub.
