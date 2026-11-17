# M7: Learned query router (the ML component)

**Time:** about 10 h (fast track 6 h: logistic regression on frozen embeddings only) · **Roles:** AI/ML engineer (training, evaluation, serving), SDE (latency and cost engineering) · **Depends on:** M5, M6 (and the start of `router_test.jsonl` from M5)

## 1. Goal and why it's real ML (not decoration)

The route decides **which pipeline runs and which model is paid for**:

| Route | Without M7 (planner decides everything) | With M7 |
|---|---|---|
| out_of_scope | planner call + templated refusal | **0 LLM calls**: templated refusal |
| narrative | planner (Haiku) → retrieval → composer (Sonnet) | **no planner call**: rule-based entities + query → retrieval → Sonnet |
| numeric | planner → compiler **+ retrieval** → Sonnet composer | planner → compiler → **Haiku composer, no retrieval/rerank** |
| hybrid | planner → both → Sonnet | same |

So the classifier changes cost and latency, and a wrong route costs accuracy. Both are measurable end to end. **T12:** % of traffic handled without an LLM routing decision, and cost/latency per question, at end-to-end accuracy no worse than the LLM-planner routing (paired bootstrap).

**A design point worth stating in interviews:** the classifier's input is the **bge-small query embedding you already compute for dense retrieval**, so routing adds roughly a dot product, not another model call [Likely; measure it].

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| Templates and generators from M4 (new seeds, never test items) | `ml/router/data.py` → `data/router/{train,val}.parquet` with `source` and `group` columns |
| Haiku paraphrases; Sonnet teacher labels (Batch API) | Teacher-label noise estimate (you check 100) |
| `eval/sets/router_test.jsonl` (~250 real questions, **hand-labeled by you**) | `ml/router/train.py`, `evaluate.py`, `export_onnx.py`; MLflow runs in `ml/router/mlruns/` |
| — | `artifacts/router/v{n}/{model.onnx, calibration.json, labels.json, card.md}` (also in S3) |
| — | `app/engine/router/{classifier.py, cascade.py}` (**you**) + an engine flag `--router onnx+llm@τ` |
| — | Report: per-class P/R/F1, confusion matrix, ECE before/after calibration, τ sweep, end-to-end cost/latency/accuracy table |

## 3. Steps

### 3.1 Labels and data (3 h)
- **Classes:** `numeric`, `narrative`, `hybrid`, `out_of_scope`. Write one-paragraph definitions with 3 borderline examples each in `ml/router/LABELING.md`. You'll be asked about borderlines.
- **Test set first (you):** label about 250 real questions: all 150 FinanceBench, the 50 narrative, 50 unanswerable/out-of-scope, and (later) user-log questions. Label blind to model output. Note your own uncertain cases.
- **Training sources:**
  1. Templates (known labels): numeric (M4 generator, new seed), narrative (`"What did {company} say about {topic} in its {year} 10-K?"` × a topic list of about 60), hybrid (numeric + "why/what drove"), out_of_scope (price, advice, forecasts, private companies, unrelated).
  2. Paraphrases ×3 with Haiku 5.5 (label-preserving).
  3. About 1,500 free-form "realistic analyst/student questions" drafted by Sonnet 5.5 and **labeled by a Sonnet teacher via the Batch API**. You audit 100 teacher labels and report the estimated label noise.
- **Leakage controls:** group-split train/val by template ID / source document; drop any training item whose cosine similarity to any test item is > 0.92; report how many were dropped.

### 3.2 Models (3 h; **you write train/evaluate**, Claude scaffolds)
Train and compare on val, then evaluate once on test:
1. Majority class (floor).
2. TF-IDF (word 1–2-grams + char 3–5-grams) + logistic regression.
3. **Frozen bge-small query embeddings + logistic regression** (multinomial, class-weighted). This is the expected winner on the cost/benefit trade-off [Guessing].
4. *(Optional)* a small MLP on embeddings.
5. *(Optional, full track)* a fine-tuned small encoder on your Mac (MPS) or as a **SageMaker training job** (adds a cloud-ML keyword; new-customer credits reportedly apply to SageMaker training [Unclear: third-party report]).
- Track every run in **MLflow**: params, data hash, metrics, confusion matrix PNG and model artifact.
- **Calibration:** temperature scaling or Platt (`CalibratedClassifierCV`) on val; report **ECE** before and after.

