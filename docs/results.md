# Results

Every number on this page comes from `eval/results/*.json` and is written by `eval/report.py`
between the AUTO markers. Nothing inside the markers is typed by hand. Each metric shows n and a
95% bootstrap CI (10,000 resamples). System-vs-system differences, when reported, use a
paired bootstrap.

**Tuning discipline:** systems are tuned on the **dev** split only; **test** is run at
milestones. FinanceBench accuracy is judged by an LLM, and the judge's agreement with human
labels (Cohen's kappa, `eval/judges/`) is printed next to it. The narrative judge is
uncalibrated and labelled as such.

<!-- AUTO:START -->
### Accuracy by set

| run | split | set | n | n scored | errors | metric | value | 95% CI | judge |
|---|---|---|---:|---:|---:|---|---:|---|---|
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | all | numeric | 240 | 240 | 0 sys / 0 judge | accuracy | 4.6% | [2.1, 7.5] | - |
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | all | financebench | 150 | 150 | 0 sys / 0 judge | accuracy | 24.0% | [17.3, 30.7] | kappa 0.65 (answered rows only) |
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | all | financebench | 150 | 150 | 0 sys / 0 judge | refusal rate | 69.3% | [62.0, 76.7] | - |
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | all | financebench | 150 | 150 | 0 sys / 0 judge | recall@5 (eval/metrics.py) | 16.7% | [10.7, 22.7] | - |
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | all | financebench | 150 | 150 | 0 sys / 0 judge | recall@10 (eval/metrics.py) | 20.7% | [14.7, 27.3] | - |
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | all | financebench | 150 | 150 | 0 sys / 0 judge | recall@20 (eval/metrics.py) | 20.7% | [14.7, 27.3] | - |

### Judge calibration (FinanceBench correctness judge vs human labels)

| run | kappa | n labeled (answered rows) | vs 0.70 target | rubric | judge model | labels sha256 | source |
|---|---:|---:|---|---|---|---|---|
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | 0.646 | 46 | below target | correctness-v2 | claude-sonnet-5-5 | `494b5d395281` | eval/judges/kappa.json |

### Cost and latency

| run | mode | composer | questions | total $ | $ / question | p50 ms | p95 ms | ef_search |
|---|---|---|---:|---:|---:|---:|---:|---:|
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | batch | claude-sonnet-5-5 | 390 | 2.2862 | 0.00586 | NOT MEASURED | NOT MEASURED | 800 |

### False refusal on answerable sets

| run | false-refusal rate |
|---|---:|
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | 83.6% |

Source: `eval/results/<run_id>.json`. Claims / unsupported-claim rate: NOT MEASURED until the support judge has chunk text to read (M6).
<!-- AUTO:END -->

## Reading the baseline numbers (hand-written; the numbers above are generated)

- **All rows, no splits yet.** The baseline run used `--split all`; `eval/sets/splits.json` does not exist, so nothing here is a dev or test number.
- **Judge calibration is below target, and the labels are not fully blind.** Cohen's kappa is 0.646 on n = 46 (`eval/judges/kappa.json`), under the 0.70 target; it is reported, not tuned. The five disagreements are listed in `eval/judges/drafts/calibration_disagreements.md`. `eval/judges/drafts/calibration_labeled.ai-seed.jsonl` holds an AI-prepared first pass of the same 46 rows; `eval/judges/calibration.jsonl` differs from it on 3 of 46 labels. Treat kappa as "judge vs human labels that started from an AI draft", not as an independent human estimate. How the seed was produced is not recorded in the repo.
- **Kappa covers only the answered FinanceBench rows.** The 46 labeled rows are the answered ones. The other 104 of the 150 are refusals, scored incorrect deterministically with no judge. So the 24.0% accuracy rests on the judge only for its 46 answered rows (36 judged correct), and kappa 0.65 says nothing about the refusals.
- **recall@10 and recall@20 are the same number (20.7%).** The baseline retrieves `top_k` = 8 chunks (`config.top_k` in the results file), so neither can see past rank 8 and they are not separate measurements. recall@8 is NOT MEASURED (only @5, @10 and @20 are computed).
- **False refusal is pooled.** The 83.6% covers the numeric (240) and FinanceBench (150) rows together. Numeric accuracy is low mostly because the baseline refuses: it has retrieval over chunks and no access to XBRL facts.
- **Corpus version.** This run predates the harness stamping `corpus_version`, so it is NOT MEASURED for this run.
- **Narrative and unanswerable are not in this run.** Their sets are still drafts; those rows and the narrative judge are NOT MEASURED.
