---
name: adr
description: Append the next numbered Architecture Decision Record to docs/decisions.md.
disable-model-invocation: true
argument-hint: "\"<decision title>\""
---

Create the next ADR in `docs/decisions.md` titled: $ARGUMENTS

Use this format, keeping it under 250 words:

```
## ADR-NNN: <title> (<YYYY-MM-DD>)
**Status:** proposed
**Context:** facts and measured numbers (with source files) that force a decision
**Options considered:** at least two, each with its main trade-off
**Decision:** what we chose
**Consequences:** TODO(Shekar): write this in my own words
**How to revisit:** which measurement or event would change this decision
```

NNN = the highest existing ADR number + 1. Don't edit other ADRs. Draft from the current conversation and the runbook; mark anything uncertain as `TODO(Shekar)`.
