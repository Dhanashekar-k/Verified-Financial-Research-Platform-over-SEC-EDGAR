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
COMPOSER ?=
EF_SEARCH ?= 800
TF_DIR := infra/terraform/envs/dev

# Spark (local Iceberg, the integration and differential tests) needs a JDK 17. The caller's JAVA_HOME wins;
# otherwise use Homebrew's openjdk@17 or a Debian-style JDK if one exists. The tests fail loudly when no JDK
# works (tests/integration/conftest.py); they never skip.
JAVA_HOME ?= $(firstword $(wildcard /opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home /usr/lib/jvm/java-17-openjdk-amd64))
ifneq ($(JAVA_HOME),)
export JAVA_HOME
export PATH := $(JAVA_HOME)/bin:$(PATH)
endif

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

# ---------- local dev (M0) ----------
up: ## Start Postgres+pgvector, Redis, Jaeger
	docker compose up -d && docker compose ps
down: ## Stop the local stack (keeps volumes)
	docker compose down
test: ## Unit + property tests (no live services); JAVA_HOME is set for Spark-backed tests
	$(PY) pytest tests/unit tests/property -m "not live" -q
test-integration: ## Integration tests (local Spark/Iceberg, Testcontainers); needs a JDK 17, fails without one
	$(PY) pytest tests/integration -m "not live" -q
lint: ## Lint + type check
	$(PY) ruff check . && $(PY) ruff format --check . && $(PY) mypy app/engine app/llm
fmt: ## Format
	$(PY) ruff format . && $(PY) ruff check --fix .
check-gold: ## Verify gold eval files match eval/sets/CHECKSUMS (M4; runs in CI)
	$(PY) python -m eval.check_gold
label-sections: ## YOU run this: pick section starts from candidates; resumes where you left off (M2)
	$(PY) python -m eval.label_sections

calibration-worksheet: ## Blind worksheet of every answered FinanceBench row of a run, e.g. make calibration-worksheet RESULTS=eval/results/<run>.json (M4; label it, then YOU promote it)
	@test -n "$(RESULTS)" || { echo "usage: make calibration-worksheet RESULTS=eval/results/<run>.json"; exit 2; }
	$(PY) python -m eval.judges.calibrate template --results $(RESULTS) --all
label-calibration: ## YOU run this: label the worksheet blind (c/i/r/skip), resumable; prints the promote command at the end (M4)
	$(PY) python -m eval.judges.label
kappa: ## Cohen's kappa of the judge vs YOUR labels in eval/judges/calibration.jsonl (same RESULTS run as the worksheet); writes eval/judges/kappa.json + a disagreement report (M4)
	@test -n "$(RESULTS)" || { echo "usage: make kappa RESULTS=eval/results/<run>.json"; exit 2; }
	$(PY) python -m eval.judges.calibrate kappa --results $(RESULTS) --all --report eval/judges/drafts/calibration_disagreements.md

promote-gold: ## YOU run this after reviewing a draft: make promote-gold SET=narrative VERIFIED_BY=<name> | SET=unanswerable (no reviewer field; VERIFIED_BY ignored) (M4)
	@test -n "$(SET)" || { echo "usage: make promote-gold SET=narrative VERIFIED_BY=<name> | SET=unanswerable"; exit 2; }
	$(PY) python -m eval.gen.promote_gold $(SET) $(if $(VERIFIED_BY),--verified-by "$(VERIFIED_BY)",)
gold-checksums: ## YOU run this after curating gold files; writes eval/sets/CHECKSUMS (M4)
	$(PY) python -m eval.check_gold --write

# ---------- data pipelines (M1-M3) ----------
fsds: ## Download FSDS quarters, e.g. make fsds FROM=2023q3 TO=2023q4 (M1)
	$(PY) python -m pipelines.fetch.download_fsds --from $(FROM) --to $(TO) --dest data/raw/fsds
spark-raw: ## Load FSDS quarters into raw Iceberg tables locally (M1)
	$(PY) python -m pipelines.spark.fsds_to_iceberg --env local --quarters $(Q)
spark-facts: ## Normalize facts locally (M1)
	$(PY) python -m pipelines.spark.normalize_facts --env local
fetch-reference: ## Freeze the SEC ticker map and the S&P 500 CSV under data/ (M1; network)
	$(PY) python -m pipelines.fetch.reference_data
spark-companies: ## Build curated.companies from raw.fsds_sub + the frozen reference files (M1)
	$(PY) python -m pipelines.spark.build_companies --env local
export-companies: ## Export the S&P 500 CIKs from curated.companies to data/targets/tracked_ciks.csv (M2)
	$(PY) python -m pipelines.spark.export_companies --env local
list-filings: ## List 10-K/10-Q targets to data/targets/filings.csv; sec.gov network (M2)
	$(PY) python -m pipelines.fetch.list_filings
bench-m1: ## DuckDB vs local Spark on FSDS quarters, e.g. make bench-m1 Q=2023q3,2023q4 LABEL=2q (M1)
	$(PY) python -m pipelines.duckdb.benchmark run --quarters $(Q) --label $(LABEL)
dq: ## Data-quality report (M1)
	$(PY) python -m pipelines.spark.dq_checks --env local
