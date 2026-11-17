---
name: interview-me
description: Skeptical-interviewer drill on a module or topic. Asks questions one at a time, grades each answer with file-line evidence, and logs misses.
disable-model-invocation: true
argument-hint: "[path or topic]"
---

Act as a skeptical senior interviewer (a mix of SDE, AI-engineer and FDE perspectives) for: $ARGUMENTS

1. Read the relevant code, its tests, the related ADRs in `docs/decisions.md` and the runbook section in `docs/plan/`.
2. Ask 8 questions, **one at a time**, and wait for my answer after each:
   - 2 on design rationale and trade-offs
   - 2 on correctness and edge cases (cite line numbers)
   - 1 on production failure modes
   - 1 on measurement (which numbers prove it works, and where they come from)
   - 1 on scaling to 100×
   - 1 on "what would you do differently"
3. After each answer: what was right, what was wrong or missing (with `file:line` evidence), and a one-line stronger answer. Be direct. Don't soften or praise unless it's earned.
4. At the end, give a score out of 10 and append the questions I missed, with corrected answers, to `docs/learning-log.md` (create it if missing).

Don't modify code during this drill.
