---
name: milestone
description: Start a milestone session. Load its runbook from docs/plan, restate the contract, check prerequisites and propose a plan before any edits.
disable-model-invocation: true
argument-hint: "[M0..M12 | S1 | S2]"
---

Start milestone $ARGUMENTS.

1. Find the runbook in `docs/plan/` whose filename starts with the milestone ID (M5 → `M05_*.md`, S1 → `S1_*.md`). Read all of it, plus the sections of `docs/plan/05_data_contracts.md` and `docs/plan/03_architecture_and_stack.md` it references.
2. Restate in at most 15 lines: the goal, inputs, outputs, the DoD checklist, the time budget, and **which files Shekar owns in this milestone** (you won't implement those).
3. Check prerequisites: the previous milestone's report exists in `docs/milestones/`; `make test` passes; any data this milestone needs exists (show counts with cheap commands). Report anything missing or failing.
4. Propose a numbered step plan: small commits, tests first where the runbook says so, the exact files to create or modify per step, and what Shekar must do himself (labels, owned modules, apply/run commands).
5. Stop and wait for approval. Don't edit anything in this turn.
