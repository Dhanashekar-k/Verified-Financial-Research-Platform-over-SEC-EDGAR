---
name: results-audit
description: Verify that every number in README.md, docs/results.md and docs/milestones/*.md traces to a results or metrics file.
disable-model-invocation: true
context: fork
agent: numbers-auditor
---

Audit `README.md`, `docs/results.md` and `docs/milestones/*.md`.

For every number (percentages, latencies, costs, counts, row counts, CIs):
1. Find its source file and key (`eval/results/*.json`, `load/results/*.json`, job-metrics JSON, bench JSON, M1/M3/M10 reports).
2. Mark it **TRACED** (same value), **MISMATCH** (the source says something else; give both) or **UNTRACED** (no source found).
3. Check that the blocks between `<!-- AUTO:START -->` and `<!-- AUTO:END -->` match what `eval/report.py` would generate for the run IDs they reference.

Output a table (file, line, number, status, source) and a one-line verdict: PASS only if there are no MISMATCH or UNTRACED entries. Don't edit any file.