emr-package: ## Build dist/pipelines-<sha>.zip; UPLOAD=s3://<lake>/artifacts publishes it (a cloud write: you run it)
	$(PY) python -m pipelines.cloud.package $(if $(UPLOAD),--upload $(UPLOAD),)
fetch-model: ## Download the quantized bge ONNX into data/models (needed by the embedding tests; a read from Hugging Face)
	$(PY) python -m scripts.stage_model download
fetch-reranker: ## Download the cross-encoder reranker into data/models/rerankers (a read from Hugging Face; M5). MODEL=<name> for the alternative
	$(PY) python -m scripts.fetch_reranker $(if $(MODEL),--model $(MODEL),)
stage-model: ## Upload data/models to the lake bucket for Glue; YES=1 uploads (a cloud write)
	$(PY) python -m scripts.stage_model upload --to s3://edgarqa-lake-019593222039/artifacts/models/bge-small-en-v1.5 $(if $(YES),--yes,)
glue-deploy: ## Publish Glue scripts + code bundle to the lake bucket; YES=1 uploads (a cloud write)
	scripts/glue_deploy.sh
emr-image-build: ## Build the custom arm64 EMR image locally (ADR-024); does not push
	docker build --platform linux/arm64 -t edgarqa-emr:$$(git rev-parse --short=12 HEAD) infra/emr
emr-image-push: ## Push the image to ECR: ECR_URL=<acct>.dkr.ecr.<region>.amazonaws.com/edgarqa-emr YES=1 (a cloud write: you run it)
	@test -n "$(ECR_URL)" || { echo "set ECR_URL (terraform output ecr_repository_url)"; exit 2; }
	@sha=$$(git rev-parse --short=12 HEAD); registry="$(firstword $(subst /, ,$(ECR_URL)))"; \
	region=$$(echo "$$registry" | cut -d. -f4); \
	if [ "$(YES)" != "1" ]; then echo "dry run: would push edgarqa-emr:$$sha to $(ECR_URL) (region $$region); add YES=1"; exit 0; fi; \
	if [ -n "$$(git status --porcelain --untracked-files=no)" ]; then echo "error: uncommitted changes: tag $$sha would not name this build (ECR tags are immutable)"; exit 2; fi; \
	set -e; \
	aws ecr get-login-password --region "$$region" | docker login --username AWS --password-stdin "$$registry"; \
	docker tag edgarqa-emr:$$sha $(ECR_URL):$$sha; \
	docker push $(ECR_URL):$$sha
emr-run: ## Show the EMR run for JOB=normalize_facts; YES=1 starts it and spends money (you run it); EMR_ARGS="--facts-rows N --arg=--x"
	$(PY) python -m pipelines.cloud.emr_run --job $(JOB) --git-sha $$(git rev-parse --short=12 HEAD) $(EMR_ARGS) $(if $(filter 1,$(YES)),--yes,)
parse-filings: ## Parse filings + chunks locally (M2); FB=1 adds FinanceBench PDFs (needs data/financebench_companies.csv)
	$(PY) python -m pipelines.spark.parse_filings --env local --html-glob data/raw/filings $(if $(FB),--financebench-dir data/raw/financebench --financebench-companies data/financebench_companies.csv,)
load-filings-html: ## Load the fetch manifest into raw.filings_html (M2)
	$(PY) python -m pipelines.spark.load_filings_html --env local
fetch-filings: ## Fetch filing HTML for data/targets/filings.csv; sec.gov network (M2; the full list belongs on an AWS box)
	$(PY) python -m pipelines.fetch.fetch_filings $(if $(DEST),--dest $(DEST),)
fetch-financebench: ## Download FinanceBench questions + cited PDFs from GitHub (M2)
	$(PY) python -m pipelines.fetch.fetch_financebench
export: ## Export serving snapshot, e.g. make export VERSION=1 (M3)
	$(PY) python -m pipelines.spark.export_serving --env local --version $(VERSION)
load: ## Load a snapshot into local Postgres and swap (M3)
	$(PY) python -m serving.load.load_snapshot --version $(VERSION) --source ./exports
remote-load: ## Load a snapshot on the EC2 box via SSM (M9; asks first)
	@echo "TODO(M9): implement"

# ---------- eval (M4+) ----------
verify-adversarial: ## M6: run the deterministic verifier over eval/sets/adversarial_claims.jsonl ($$0)
	$(PY) python -m eval.verifier_adversarial
verify-ablation: ## M6: dev ablation, engine with and without the verifier, from the saved trace ($$0)
	$(PY) python -m eval.verifier_ablation
bench: ## Eval harness, e.g. make bench SYSTEM=baseline SPLIT=dev MODE=replay (DSN from $$EDGARQA_SERVING_DSN)
	$(PY) python -m eval.run --system $(SYSTEM) --sets $(SETS) --split $(SPLIT) --mode $(MODE) $(if $(COMPOSER),--composer $(COMPOSER),) --ef-search $(EF_SEARCH) --out eval/results/ $(if $(CORPUS_VERSION),--corpus-version $(CORPUS_VERSION),)
splits: ## Stratified dev/test splits -> eval/sets/drafts/splits.json (M4; Shekar promotes)
	$(PY) python -m eval.splits
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
