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
| `2026-11-25T05-46-32Z_baseline-0.2.0_sonnet` | all | narrative | 50 | 50 | 0 sys / 0 judge | gold-point coverage | 40.5% | [28.0, 53.5] | uncalibrated |
| `2026-11-25T05-46-32Z_baseline-0.2.0_sonnet` | all | narrative | 50 | 50 | 0 sys / 0 judge | contradiction rate | 0.0% | [0.0, 0.0] | uncalibrated |
| `2026-11-25T05-46-32Z_baseline-0.2.0_sonnet` | all | narrative | 50 | 50 | 0 sys / 0 judge | recall@5 (eval/metrics.py) | 36.0% | [24.0, 50.0] | - |
| `2026-11-25T05-46-32Z_baseline-0.2.0_sonnet` | all | narrative | 50 | 50 | 0 sys / 0 judge | recall@10 (eval/metrics.py) | 40.0% | [26.0, 54.0] | - |
| `2026-11-25T05-46-32Z_baseline-0.2.0_sonnet` | all | narrative | 50 | 50 | 0 sys / 0 judge | recall@20 (eval/metrics.py) | 40.0% | [26.0, 54.0] | - |
| `2026-11-25T05-46-32Z_baseline-0.2.0_sonnet` | all | unanswerable | 50 | 50 | 0 sys / 0 judge | refusal rate | 94.0% | [86.0, 100.0] | - |
| `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | dev | numeric | 72 | 72 | 0 sys / 0 judge | accuracy | 2.8% | [0.0, 6.9] | - |
| `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | dev | financebench | 45 | 45 | 0 sys / 0 judge | accuracy | 24.4% | [13.3, 37.8] | kappa 0.65 (answered rows only) |
| `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | dev | financebench | 45 | 45 | 0 sys / 0 judge | refusal rate | 68.9% | [55.6, 82.2] | - |
| `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | dev | financebench | 45 | 45 | 0 sys / 0 judge | recall@5 (eval/metrics.py) | 15.6% | [4.4, 26.7] | - |
| `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | dev | financebench | 45 | 45 | 0 sys / 0 judge | recall@10 (eval/metrics.py) | 20.0% | [8.9, 33.3] | - |
| `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | dev | financebench | 45 | 45 | 0 sys / 0 judge | recall@20 (eval/metrics.py) | 20.0% | [8.9, 33.3] | - |
| `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet` | test | numeric | 168 | 168 | 0 sys / 0 judge | accuracy | 5.4% | [2.4, 8.9] | - |
| `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet` | test | financebench | 105 | 105 | 0 sys / 0 judge | accuracy | 23.8% | [16.2, 32.4] | kappa 0.65 (answered rows only) |
| `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet` | test | financebench | 105 | 105 | 0 sys / 0 judge | refusal rate | 69.5% | [61.0, 78.1] | - |
| `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet` | test | financebench | 105 | 105 | 0 sys / 0 judge | recall@5 (eval/metrics.py) | 17.1% | [10.5, 24.8] | - |
| `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet` | test | financebench | 105 | 105 | 0 sys / 0 judge | recall@10 (eval/metrics.py) | 21.0% | [13.3, 28.6] | - |
| `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet` | test | financebench | 105 | 105 | 0 sys / 0 judge | recall@20 (eval/metrics.py) | 21.0% | [13.3, 28.6] | - |

### Judge calibration (FinanceBench correctness judge vs human labels)

| run | kappa | n labeled (answered rows) | vs 0.70 target | rubric | judge model | labels sha256 | source |
|---|---:|---:|---|---|---|---|---|
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | 0.646 | 46 | below target | correctness-v2 | claude-sonnet-5-5 | `494b5d395281` | eval/judges/kappa.json |
| `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | 0.646 | 46 | below target | correctness-v2 | claude-sonnet-5-5 | `494b5d395281` | eval/judges/kappa.json; labels measured on run 2026-11-25T00-43-10Z_baseline-0.2.0_sonnet, not on this run's rows (same judge and rubric; answer identity not checked by report.py) |
| `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet` | 0.646 | 46 | below target | correctness-v2 | claude-sonnet-5-5 | `494b5d395281` | eval/judges/kappa.json; labels measured on run 2026-11-25T00-43-10Z_baseline-0.2.0_sonnet, not on this run's rows (same judge and rubric; answer identity not checked by report.py) |

### Cost and latency

| run | mode | composer | questions | total $ | $ / question | p50 ms | p95 ms | ef_search |
|---|---|---|---:|---:|---:|---:|---:|---:|
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | batch | claude-sonnet-5-5 | 390 | 2.2862 | 0.00586 | NOT MEASURED | NOT MEASURED | 800 |
| `2026-11-25T05-46-32Z_baseline-0.2.0_sonnet` | batch | claude-sonnet-5-5 | 100 | 0.0009 | 0.00001 | NOT MEASURED | NOT MEASURED | 800 |
| `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | replay | claude-sonnet-5-5 | 117 | 0.0000 | 0.00000 | NOT MEASURED | NOT MEASURED | 800 |
| `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet` | replay | claude-sonnet-5-5 | 273 | 0.0000 | 0.00000 | NOT MEASURED | NOT MEASURED | 800 |

