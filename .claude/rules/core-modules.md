---
paths:
  - "pipelines/common/fiscal_ref.py"
  - "app/engine/plan/**"
  - "app/engine/verify/**"
  - "app/engine/sql_guard/**"
  - "app/engine/router/**"
  - "ml/router/**"
  - "eval/metrics.py"
---

# Core modules: interview-critical code

Shekar writes the implementation in the files listed in CLAUDE.md ("Shekar-owned modules"). When you work near these files:

- Your job is to **write tests, review, explain, and find counterexamples**, not to implement.
- When asked to review, cite `file:line` and give a concrete failing input for every bug you claim.
- Propose changes as a unified diff **in chat**; don't apply it unless Shekar launched the session with `EDGARQA_ALLOW_CORE_EDITS=1` and asked you to.
- Prefer property-based tests (Hypothesis) for parsers, tolerances, fiscal-period derivation and SQL guard rejection.
- In the plan compiler and verifier, values are `Decimal` in base units; provenance expressions use only `f:<fact_id>`, numbers, `+ - * /` and parentheses.
- `pipelines/spark/fiscal.py` and `pipelines/spark/versions.py` were implemented by Claude at Shekar's request (2026-11-17, ADR-021) against `tests/integration/test_differential.py`; Claude may maintain them. They must keep agreeing with `pipelines/common/fiscal_ref.py`, which stays Shekar's: propose changes to the reference as a diff in chat.
- In `app/engine/verify`, `numbers.py`, `numeric.py`, `quotes.py` and `policy.py` are Shekar's (`numbers.py` since 2026-11-26, ADR-049 option A, replacing M06 §3.1). Where a stub's docstring disagrees with a decision recorded in ADR-049, the ADR wins.
- Neighbouring files that Claude may implement (for example `app/engine/verify/pipeline.py`, `render.py`, `evidence.py`, `models.py`, `app/engine/router/classifier.py`, `ml/router/train.py`) must keep the interfaces the owned modules expect.
