# M0 report: Foundation and accounts

**Status: NOT DONE.** Of the 10 checks below, 5 PASS, 1 PARTIAL (containers), 2 FAIL (AWS profile, Terraform) and 2 are NOT RUN (budget alert email, `/status`). Runbook: `docs/plan/M00_foundation.md` §6.

Run date: 2026-11-17. All commands were run from the repo root.

## Checklist

| # | DoD item | Result | Evidence |
|---|---|---|---|
| 1 | `make up`: all three containers healthy | **PARTIAL** | `docker compose ps`: `postgres-1` Up (healthy), `redis-1` Up (healthy), `jaeger-1` Up with **no healthcheck status** shown. Runbook step 3 asks for healthchecks; jaeger has none, so "healthy" is only "running" for it. `make up` was not run by /dod (the stack was already up for 15 minutes), and jaeger has no healthcheck, so "healthy" is verified for postgres and redis only. |
| 2a | `make test` passes locally | **PASS** | `make test` → `1 passed in 0.07s` |
| 2b | `make lint` passes locally | **PASS** | `make lint` → ruff `All checks passed!`, `25 files already formatted`, mypy `Success: no issues found in 1 source file` |
| 2c | CI passes in GitHub Actions | **PASS** | `gh run list`: `completed success` for commit `chore(m0): project scaffold…`, run 44020332784, 14s. CI runs `pytest tests/unit` only, so `tests/property` is not run in CI (differs from `make test`). |
| 3a | `aws sts get-caller-identity --profile edgarqa` works | **FAIL** | `aws: [ERROR]: The config profile (edgarqa) could not be found` |
| 3b | Terraform applied (lake bucket, Glue DBs, budgets, OIDC role) | **FAIL** | Skeleton now written (`infra/terraform/envs/dev` plus modules `lake`, `budgets`, `github_oidc`). With Terraform 1.17.2: `terraform fmt -recursive` exit 0 (no files changed), `terraform init -backend=false` succeeded, `terraform validate` → "Success! The configuration is valid." **Not applied**: no `plan` or `apply` has been run, and the `edgarqa` AWS profile (3a) doesn't exist yet. |
| 4 | Budget alert email received in a test | **NOT RUN** | Depends on 3b (no budgets exist). Needs a manual test by Shekar. |
| 5a | Hook blocks gold-file edits | **PASS** | Ran `.claude/hooks/protect-paths.sh` with `CLAUDE_PROJECT_DIR` set: `eval/sets/x.jsonl` → "Blocked by …", exit 2; `eval/sets/drafts/x.jsonl` → exit 0. Hook is registered in `.claude/settings.json` (PreToolUse, `Edit\|Write`). Tested the script directly, not via a live Edit call. |
| 5b | `/status` shows the subscription | **NOT RUN** | Interactive command; I can't run it. Shekar to confirm. |
| 6 | `docs/milestones/M00-report.md` written | **PASS** | This file. |

## Other runbook outputs checked (not DoD items)

| Item | Result | Evidence |
|---|---|---|
| `tests/conftest.py` socket block + `EDGARQA_LLM_MODE=stub` | Present | Fixtures `_default_llm_stub`, `_block_external_sockets`. No test exercises the block yet, so it is unverified. |
| `.env` not committed | OK | `git ls-files .env` → 0 files; `.env` in `.gitignore` line 2; `*.tfstate` and `warehouse/` also ignored. |
| ADR-001…004 seeded | Present | `docs/decisions.md`: ADR-001 to ADR-004 (dated 2026-11-16). Whether they were rewritten in Shekar's words is not something I can check. |
| `ANTHROPIC_API_KEY` not used | OK | Only `.env.example` and docs mention it, as a warning; the variable is `EDGARQA_ANTHROPIC_API_KEY`. |
| Makefile targets from runbook §3.4 | Present | `grep` of `Makefile`: `up`, `down`, `test`, `lint`, `fmt`, `bench`, `status`, `cost-today`, `tf-init`, `tf-plan`, `tf-apply` all defined. Caveat: `tf-init` runs plain `init`, so it needs `-backend-config="bucket=…"`. |

## Numbers

| Value | Source file | Key |
|---|---|---|
| NOT MEASURED | n/a | M0 has no result files (`eval/results/*.json`, `load/results/*.json` or job metrics). The only counts in this report (1 test passed, 25 files formatted, CI run time) are command output, not result-file values. |

## What's left before M0 is done

1. Create the `edgarqa` AWS CLI profile (runbook step 5) and verify `aws sts get-caller-identity --profile edgarqa`. Shekar's action.
2. ~~Write the Terraform skeleton and run `fmt` and `validate`.~~ Done (see 3b).
3. Shekar runs `terraform init -backend-config="bucket=edgarqa-tfstate-<account-id>"` (the Makefile's `tf-init` runs plain `init` and needs updating), then `make tf-plan`, reviews the plan, then `make tf-apply`. Claude must not apply (CLAUDE.md rule 8).
4. Shekar tests the budget alert ($0.01 threshold, wait for the email, restore).
5. Shekar confirms `/status` shows the subscription.
6. Re-run `/dod M0` after steps 1–5.

## Open issues

- Jaeger has no healthcheck, so "all three healthy" is not strictly verifiable. Adding one is optional.
- `make test` runs `tests/unit tests/property`; CI runs only `tests/unit`. The runbook specifies unit only for CI, so this is consistent with the runbook, but local and CI differ.
- The socket-blocking fixture has no test of its own.
- `tests/unit` plus `tests/property` hold one passing test in total, which is a thin base.

## Decisions that may need an ADR

- None required by M0 itself. If Shekar wants a jaeger healthcheck or to align the CI and local test targets, a short note in `docs/decisions.md` would be enough.
