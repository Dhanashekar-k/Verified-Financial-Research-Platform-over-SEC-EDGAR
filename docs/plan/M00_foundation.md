# M0: Foundation and accounts

**Time:** about 6 h · **Roles:** SDE (CI, IaC), all (the base for everything) · **Depends on:** Day 0 in `00_START_HERE.md`

## 1. Goal

A repo where every later step is one command, an AWS account that can't surprise you, and Claude Code configured so it can't burn credits or edit what you own.

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| `manual/repo_starter/`, `manual/*.md` | Git repo `edgar-verified-qa` with `CLAUDE.md`, `.claude/`, `docs/plan/`, `docs/decisions.md` (ADR-001…004 seeded) |
| AWS account (Day 0 decision) | Budgets ($20/$50/$80), Cost Anomaly Detection, admin user with MFA, AWS CLI profile `edgarqa` |
| Claude Console key | `.env` with `EDGARQA_ANTHROPIC_API_KEY`, `SEC_USER_AGENT`; Console spend limit set |
| — | `make up` (Postgres+pgvector, Redis, Jaeger), `make test`, `make lint`, `make cost-today`, `make status` |
| — | Terraform skeleton: S3 backend (with `use_lockfile`), lake bucket, Glue databases `raw` and `curated`, budgets module, GitHub OIDC role |
| — | GitHub Actions: lint + unit tests on every push |

## 3. Steps

1. **Create the repo.**
   ```bash
   mkdir edgar-verified-qa && cd edgar-verified-qa && git init
   cp -R "<path>/project 1. financial report/manual/repo_starter/." .
   mv dot-claude .claude              # shipped under a neutral name; Claude Code needs .claude/
   mv Makefile.starter Makefile
   mkdir -p docs/plan && cp "<path>/project 1. financial report/manual/"0*.md docs/plan/
   cp "<path>/project 1. financial report/manual/milestones/"*.md docs/plan/
   chmod +x .claude/hooks/*.sh
   cp .env.example .env    # then fill in values; .env is gitignored
   ```
2. **Python environment (uv).** Python 3.12; dependency groups `core`, `spark`, `api`, `ml`, `dev`. Pin PySpark 3.5.x and Java 17 (`brew install openjdk@17`, set `JAVA_HOME`).
3. **docker-compose.yml** services: `postgres` (`pgvector/pgvector:pg17`, port 5432, volume), `redis` (`redis:7`, 6379), `jaeger` (all-in-one, UI on 16686, OTLP on 4317/4318). An `airflow` profile comes later (M10). **No MinIO**: local Iceberg uses a file-based catalog under `./warehouse` (ADR-016).
4. **Makefile targets** (the skeleton in `repo_starter/Makefile`): `up, down, test, lint, fmt, bench, cost-today, status, tf-init, tf-plan, tf-apply` (the last asks for confirmation).
5. **AWS CLI profile.** `aws configure sso` (IAM Identity Center) or access keys for an IAM user with MFA. Region `us-east-1`. Verify with `aws sts get-caller-identity --profile edgarqa`.
6. **Terraform skeleton** (`infra/terraform/envs/dev`):
   - `backend.tf`: S3 backend in a bucket you create once by hand (`edgarqa-tfstate-<account-id>`), with `use_lockfile = true` [Likely: S3-native locking is in Terraform ≥ 1.10].
   - `providers.tf`: `default_tags { project = "edgarqa", env = "dev" }`.
   - Modules now: `lake` (S3 lake bucket with versioning off, public access block, lifecycle rule to abort incomplete multipart uploads after 7 days; Glue databases `raw` and `curated`), `budgets` (3 thresholds + email), `github_oidc` (OIDC provider + a role your repo can assume; permissions added in M9).
7. **GitHub Actions** `ci.yml`: on push run `uv sync`, `ruff check`, `ruff format --check`, `mypy app/engine` (allow-fail until M5), and `pytest tests/unit -m "not live"`.
8. **pytest safety fixture** (`tests/conftest.py`): block outbound sockets to `api.anthropic.com` and `sec.gov` unless a test is marked `live`; set `EDGARQA_LLM_MODE=stub` by default.
9. **Check Claude Code:** `claude` → `/status` (subscription) → `/memory` (CLAUDE.md loaded) → ask it to edit `eval/sets/x.jsonl` and confirm the hook blocks it.
10. **Seed ADRs** 001–004 in `docs/decisions.md` (already drafted in the starter; rewrite them in your words).

## 4. Claude Code session

```
/milestone M0
Then: Generate pyproject.toml (uv, Python 3.12, groups core/spark/api/ml/dev with the libraries in
docs/plan/03_architecture_and_stack.md §2), docker-compose.yml (postgres pgvector pg17, redis 7,
jaeger all-in-one; healthchecks; named volumes), and fill in the Makefile targets listed in
docs/plan/M00_foundation.md §3.4. Do not add MinIO or Airflow yet. Then write tests/conftest.py
with the socket-blocking fixture and the stub-LLM default. Show me the plan first.
```
```
Write the Terraform for infra/terraform/envs/dev and modules lake, budgets, github_oidc as in
M00 §3.6. Variables: account_id, region, alert_email, github_repo. Run terraform fmt and
validate only.
```
You then run `make tf-init && make tf-plan`, read the plan line by line, and run `make tf-apply` yourself.

## 5. You write

- Rewrite ADR-001…004 in your own words.
- Test the budget alert: temporarily set a $0.01 budget threshold, wait for the email, then restore it.

## 6. Definition of done

- [ ] `make up` → all three containers healthy (`docker compose ps`).
- [ ] `make test` and `make lint` pass locally and in GitHub Actions.
- [ ] `aws sts get-caller-identity --profile edgarqa` works; Terraform applied (lake bucket, Glue DBs, budgets, OIDC role).
- [ ] Budget alert email received in a test.
- [ ] `/status` in Claude Code shows the subscription; the hook blocks gold-file edits.
- [ ] `docs/milestones/M00-report.md` written by `/dod M0`.

## 7. Pitfalls

- An `ANTHROPIC_API_KEY` anywhere → Claude Code bills credits (see `06_claude_code_playbook.md` §1).
- Docker Desktop memory: give it at least 6 GB (Postgres + HNSW builds later).
- Don't commit `.env`, `*.tfstate` or `warehouse/` (all in `.gitignore`).

## 8. Interview prep

- "How do you keep secrets out of your repo and CI?" → `.env` gitignored locally, SSM Parameter Store in AWS, GitHub OIDC (no static keys).
- "How do you prevent surprise cloud bills?" → Budgets, anomaly detection, tags, capacity caps, no NAT, teardown targets.
