# M6: Verifier (no unsupported claims reach the user)

**Time:** about 16 h · **Roles:** AI engineer (the main differentiator), SDE (testable design) · **Depends on:** M5

## 1. Goal

Each claim is checked deterministically first and by an LLM only where judgment is unavoidable. Unsupported claims are dropped. If what remains doesn't answer the question, the system refuses and shows what it found. Measured with an ablation, an adversarial set and a hand audit.

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| `Answer` from the composer (claims with provenance) | `app/engine/verify/{numbers.py, numeric.py, quotes.py, entail.py, policy.py, render.py}` (**you**: numeric, quotes, policy) |
| `QueryPlan`, `facts`, `chunks` | Verified `Answer` + per-claim `VerifierResult` (`05_data_contracts.md` §4.4) |
| — | `eval/sets/adversarial_claims.jsonl` (about 40, **you write**) |
| — | Results: verifier precision/recall on the adversarial set; ablation with/without verifier; the strictness sweep curve; 50-claim hand audit with a Wilson CI |

## 3. Design

### 3.1 Number parsing (`numbers.py`; Claude writes it, property-tested)
`parse_numbers(text) -> list[ParsedNumber(value: Decimal, unit_hint, precision, span)]` handles `$383.3 billion`, `383,285 million`, `$1.2B`, `29.8%`, `(1,234)` (negative), `-3.1%`, `1.5x`, `bps`, and scale words (thousand/million/billion/trillion, K/M/B). `precision` comes from the decimals shown and the scale (for example "383.3 billion" → ±0.05e9). Years (2019–2030 near "fiscal"/"FY") are classified as years, not values.

### 3.2 Numeric checks (`numeric.py`; **you**)
For `numeric` / `numeric_derived` claims with `provenance.type == "expr"`:
1. **Provenance exec:** every `fact_id` exists in the live `facts`; re-evaluate `expr` with `Decimal` → `v*`. A mismatch with `claim.value` beyond the tolerance → `unsupported`.
2. **Tolerance:** `tol = max(0.005 × |v*|, precision_of(claim.display))`. A display of "29.8%" allows ±0.0005 on the ratio. Percent and ratio are normalized.
3. **Number in text:** at least one number parsed from `claim.text` matches `v*` within its precision. **No other non-year number** in the text is left unsupported.
4. **Metadata match:** the facts' `cik` is in the plan's companies; `fiscal_year`/`fiscal_period` match the plan's periods (for growth, t and t−1); `uom` is consistent with the metric's unit; the `tag` is in the catalog's tags for that metric (or its formula operands).
5. **(After S1)** `anomaly_score` above the threshold → status `flagged` (shown with a warning, not "verified").

### 3.3 Text checks (`quotes.py` deterministic, **you**; `entail.py` LLM)
For `text` claims with `provenance.type == "chunk"`:
1. The chunk exists and its `cik` is in the plan's companies (catches a citation of the wrong company).
2. **Quote substring:** `normalize(quote) ⊂ normalize(chunk.text)` (case, whitespace, curly quotes, dashes, non-breaking spaces).
3. **Numbers in the claim must appear in the quote** (via `parse_numbers` and precision matching). This catches "grew 8%" when the source says 6%.
4. **Entailment (LLM):** Haiku 5.5, structured verdict `{label: SUPPORTED|PARTIAL|NOT_SUPPORTED, reason}`, given the claim, the quote and ±1 neighbouring sentence. The system prompt is strict: every assertion in the claim must be stated or directly implied. Calls run in parallel per claim and are cached on `(claim_text, chunk_id)`.
   - Optional comparison: a small open NLI cross-encoder instead of the LLM, measured on the adversarial set for cost and latency vs accuracy.

### 3.4 Policy and rendering (`policy.py` **you**, `render.py`)
- Drop `unsupported`. `PARTIAL`: drop in strict mode (the default) or keep it marked "partially supported" in lenient mode.
- **Sufficiency:** numeric/hybrid plans need a verified numeric claim for every requested (company, metric, period). Narrative plans need at least one verified text claim per narrative topic. If that isn't met, return a `refusal` with `found: [surviving claims]` and `missing: [...]`.
- **Render `answer_markdown` deterministically from the surviving claims** (in composer order, with `[n]` markers), so a dropped claim can never leave dangling prose. Composer prose isn't shown directly; it's input to the renderer.
- Verification status: `verified` (all kept claims passed), `partially_verified` (some dropped, still sufficient), `refused`.

## 4. Evaluation

1. **Adversarial claims (you write about 40):** wrong year, wrong company, million vs billion, percent vs ratio, sign flip, rounding that's "close but wrong," a quote absent from the chunk, a number changed in the text, a partially supported text claim, a claim citing the right chunk but adding an unsupported cause, and about 10 **correct** claims (to measure false drops). Report verifier precision and recall for catching bad claims.
2. **Ablation on test:** engine with and without the verifier: accuracy, unsupported-claim rate (independent judge, a different model and prompt from `entail.py`), refusal rate on unanswerable, false refusal on answerable, added latency and cost.
3. **Strictness sweep on dev:** numeric tolerance × PARTIAL policy × entailment strictness → a curve of unsupported-claim rate vs false-refusal rate. Pick the operating point (ADR-010) and say why.
4. **Hand audit:** sample 50 kept claims from the test run and label them supported or unsupported yourself. Report the unsupported rate with a **Wilson 95% interval**, plus judge-vs-you agreement.

## 5. Claude Code session plan

```
/milestone M6
Step 1: numbers.py per M06 §3.1 with Hypothesis property tests (format random Decimals with random
scale words/precision → parse → equal within precision; parentheses negatives; percent vs bps).
```
```
Step 2: Use the test-writer subagent: write tests for numeric.py, quotes.py and policy.py from §3.2–3.4,
including every adversarial category in §4.1 as unit cases. Don't implement them.
```
```
Step 3: entail.py (Haiku structured verdict, parallel, cached) + render.py + wiring into
engine.answer with a --verifier on|off flag. Then the strictness-sweep runner and the ablation
runner (results JSON → report tables).
```
You implement `numeric.py`, `quotes.py` and `policy.py` until the tests pass, write `adversarial_claims.jsonl`, do the 50-claim audit, then `/dod M6` and `/interview-me app/engine/verify`.

## 6. Definition of done

- [ ] T3, T5 and T6 measured on test, with CIs; the pre-verifier unsupported rate reported too.
- [ ] Adversarial-set precision and recall reported.
- [ ] The sweep curve and the chosen operating point (ADR-010).
- [ ] Hand audit: Wilson interval + judge agreement.
- [ ] If a target is missed, `docs/results.md` explains why and what you'd try next. A missed target, honestly explained, is fine.

## 7. Pitfalls

- An LLM judge that agrees with the LLM verifier because they're the same prompt or model. Keep them independent.
- Normalization asymmetry: quotes normalized but chunks not, or the reverse.
- Treating fiscal years in text as values (false drops).
- Dropping claims but keeping the composer's prose (dangling, unverified sentences). Render from claims.
- Over-strictness: a near-zero unsupported rate bought with a 40% false-refusal rate isn't a win. Show the curve.

## 8. Interview prep

- "How does the verifier decide a number matches?" (precision-derived tolerance, re-execution, metadata).
- "What fraction of errors do deterministic checks catch before the LLM?" Measure it: share of drops by check type.
- "What's your false-refusal rate and why that operating point?"
- "How do you know the verifier isn't just the judge agreeing with itself?"
