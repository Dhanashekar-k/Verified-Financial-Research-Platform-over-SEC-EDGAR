## What & why
<!-- One or two sentences. Link the issue: Closes #NN -->

## Milestone / scope
<!-- e.g. M1 — XBRL lakehouse. Which runbook section does this satisfy? -->

## How to verify
- [ ] `make test` and `make lint` green locally
- [ ] New behavior has a test (or say why N/A)
- [ ] No invented numbers — results trace to `eval/results/*.json` (or N/A)
- [ ] Contracts unchanged, or `docs/plan/05_data_contracts.md` + Pydantic models + an ADR updated
- [ ] No `terraform apply` / `aws` mutations in this PR

## Reviewer notes
<!-- What should the reviewer scrutinize? What did you deliberately leave out? -->
