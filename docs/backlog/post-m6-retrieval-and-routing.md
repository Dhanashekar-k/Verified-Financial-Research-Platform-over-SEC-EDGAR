# Post-M6 work item: FinanceBench is bound by retrieval and routing

Opened 2026-11-26. **Not part of M6.** The verifier only drops claims, so it cannot fix this.

## Evidence
Source: `eval/results/2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` (test split), key `per_question`.

| Count | Filter |
|---|---|
| 83 | `set == "financebench"` and `refused` |
| 58 of those 83 | `recall_at_20 == 0` |
| 6 of 6 | FinanceBench questions containing "working capital": `route == "narrative"` |

Working capital is not a metric-catalog key. A numeric question about it therefore goes to filing text instead of the XBRL path.

**Caveat on `recall_at_20`:** `retrieved_top20` holds at most 8 ids per question in this file (the engine's `top_k` is 8). So `recall_at_20 == 0` means the gold chunk was not among the 8 or fewer retrieved. It says nothing about ranks 9 to 20. Measure recall at a true depth of 20 or more on dev before choosing between ranking and candidate-generation fixes.

## Direction
- Expand the metric catalog (reviewed definitions, like ADR-042), starting with the concepts dev questions ask for.
- Improve retrieval and routing. Not the verifier.

## Rules for this work
- Develop on **dev** only.
- These test rows have now informed a diagnosis. Any later test result for FinanceBench must say so.
