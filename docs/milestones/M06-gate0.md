# M6 Gate 0: unsupported claims before any verifier

Measured 2026-11-26 on the M5 dev run with the Haiku writer, corpus version 1. Sources, unless a line says otherwise: `eval/results/verifier/gate0_dev_pre.json`. Intervals on claim rates are Wilson 95%.

## Headline: text claims

| Text claims | n | Count | Rate | Wilson 95% | Key |
|---|---:|---:|---:|---|---|
| **Unsupported** | 118 | 0 | **0.0%** | [0.0, 3.2] | `headline_text_claims` |
| Partial | 118 | 4 | 3.4% | [1.3, 8.4] | `headline_text_claims` |

## Pre-registered rule (ADR-049, committed before the first judge call)
- Deciding number: the **text-claim** unsupported point estimate; `partial` stays in the denominator only.
- At or below 5.0% (at most 5 of 118): **no `entail.py`; a fully deterministic verifier** (numeric re-execution and the quote checks, plus policy and render). Above: keep one strict entailment pass.
- Observed: 0 of 118. **Branch: deterministic** (`preregistered_rule`). The interval's upper end is also below 5%.

## Numeric claims (separate; they do not decide headroom)
The judge does the arithmetic here, and the verifier will re-check these claims deterministically.

| Group | n | Unsupported | Partial | Wilson 95% (each) | Key |
|---|---:|---:|---:|---|---|
| All numeric | 102 | 0 | 0 | [0.0, 3.6] | `numeric_claims` |
| `numeric` | 62 | 0 | 0 | [0.0, 5.8] | `by_kind.numeric` |
| `numeric_derived` | 40 | 0 | 0 | [0.0, 8.8] | `by_kind.numeric_derived` |

All claims together (secondary): 220, with 0 unsupported and 4 partial (`all_claims`). No claim cited a missing passage or one the composer had not been shown (`missing_chunk`, `cited_but_not_shown`).

## Where the partials are
All 4 are FinanceBench text claims (`by_set.financebench.text`: 4 of 52). Narrative text claims: 0 of 56. Read them in `eval/results/verifier/gate0_dev_review.md`.
- `fb-00651 c1`: says "increased growth of 12.5%" without saying it is Adjusted Reported EPS; the quote also carries 11.5% for operational EPS.
- `fb-00723 c1`: gives one driver of a cost increase where the passage gives more.
- `fb-01328 c1`: infers "restructuring costs are 0" from the lack of a line item.
- `fb-01328 c2`: asserts an absence from the income statement that the cited passage cannot show.

Claims inside refused answers count: 65 of the 118 text claims are in refused answers (`by_answer.refused.text`).

## False refusal with no verifier
Dev `answerable_false_refusal` is **33.3%** (n=132, bootstrap 95% [25.8, 41.7]). The value is copied from the M5 results file (`dev_answerable_false_refusal_no_verifier`).

## Extra check, not pre-registered: M5's grounding rules on the same claims
Source: `eval/results/verifier/gate0_m5_grounding.json`. This is the existing structural check, **not** the M6 verifier.
- Quote not found in the cited passage: **0** (no `quote_not_in_chunk` in `by_rule`).
- It flags 26 of 220 claims: 25 text claims with `number_not_in_quote` and 1 numeric claim with `number_not_in_result`. The judge called 22 of the 26 supported and 4 partial.
- 18 of the 25 text flags involve only 4-digit years (`number_not_in_quote_year_only`).
- **What this means for `quotes.py`:** a naive "every number in the claim appears in the quote" rule would drop claims the judge supports. In 6 of the other 7, the stray tokens other than years are labels: a product name (787), "12(b)", days of a month or a quarter. The number parser should not treat these as values. The 7th, `fb-01328 c1`, is different: its "0" is the value the claim asserts, it is not in the quote, and the judge marked the claim partial because the inference does not follow. A rule should **not** exempt that one.

## How far to trust the zero
- **The judge is uncalibrated, and it said "unsupported" for none of the 220 claims.** It did mark 4 partial. No deliberately wrong claim was given to it, so its ability to call a claim unsupported on this data is `NOT MEASURED`.
- The judge saw the whole cited passage, which is looser than a check of the quote alone. It judged each claim without the question.
- Claims share questions, so the intervals are probably too narrow.
- This is dev only. No test-split figure exists.

## What it means for Phase B
- On this judge's metric **there is no headroom on dev**: dropping claims cannot take the unsupported rate below 0. The dev ablation can show only what the verifier costs (more refusals), or that it costs nothing.
- Whether the verifier catches wrong claims at all cannot come from this run. The adversarial set that would measure it was cut. Unit tests written for it will check its rules but will not be a measured result.

## Acceptance, cost and box
- Trace capture: accepted with no mismatches against `2026-11-26T00-27-09Z_engine-0.6.1_haiku` (147 questions, 220 claims; answer text, refusal, retrieved ids, route and token counts), replay mode, no API calls (`trace_acceptance`).
- Judge: `claude-sonnet-5-5`, `support-v1`, evidence `evidence-v1`, 220 requests in 11 batches, none failed. 234,992 input and 20,025 output tokens. **$0.2277** of the $2 cap, by harness accounting and not an invoice (`cost`).
- Box: start requested 06:05:39Z, confirmed stopped 06:09:48Z (`gate0_box_session.json`). Dollars `NOT MEASURED`.
- Reproduce at no cost: `uv run python -m eval.verifier_gate0 --max-usd 2 --mode replay --out-dir <dir>`. This was run once, and every field matched apart from the batch list.