### False refusal on answerable sets

| run | false-refusal rate |
|---|---:|
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | 83.6% |
| `2026-11-25T05-46-32Z_baseline-0.2.0_sonnet` | 52.0% |
| `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | 83.8% |
| `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet` | 83.5% |

Source: `eval/results/<run_id>.json`. Claims / unsupported-claim rate: NOT MEASURED until the support judge has chunk text to read (M6).
<!-- AUTO:END -->

## Reading the baseline numbers (hand-written; the numbers above are generated)

- **Splits.** `eval/sets/splits.json` (seed 13, 30% dev) now exists. The `00-43` run and the narrative/unanswerable run use `--split all`; the two `replay` rows are the dev (117 questions) and test (273) halves of numeric + FinanceBench. Dev is the number to iterate on; test was run once, at M4 close.
- **Judge calibration is below target, and the labels are not fully blind.** Cohen's kappa is 0.646 on n = 46 (`eval/judges/kappa.json`), under the 0.70 target; it is reported, not tuned. The five disagreements are listed in `eval/judges/drafts/calibration_disagreements.md`. `eval/judges/drafts/calibration_labeled.ai-seed.jsonl` holds an AI-prepared first pass of the same 46 rows; `eval/judges/calibration.jsonl` differs from it on 3 of 46 labels. Treat kappa as "judge vs human labels that started from an AI draft", not as an independent human estimate. How the seed was produced is not recorded in the repo.
- **Kappa covers only the answered FinanceBench rows.** The 46 labeled rows are the answered ones. The other 104 of the 150 are refusals, scored incorrect deterministically with no judge. So the 24.0% accuracy rests on the judge only for its 46 answered rows (36 judged correct), and kappa 0.65 says nothing about the refusals.
- **recall@10 and recall@20 are the same number (20.7%).** The baseline retrieves `top_k` = 8 chunks (`config.top_k` in the results file), so neither can see past rank 8 and they are not separate measurements. recall@8 is NOT MEASURED (only @5, @10 and @20 are computed).
- **False refusal is pooled.** The 83.6% covers the numeric (240) and FinanceBench (150) rows together. Numeric accuracy is low mostly because the baseline refuses: it has retrieval over chunks and no access to XBRL facts.
- **Corpus version.** The `00-43` run predates the harness stamping `corpus_version` (key `corpus_version` is null in its file): NOT MEASURED for it. The three later runs carry `corpus_version` = 1, read from `corpus_state` before and after the run.
- **The replay reproduces the recorded baseline.** Dev + test replay (from the banked cassettes, retrieval re-run against the serving DB at `ef_search` 800) was compared with the `00-43` run for all 390 ids: `correct`, `refused`, `retrieved_top20` and `answer_markdown` are identical for every one. Pooled, the two replays give 11/240 numeric and 36/150 FinanceBench, the same counts as the all-rows run. The comparison was an ad-hoc script run in the session, not a committed check.
- **Narrative (50) and unanswerable (50).** Run `05-46-32` (`sets.narrative`, `sets.unanswerable` in its results file). Gold-point coverage 40.5% and contradiction rate 0.0% are from the narrative judge, which is **uncalibrated** (no human labels; kappa exists only for the FinanceBench correctness judge). A contradiction rate of 0.0% with a CI of [0, 0] means the judge flagged none; it does not show the answers are free of contradictions. Unanswerable refusal rate is the deterministic `refusal is not None` test: 94.0% [86, 100]. The narrative false-refusal rate is 52.0% (`answerable_false_refusal`), so about half of the answerable narrative questions are refused; coverage counts those rows as 0.
- **Narrative recall@10 = recall@20 (40.0%)** for the same reason as FinanceBench: `top_k` = 8. The narrative recall rows are hit@k on `gold_chunk_ids` (one or two chunks per question).
- **This run needed a rerun.** The first narrative/unanswerable attempt (`eval/results/superseded/2026-11-25T05-43-05Z_baseline-0.2.0_sonnet.json`, $0.8084, kept for the record) lost one row, `nar-001`, to a dropped database connection (the SSM tunnel idled out), which scored it as a system failure. Rerunning from the banked cassettes submitted only the missing judge request; that file (`05-46-32`) has 0 errors and cost $0.0009 by itself, so its row in the cost table understates the run. The narrative/unanswerable batch cost $0.8093 in total ($0.8084 + $0.0009, `cost.total_usd` of the two files).
- **Box time.** Start call 2026-11-25T04:58:19Z, stop call 05:50:11Z: 51 min 52 s of wall time. Box dollars are NOT MEASURED.
