# M4: Evaluation sets and harness (built before the engine)

**Time:** about 14 h · **Roles:** AI engineer (eval rigor is the main signal), SDE (reproducibility) · **Depends on:** M3

## 1. Goal

Know your score before and after every change. Four question sets, a dev/test split, one command, deterministic scoring where possible, a calibrated LLM judge where not, bootstrap confidence intervals, and a naive-RAG baseline measured first.

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| FinanceBench JSONL + indexed PDFs (M2) | `eval/sets/financebench.jsonl` (150) |
| `curated.facts` / Postgres `facts` | `eval/sets/numeric_auto.jsonl` (300) with a second-source check column |
| Chunks from Items 1A / 7 / 7A | `eval/sets/narrative.jsonl` (50, LLM-drafted, **you verify**) |
| — | `eval/sets/unanswerable.jsonl` (50), `eval/sets/splits.json` (dev ≈ 30% / test ≈ 70%, stratified) |
| — | `app/llm/client.py` (registry, structured outputs, caching, batch, accounting, stub, record/replay) |
| — | `eval/run.py`, `eval/report.py`, `eval/judges/` (+ `calibration.jsonl`, your 30 labels) |
| — | Baseline results `eval/results/<run>_baseline.json` and tables in `docs/results.md` |

## 3. Steps

### 3.1 LLM client (3 h; Claude writes it, you review the accounting)
`app/llm/client.py`:
- Registry: `planner=claude-haiku-5-5`, `composer=claude-sonnet-5-5|claude-haiku-5-5`, `verifier=claude-haiku-5-5`, `judge=claude-sonnet-5-5` (configurable per run).
- `structured(model, system, messages, schema: type[BaseModel]) -> (obj, Usage)` using **structured outputs** (`output_config.format` with `type: "json_schema"`) [Certain that the feature is GA; check the docs for the exact request shape and the supported-model list].
- **Prompt caching:** put static content (system prompt, metric catalog, schema instructions) first with a cache breakpoint [Certain that caching exists; the minimum cacheable length is in the docs].
- Retries: 429/529/5xx with exponential backoff; per-call timeout.
- **Accounting:** every call returns `Usage(input, cache_write, cache_read, output, cost_usd, latency_ms, model)` priced from a table in `app/llm/prices.py` (the prices in `04_aws_and_cost_plan.md` §5).
- **Modes:** `live`, `stub` (deterministic fakes for tests), `record` and `replay` (store live responses keyed by a request hash in `eval/cassettes/`; replay costs $0 when you only change scoring code), and `batch` (Message Batches: submit all requests, poll, collect; 50% cheaper [Certain]).
- Reads `EDGARQA_ANTHROPIC_API_KEY` only.

### 3.2 FinanceBench set (1 h)
Convert the HF JSONL to `financebench.jsonl` (`05_data_contracts.md` §6). Map `doc_name` → chunks with `doc_source='financebench_pdf'`. Keep **all 150**.

### 3.3 Auto-generated numeric set (3 h; generator by Claude, sampling design by you)
`eval/gen/make_numeric_set.py --n 300 --seed 13`:
- Templates: `single_value` (120), `growth` YoY % (80), `ratio` such as margins (60), `compare` two companies (40).
- Stratify by SIC sector, fiscal year (2020–latest), metric (from the catalog) and annual vs quarterly. Include about 20 restated facts (gold = the **original** value per ADR-017; record the latest value too).
- Gold is computed by SQL, together with `gold_fact_ids`. Keep `tolerance_rel = 0.01`.
- Paraphrase questions with Haiku 5.5 (temperature 0.7) for diversity. **You check 30 paraphrases** for meaning drift and fix the generator if any drift.
- **Second-source check:** 30 random golds against the companyfacts API (automated), and 10 against the 10-K text (you, by hand). Record `second_source_check` and report agreement.

### 3.4 Narrative set (2.5 h; **you verify all 50**)
- Sample 120 chunks from Items 1A, 7 and 7A across sectors and years. Sonnet 5.5 drafts one question per chunk with 2–4 `gold_points` and the `gold_chunk_ids` (including a second supporting chunk when one exists).
- You keep the best 50: rewrite unnatural questions, fix the points, and set `verified_by`. Include about 10 that need two chunks.

### 3.5 Unanswerable set (1 h; you write them)
About 10 each of: future guidance that isn't in filings; information not disclosed (for example office-level headcount); out of scope (stock price, buy/sell advice, private companies); false premise ("Why did Apple's revenue double in FY2023?"); wrong entity–period combinations (a fiscal year before the company filed with the SEC).

### 3.6 Splits and gold checksums (0.5 h)
`splits.json`: stratified dev/test per set (seed fixed). **Iterate on dev only.** Report test (and all) at milestones. Note in the README that you tuned on dev.
Have Claude write `eval/check_gold.py` (SHA-256 of every gold file vs `eval/sets/CHECKSUMS`). **You** run `make gold-checksums` after each curation pass and commit the result. CI runs `make check-gold`, which catches gold edits made through the shell, where the hook can't see them.

