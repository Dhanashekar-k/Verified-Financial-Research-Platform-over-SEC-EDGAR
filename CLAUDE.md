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
- `pipelines/spark/fiscal.py`, `pipelines/spark/versions.py`, `pipelines/common/fiscal_ref.py`
- `app/engine/plan/compiler.py`, `app/engine/plan/periods.py`, `app/engine/plan/metric_catalog.yaml`
- `app/engine/verify/numeric.py`, `app/engine/verify/quotes.py`, `app/engine/verify/policy.py`
- `app/engine/sql_guard/guard.py`, `app/engine/router/cascade.py`
- `ml/router/model_fns.py`, `eval/metrics.py`

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

## How to work in this repo
- Start every milestone in plan mode with `/milestone <id>`. Wait for approval before editing.
- Prefer the smallest change that satisfies the runbook. Ask when the runbook is ambiguous rather than guessing.
- When reporting results, quote the file and key you took each number from.
- Use the `reviewer` subagent on staged diffs before commits, and the `test-writer` subagent for test-first work.
