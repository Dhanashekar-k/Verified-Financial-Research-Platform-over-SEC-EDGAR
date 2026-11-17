# edgar-verified-qa Makefile skeleton.
# Targets marked (Mx) are filled in during that milestone; until then they print a reminder.
# Recipes must be indented with a TAB.

SHELL := /bin/bash
.DEFAULT_GOAL := help
PY := uv run
SYSTEM ?= engine
SPLIT ?= dev
MODE ?= replay
SETS ?= numeric,financebench,narrative,unanswerable
TF_DIR := infra/terraform/envs/dev

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

# ---------- local dev (M0) ----------
up: ## Start Postgres+pgvector, Redis, Jaeger
	docker compose up -d && docker compose ps
down: ## Stop the local stack (keeps volumes)
	docker compose down
test: ## Unit + property tests (no live services)
	$(PY) pytest tests/unit tests/property -m "not live" -q
test-integration: ## Integration tests (Testcontainers)
	$(PY) pytest tests/integration -m "not live" -q
lint: ## Lint + type check
	$(PY) ruff check . && $(PY) ruff format --check . && $(PY) mypy app/engine
fmt: ## Format
	$(PY) ruff format . && $(PY) ruff check --fix .
check-gold: ## Verify gold eval files match eval/sets/CHECKSUMS (M4; runs in CI)
	$(PY) python -m eval.check_gold
gold-checksums: ## YOU run this after curating gold files; writes eval/sets/CHECKSUMS (M4)
	$(PY) python -m eval.check_gold --write

# ---------- data pipelines (M1-M3) ----------
fsds: ## Download FSDS quarters, e.g. make fsds FROM=2023q3 TO=2023q4 (M1)
	$(PY) python -m pipelines.fetch.download_fsds --from $(FROM) --to $(TO) --dest data/raw/fsds
spark-raw: ## Load FSDS quarters into raw Iceberg tables locally (M1)
	$(PY) python -m pipelines.spark.fsds_to_iceberg --env local --quarters $(Q)
spark-facts: ## Normalize facts locally (M1)
	$(PY) python -m pipelines.spark.normalize_facts --env local
dq: ## Data-quality report (M1)
	$(PY) python -m pipelines.spark.dq_checks --env local
emr-package: ## Zip pipelines/ to S3 artifacts (M1)
	@echo "TODO(M1): implement"
emr-run: ## Run a job on EMR Serverless, e.g. make emr-run JOB=normalize_facts (M1; asks first)
	@echo "TODO(M1): implement"
export: ## Export serving snapshot, e.g. make export VERSION=1 (M3)
	$(PY) python -m pipelines.spark.export_serving --env local --version $(VERSION)
load: ## Load a snapshot into local Postgres and swap (M3)
	$(PY) python -m serving.load.load_snapshot --version $(VERSION) --source ./exports
remote-load: ## Load a snapshot on the EC2 box via SSM (M9; asks first)
	@echo "TODO(M9): implement"

# ---------- eval (M4+) ----------
bench: ## Eval harness, e.g. make bench SYSTEM=engine SPLIT=dev MODE=replay
	$(PY) python -m eval.run --system $(SYSTEM) --sets $(SETS) --split $(SPLIT) --mode $(MODE) --out eval/results/
report: ## Regenerate docs/results.md auto tables
	$(PY) python -m eval.report
router-train: ## Train the router (M7), e.g. make router-train SEED=13
	$(PY) python -m ml.router.train --seed $(SEED)

# ---------- ops ----------
llm-spend: ## LLM spend from usage_log + eval results (M4/M8)
	@echo "TODO(M4): implement"
latency-report: ## p50/p95 per route and stage (M8)
	@echo "TODO(M8): implement"
triage: ## User-feedback triage list (M11)
	@echo "TODO(M11): implement"
freshness-report: ## T10 freshness lag report (M10)
	@echo "TODO(M10): implement"
status: ## What's running in AWS (EMR jobs, EC2, queues)
	@echo "EMR Serverless apps:"; aws emr-serverless list-applications --profile edgarqa --query 'applications[].{name:name,state:state}' --output table 2>/dev/null || echo "  (none / AWS not configured)"
	@echo "EC2 (tag project=edgarqa):"; aws ec2 describe-instances --profile edgarqa --filters "Name=tag:project,Values=edgarqa" --query 'Reservations[].Instances[].{id:InstanceId,state:State.Name,type:InstanceType}' --output table 2>/dev/null || echo "  (none)"
	@echo "SQS queues:"; aws sqs list-queues --profile edgarqa --queue-name-prefix edgarqa --output table 2>/dev/null || echo "  (none)"
cost-today: ## Cost Explorer for the last 2 days, project tag
	@aws ce get-cost-and-usage --profile edgarqa \
	  --time-period Start=$$(date -u -v-2d +%Y-%m-%d 2>/dev/null || date -u -d '2 days ago' +%Y-%m-%d),End=$$(date -u +%Y-%m-%d) \
	  --granularity DAILY --metrics UnblendedCost \
	  --filter '{"Tags":{"Key":"project","Values":["edgarqa"]}}' \
	  --group-by Type=DIMENSION,Key=SERVICE --output table 2>/dev/null || echo "cost-today: AWS not configured yet (Day 0)"

# ---------- infrastructure (asks before mutating) ----------
tf-init: ## terraform init
	terraform -chdir=$(TF_DIR) init
tf-plan: ## terraform plan
	terraform -chdir=$(TF_DIR) plan -out=tfplan
tf-apply: ## terraform apply (the saved plan)
	terraform -chdir=$(TF_DIR) apply tfplan
deploy: ## Tag-based deploy happens in GitHub Actions; this is a manual fallback (M9)
	@echo "TODO(M9): implement"
stop-serving: ## Stop the EC2 serving box (M9)
	@echo "TODO(M9): implement"
destroy-compute: ## Destroy serving/ingest/cdn modules, keep lake/budgets/IAM (M9)
	@echo "TODO(M9): implement"
destroy-all: ## Destroy everything except the lake bucket (needs CONFIRM=yes)
	@[ "$(CONFIRM)" = "yes" ] || (echo "Refusing: pass CONFIRM=yes" && exit 1)
	@echo "TODO(M12): implement"