### 3.3 Cascade and threshold (2 h; **you**)
- `cascade.route(embedding, question) -> Route(label, source, confidence)`: if `max_p ≥ τ`, use the classifier; else call the LLM planner as before.
- **Sweep τ** on dev end to end (not just classifier accuracy): % handled locally, accuracy per set, cost/question, p95 latency. Choose τ at the knee where accuracy ≥ the LLM-routing baseline and local share is maximized. Write ADR-011.
- Narrative-route entity extraction without the planner: `resolve_company` over n-grams + a regex for years/quarters. If that fails, fall back to the planner.

### 3.4 Export and serving (1 h)
- Export with skl2onnx (or optimum for an encoder) → `model.onnx`. Load once at API start with onnxruntime (CPU, 1 thread). **Equivalence test:** ONNX vs sklearn predictions on 1,000 samples (max abs diff < 1e-5).
- Model card `card.md`: data sources and sizes, label definitions, metrics, known failure modes, τ, and when to retrain.
- Log `route`, `route_source` and `route_conf` in `usage_log` (M8). In M11, label user questions, add them to the test set (new version) and re-check.

### 3.5 End-to-end evaluation (1 h)
Run test with `--router planner` vs `--router onnx+llm@τ`. Report the accuracy deltas (paired bootstrap), cost/question, p50/p95 latency and % local. These are T12.

## 4. Claude Code session plan

```
/milestone M7
Step 1: ml/router/data.py per M07 §3.1: template generation (reuse eval/gen with new seeds),
paraphrase via the LLM client (record mode), the teacher-labeling batch job, leakage controls
(group split + similarity dedup against eval/sets/router_test.jsonl). Print class counts per source.
```
```
Step 2: scaffold ml/router/train.py and evaluate.py with MLflow logging (params, data hash, metrics,
confusion matrix artifact). They call fit_model() and evaluate_model() from ml/router/model_fns.py:
create that file as a stub once; it's mine after that. Write tests for the data pipeline and the
ONNX equivalence check.
```
```
Step 3: the τ-sweep runner over the M4 harness (end-to-end on dev) and the report tables.
Then export_onnx.py and app/engine/router/classifier.py loading the ONNX model; cascade.py is mine.
```

## 5. You write

Labels (`router_test.jsonl`, 100 teacher-label audits), `LABELING.md`, `ml/router/model_fns.py` (`fit_model`/`evaluate_model`), `app/engine/router/cascade.py`, the model card, ADR-011.

## 6. Definition of done

- [ ] Test macro-F1 for every model (+ majority floor); per-class P/R/F1 and confusion matrix for the chosen one.
- [ ] ECE before/after calibration.
- [ ] τ chosen from the end-to-end sweep; T12 reported with CIs.
- [ ] ONNX equivalence test green; p99 routing latency on CPU reported.
- [ ] MLflow runs reproducible from `make router-train SEED=…`.

## 7. Pitfalls

- Evaluating on synthetic paraphrases: inflated scores. Test is real questions only.
- Template leakage (the same template in train and val) inflates val. Use group splits.
- Optimizing classifier accuracy instead of end-to-end outcomes (a hybrid→narrative error may cost nothing; numeric→narrative costs a lot). Look at error **cost**, not just counts.
- Forgetting that the planner is still needed for numeric routes. The savings come from route-dependent pipelines and model tiers, so say that precisely.

## 8. Interview prep

- Data provenance, label noise and leakage controls.
- Why macro-F1; the confusion pattern; which errors are expensive.
- Calibration and how τ was chosen (end to end, not on F1).
- Why logistic regression on frozen embeddings beat (or lost to) a fine-tuned encoder, with numbers.
- How you'd monitor drift (route confidence distribution in `usage_log`, periodic relabeling of user questions).
