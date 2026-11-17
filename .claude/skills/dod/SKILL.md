---
name: dod
description: Run a milestone's Definition-of-Done checks and write docs/milestones/<ID>-report.md using numbers copied only from result files.
disable-model-invocation: true
argument-hint: "[milestone ID]"
---

Definition-of-done check for milestone $ARGUMENTS.

1. Read the DoD checklist in the matching runbook in `docs/plan/`.
2. For each item, run the command(s) that verify it (tests, make targets, SQL, file checks). Record **PASS / FAIL / NOT RUN**, with the command and the key output lines as evidence.
3. Numbers: copy them **only** from files (`eval/results/*.json`, `load/results/*.json`, job-metrics JSON, bench JSON). For each number, give the file path and JSON key. Never estimate, extrapolate or re-round. If no source exists, write `NOT MEASURED`.
4. Write `docs/milestones/<ID>-report.md` with: the checklist with evidence; a numbers table (value, source file, key); open issues; and decisions that need an ADR.
5. If any item FAILs, say clearly that the milestone isn't done and list what's left. Don't mark it done.