### 3.7 Harness `eval/run.py` (2.5 h; **you write the metric definitions**)
```
uv run python -m eval.run --system baseline|engine --sets numeric,financebench,narrative,unanswerable \
   --split dev|test|all --mode replay|live|batch --composer sonnet|haiku --out eval/results/
```
- Calls the engine **in-process** through one interface, `answer(question: str, cfg) -> Answer` (the same `Answer` JSON schema for the baseline and the engine, so scoring is identical).
- **Metrics** (you write `eval/metrics.py`):
  - *Numeric accuracy:* the primary claim's `value` is within `tolerance_rel` of gold after unit normalization (percent ↔ ratio, thousands/millions).
  - *FinanceBench accuracy:* the judge labels correct / incorrect / refused against `gold_answer`. Report accuracy and refusal rate.
  - *Narrative:* the judge scores gold-point coverage (0–1) and contradiction (yes/no); citation precision is the share of cited chunks that the judge says support the claim.
  - *Refusal* on unanswerable (deterministic: `refusal != null`); *false refusal* on answerable sets.
  - *Unsupported-claim rate:* an independent judge (prompt and model different from the M6 verifier) labels each claim against its cited evidence.
  - *Retrieval:* FinanceBench evidence-page recall@k (k = 5, 10, 20); narrative gold-chunk recall@k.
  - *Latency* p50/p95 per route and *cost* per question (from `Usage`).
- **Statistics:** 95% bootstrap CIs (10,000 resamples) per metric, and a **paired bootstrap** for system-vs-system differences (that's what backs "+15 points").
- Output: `eval/results/<utc>_<system>_<config>.json` (`05_data_contracts.md` §6). `eval/report.py` regenerates the tables in `docs/results.md` between `<!-- AUTO:START -->` / `<!-- AUTO:END -->` markers. **Hand-typed numbers in results are not allowed.**

### 3.8 Judge calibration (1 h; **you label**)
Run the baseline on FinanceBench dev. You label 30 answers correct/incorrect blind to the judge, in `eval/judges/calibration.jsonl` (protected). Compute Cohen's κ between judge and you. Target κ ≥ 0.7 [Guessing as a reasonable bar]. If it's lower, read the disagreements, refine the judge rubric and re-measure. **Report κ next to every FinanceBench number.**

### 3.9 Baseline (1 h)
`app/engine/baseline.py`: embed the question → dense top-8 over all chunks (no filters) → Sonnet 5.5 composer with the same `Answer` schema → no verifier. Run on all sets (`--mode batch` for the test split). Optional: a **long-context baseline** on 30 FinanceBench questions, putting the whole source document into Sonnet 5.5. It costs a few dollars [Guessing] and answers the "why not just paste the 10-K?" question with data.

## 4. Claude Code session plan

```
/milestone M4
Step 1: app/llm/client.py per M04 §3.1, with modes live/stub/record/replay/batch and prices.py.
Unit tests: cost math, retry behavior (mocked), replay determinism, stub default in tests.
Plan first. Check the structured-outputs request shape against the current docs and tell me which
models the docs list as supported.
```
```
Step 2: eval/gen/make_numeric_set.py per §3.3 (templates, stratified sampling, SQL gold, paraphrase
via the client in record mode, second-source check via companyfacts). Print the stratification table.
```
```
Step 3: narrative drafting script per §3.4 (writes eval/sets/drafts/narrative_drafts.jsonl, NOT the
gold file). I'll curate the gold file myself.
```
```
Step 4: eval/run.py + report.py skeleton per §3.7 with bootstrap and paired bootstrap utilities.
Leave eval/metrics.py functions as stubs with docstrings. I'll implement them. Write tests for
the bootstrap utilities with known distributions.
```
```
Step 5: judge prompts (eval/judges/) + calibration script computing Cohen's kappa + a disagreement
report. Then the baseline per §3.9.
```

## 5. You write

`eval/metrics.py`; the curated `narrative.jsonl` and `unanswerable.jsonl`; `calibration.jsonl` labels; the 30 paraphrase checks; the 10 manual 10-K checks.

## 6. Definition of done

- [ ] All four sets plus splits committed (gold files protected by the hook); `eval/sets/CHECKSUMS` committed and `make check-gold` green in CI.
- [ ] Numeric gold second-source agreement reported (30 automated + 10 manual).
- [ ] Judge κ reported (target ≥ 0.7, or refined with the disagreements documented).
- [ ] `make bench SYSTEM=baseline SPLIT=all MODE=batch` produces a results JSON; `docs/results.md` tables auto-generated with CIs.
- [ ] Cost of the baseline run recorded from accounting.

## 7. Pitfalls

- Letting the baseline output free text while the engine outputs JSON, which makes scoring unfair. Use one schema.
- Tuning on test. Use the dev split only.
- A judge that sees the system's citations but not the gold: make it compare against gold for correctness and against evidence for support. These are separate prompts.
- Numeric questions whose metric isn't in the catalog yet. Generate only from catalog metrics, and add metrics before generating more questions.
- Paraphrases that change the period ("last year") or the metric ("profit" vs "operating income"). That's why you check 30.

## 8. Interview prep

- How you know +15 points isn't noise (paired bootstrap, n, CI).
- Why κ, what your κ was, and what the judge got wrong.
- What the numeric set measures, honestly (resolution and mapping, not ground truth), and how you de-circularized it.
- Why build eval before the engine.
