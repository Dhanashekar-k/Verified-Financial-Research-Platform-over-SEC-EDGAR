# S1 (stretch): Anomaly detection on XBRL facts with Spark MLlib

**Time:** about 14 h · **Roles:** ML + big data (distributed training and scoring over all facts), AI (it makes the verifier smarter) · **Depends on:** M1, M6. Do it only if you're on schedule after M11 has started.

## 1. Goal and why

The verifier treats XBRL as ground truth, but filers make errors: scale mistakes (thousands vs units), sign flips, wrong tags, period mix-ups [Likely]. S1 scores **every fact** for the likelihood of a data error. The verifier then shows high-score facts as `flagged` instead of `verified`. This is big-data ML with a product reason, evaluated honestly without ground-truth labels.

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| `curated.facts`, `curated.facts_history`, `curated.companies` (SIC) | `pipelines/spark/ml/{features.py, inject.py, train_gbt.py, score.py}` |
| — | Feature table `curated.fact_features`; model in `s3://…/artifacts/anomaly/v1/` (MLlib pipeline) |
| — | `anomaly_score` filled in `curated.facts` → export → Postgres → verifier policy |
| — | Report: PR-AUC and recall per corruption type (held-out companies), real-restatement recall, manual precision@50, the rules-only baseline, EMR cost |

## 3. Steps

### 3.1 Features (Spark window functions; 4 h)
Per series `(cik, tag, uom, qtrs)` ordered by `period_end`:
- `log10(|v|+1)`, sign, trailing-zero count, `v mod 1000 == 0`.
- Log-ratio to the previous and next period, and to the rolling median of the last 4 same-length periods.
- **Peer robust z:** |log v − median_peer| / MAD_peer, where peer = same 2-digit SIC, same tag, same fiscal year.
- **Accounting identity residuals** at company-period level, joined back to every fact involved:
  - `Assets − LiabilitiesAndStockholdersEquity`
  - `(Q1+Q2+Q3+Q4) − FY` for flow concepts (when all are reported)
  - `GrossProfit − (Revenue − CostOfRevenue)`
- `is_restated`, `n_versions`, and companyfacts disagreement (where available).

### 3.2 Labels by synthetic injection (2 h)
`inject.py`: from series with ≥ 8 periods, corrupt about 2% of facts, **recomputing features with the corrupted value in place**. Corruption types: ×1000, ÷1000, ×10^6, sign flip, ×10 (decimal shift), adjacent-period swap, related-tag swap (for example revenue ↔ gross profit). Label corrupted = 1, untouched = 0.
**Split by CIK** (group split): models must generalize to unseen companies.

### 3.3 Models (3 h)
1. **Rules-only baseline:** thresholds on the robust z and the identity residuals (tuned on validation).
2. **MLlib `GBTClassifier`** (`VectorAssembler` → GBT, depth 5, about 100 iterations, class weighting via a weight column). Distributed training on EMR Serverless.
3. *(Optional)* sklearn IsolationForest on a sample, as an unsupervised comparison.

### 3.4 Evaluation without real ground truth (3 h; **you**)
- **Injected test (held-out CIKs):** PR-AUC overall and per corruption type; recall at precision 0.9.
- **Natural labels:** restatement pairs where the later value differs by about ×1000 or a sign flip (likely corrected errors). What recall does the model achieve on the *original* values?
- **Manual precision@50:** you inspect the top 50 flagged real facts against the filing and label error / legitimate outlier / unclear.
- Compare with the rules baseline. **If the GBT doesn't beat the rules, say so and ship the rules.** That's a legitimate, defensible outcome.

### 3.5 Scoring and integration (2 h)
- `score.py` on EMR: score all facts → `anomaly_score` → MERGE into `curated.facts` → the next export carries it.
- Choose the threshold at precision ≥ 0.9 on validation. The verifier marks those facts `flagged` (yellow badge with "possible data error in source filing"), and the answer cites the filing so the user can check.
- Record EMR cost and runtime (more big-data numbers for the resume).

## 4. Claude Code session plan
```
/milestone S1
Step 1: features.py with Spark window functions per S1 §3.1; unit tests on a tiny DataFrame with
hand-computed expected features. Step 2: inject.py with a group split by CIK. Step 3: train_gbt.py
(MLlib pipeline, weight column, model save to S3) and a rules baseline. evaluate functions are mine.
```

## 5. Definition of done
- [ ] PR-AUC per corruption type on held-out companies; GBT vs rules table.
- [ ] Natural-label recall and manual precision@50 reported.
- [ ] `anomaly_score` live in Postgres; the verifier shows `flagged`; 3 example flagged facts documented.
- [ ] EMR cost and runtime recorded.

## 6. Interview prep
- How you evaluated anomaly detection without labels (three complementary estimates).
- Why group-split by company.
- Why the identity residuals are the strongest features (if they are), and what that says about rules vs ML.
