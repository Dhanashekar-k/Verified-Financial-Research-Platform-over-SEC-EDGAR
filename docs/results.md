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
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | numeric | 72 | 72 | 0 sys / 0 judge | accuracy | 100.0% | [100.0, 100.0] | - |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | financebench | 45 | 45 | 0 sys / 0 judge | accuracy | 13.3% | [4.4, 24.4] | kappa 0.65 (answered rows only) |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | financebench | 45 | 45 | 0 sys / 0 judge | refusal rate | 84.4% | [73.3, 93.3] | - |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | financebench | 45 | 45 | 0 sys / 0 judge | recall@5 (eval/metrics.py) | 17.8% | [6.7, 28.9] | - |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | financebench | 45 | 45 | 0 sys / 0 judge | recall@10 (eval/metrics.py) | 17.8% | [6.7, 28.9] | - |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | financebench | 45 | 45 | 0 sys / 0 judge | recall@20 (eval/metrics.py) | 17.8% | [6.7, 28.9] | - |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | narrative | 15 | 15 | 0 sys / 0 judge | gold-point coverage | 50.0% | [28.3, 71.7] | uncalibrated |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | narrative | 15 | 15 | 0 sys / 0 judge | contradiction rate | 0.0% | [0.0, 0.0] | uncalibrated |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | narrative | 15 | 15 | 0 sys / 0 judge | recall@5 (eval/metrics.py) | 60.0% | [33.3, 86.7] | - |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | narrative | 15 | 15 | 0 sys / 0 judge | recall@10 (eval/metrics.py) | 66.7% | [40.0, 86.7] | - |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | narrative | 15 | 15 | 0 sys / 0 judge | recall@20 (eval/metrics.py) | 66.7% | [40.0, 86.7] | - |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | unanswerable | 15 | 15 | 0 sys / 0 judge | refusal rate | 100.0% | [100.0, 100.0] | - |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | numeric | 72 | 72 | 0 sys / 0 judge | accuracy | 100.0% | [100.0, 100.0] | - |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | financebench | 45 | 45 | 0 sys / 0 judge | accuracy | 11.1% | [2.2, 20.0] | kappa 0.65 (answered rows only) |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | financebench | 45 | 45 | 0 sys / 0 judge | refusal rate | 86.7% | [75.6, 95.6] | - |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | financebench | 45 | 45 | 0 sys / 0 judge | recall@5 (eval/metrics.py) | 17.8% | [6.7, 28.9] | - |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | financebench | 45 | 45 | 0 sys / 0 judge | recall@10 (eval/metrics.py) | 17.8% | [6.7, 28.9] | - |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | financebench | 45 | 45 | 0 sys / 0 judge | recall@20 (eval/metrics.py) | 17.8% | [6.7, 28.9] | - |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | narrative | 15 | 15 | 0 sys / 0 judge | gold-point coverage | 61.7% | [40.0, 81.7] | uncalibrated |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | narrative | 15 | 15 | 0 sys / 0 judge | contradiction rate | 0.0% | [0.0, 0.0] | uncalibrated |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | narrative | 15 | 15 | 0 sys / 0 judge | recall@5 (eval/metrics.py) | 60.0% | [33.3, 86.7] | - |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | narrative | 15 | 15 | 0 sys / 0 judge | recall@10 (eval/metrics.py) | 66.7% | [40.0, 86.7] | - |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | narrative | 15 | 15 | 0 sys / 0 judge | recall@20 (eval/metrics.py) | 66.7% | [40.0, 86.7] | - |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | unanswerable | 15 | 15 | 0 sys / 0 judge | refusal rate | 100.0% | [100.0, 100.0] | - |

### Judge calibration (FinanceBench correctness judge vs human labels)

| run | kappa | n labeled (answered rows) | vs 0.70 target | rubric | judge model | labels sha256 | source |
|---|---:|---:|---|---|---|---|---|
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | 0.646 | 46 | below target | correctness-v2 | claude-sonnet-5-5 | `494b5d395281` | eval/judges/kappa.json |
| `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | 0.646 | 46 | below target | correctness-v2 | claude-sonnet-5-5 | `494b5d395281` | eval/judges/kappa.json; labels measured on run 2026-11-25T00-43-10Z_baseline-0.2.0_sonnet, not on this run's rows (same judge and rubric; answer identity not checked by report.py) |
| `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet` | 0.646 | 46 | below target | correctness-v2 | claude-sonnet-5-5 | `494b5d395281` | eval/judges/kappa.json; labels measured on run 2026-11-25T00-43-10Z_baseline-0.2.0_sonnet, not on this run's rows (same judge and rubric; answer identity not checked by report.py) |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | 0.646 | 46 | below target | correctness-v2 | claude-sonnet-5-5 | `494b5d395281` | eval/judges/kappa.json; labels measured on run 2026-11-25T00-43-10Z_baseline-0.2.0_sonnet, not on this run's rows (same judge and rubric; answer identity not checked by report.py) |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | 0.646 | 46 | below target | correctness-v2 | claude-sonnet-5-5 | `494b5d395281` | eval/judges/kappa.json; labels measured on run 2026-11-25T00-43-10Z_baseline-0.2.0_sonnet, not on this run's rows (same judge and rubric; answer identity not checked by report.py) |

### Cost and latency

| run | mode | composer | questions | total $ | $ / question | p50 ms | p95 ms | ef_search |
|---|---|---|---:|---:|---:|---:|---:|---:|
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | batch | claude-sonnet-5-5 | 390 | 2.2862 | 0.00586 | NOT MEASURED | NOT MEASURED | 800 |
| `2026-11-25T05-46-32Z_baseline-0.2.0_sonnet` | batch | claude-sonnet-5-5 | 100 | 0.0009 | 0.00001 | NOT MEASURED | NOT MEASURED | 800 |
| `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | replay | claude-sonnet-5-5 | 117 | 0.0000 | 0.00000 | NOT MEASURED | NOT MEASURED | 800 |
| `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet` | replay | claude-sonnet-5-5 | 273 | 0.0000 | 0.00000 | NOT MEASURED | NOT MEASURED | 800 |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | batch | claude-haiku-5-5 | 147 | 0.0718 | 0.00049 | NOT MEASURED | NOT MEASURED | 800 |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | batch | claude-sonnet-5-5 | 147 | 0.6951 | 0.00473 | NOT MEASURED | NOT MEASURED | 800 |

### False refusal on answerable sets

| run | false-refusal rate |
|---|---:|
| `2026-11-25T00-43-10Z_baseline-0.2.0_sonnet` | 83.6% |
| `2026-11-25T05-46-32Z_baseline-0.2.0_sonnet` | 52.0% |
| `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | 83.8% |
| `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet` | 83.5% |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | 33.3% |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | 32.6% |

Source: `eval/results/<run_id>.json`. Claims / unsupported-claim rate: NOT MEASURED until the support judge has chunk text to read (M6).

### Engine vs baseline (paired bootstrap)

| engine run | baseline run | split | set | paired n | engine | baseline | difference | 95% CI | p | reading |
|---|---|---|---|---:|---:|---:|---:|---|---:|---|
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | dev | numeric | 72 | 100.0% | 2.8% | +97.2 pp | [93.1, 100.0] | 0.0002 | engine higher |
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | dev | financebench | 45 | 13.3% | 24.4% | -11.1 pp | [-24.4, 2.2] | 0.1546 | no difference shown |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | dev | numeric | 72 | 100.0% | 2.8% | +97.2 pp | [93.1, 100.0] | 0.0002 | engine higher |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet` | dev | financebench | 45 | 11.1% | 24.4% | -13.3 pp | [-28.9, 0.0] | 0.0954 | no difference shown |

The difference is engine minus baseline on the questions both runs scored; the CI is a paired bootstrap (10,000 resamples) and `reading` only says whether it excludes 0.

### Composer Pareto (accuracy, cost, latency per composer)

| run | split | composer | numeric accuracy | FinanceBench accuracy | narrative coverage | system $ / question (no judge) | p95 ms |
|---|---|---|---:|---:|---:|---:|---:|
| `2026-11-26T00-27-09Z_engine-0.6.1_haiku` | dev | claude-haiku-5-5 | 100.0% | 13.3% | 50.0% | 0.00033 | 14100 (dev live sample, n=32) |
| `2026-11-26T00-51-47Z_engine-0.6.1_sonnet` | dev | claude-sonnet-5-5 | 100.0% | 11.1% | 61.7% | 0.00457 | 15350 (dev live sample, n=32) |

Latency sample `2026-11-26T00-59-58Z_engine-0.6.1_haiku` (engine, claude-haiku-5-5, dev split, sets numeric, financebench, narrative, unanswerable, n=32 answered, refusals included): p50 5736 ms, p95 14100 ms. Live calls from a laptop that runs the whole engine (embedding and reranking included) against the box's database, so it includes the laptop-to-box network delay and is not a server-side number.

Latency sample `2026-11-26T00-56-19Z_engine-0.6.1_sonnet` (engine, claude-sonnet-5-5, dev split, sets numeric, financebench, narrative, unanswerable, n=32 answered, refusals included): p50 5905 ms, p95 15350 ms. Live calls from a laptop that runs the whole engine (embedding and reranking included) against the box's database, so it includes the laptop-to-box network delay and is not a server-side number.

The cost column is the system's own spend (judges excluded) on calls the run paid for: planner replies already recorded are served from cassettes at no cost.
The planner cost $0.00008 per question in `2026-11-25T11-00-52Z_intent_planner-claude-haiku-5-5` (batch, dev).

### Retrieval ablation (dev split)

Run `2026-11-25T12-38-14Z_retrieval-ablation`, corpus version 1, 60 questions, plan: oracle: the gold company and fiscal year, not the planner; narrative rows also get their gold chunk's section as the hint (a best case for the boost; FinanceBench PDFs have no section, so that row repeats the one before); reranker `Xenova/ms-marco-MiniLM-L-6-v2`. Latency is client-side wall time per call, including the tunnel and the query embedding; not the serving box's own speed.


**financebench** (hit@k, 95% CI)

| component | n | @1 | @3 | @5 | @8 | vs row above (@8, paired) | p50 ms | p95 ms |
|---|---:|---|---|---|---|---|---:|---:|
| lexical only | 45 | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | - | 19844 | 33756 |
| dense only | 45 | 11.1% [2.2, 20.0] | 15.6% [4.4, 26.7] | 15.6% [4.4, 26.7] | 20.0% [8.9, 33.3] | +20.0 pp [8.9, 33.3] | 572 | 6633 |
| hybrid (RRF) | 45 | 0.0% [0.0, 0.0] | 8.9% [2.2, 17.8] | 15.6% [4.4, 26.7] | 15.6% [4.4, 26.7] | -4.4 pp [-11.1, 0.0] | 21464 | 34267 |
| hybrid + rerank | 45 | 8.9% [2.2, 17.8] | 15.6% [6.7, 26.7] | 15.6% [6.7, 26.7] | 17.8% [6.7, 28.9] | +2.2 pp [-4.4, 11.1] | 19265 | 34973 |
| hybrid + rerank + filters | 45 | 20.0% [8.9, 31.1] | 37.8% [24.4, 51.1] | 44.4% [28.9, 57.8] | 46.7% [31.1, 60.0] | +28.9 pp [15.6, 42.2] | 1192 | 1574 |
| hybrid + rerank + filters + section boost (oracle hint) | 45 | 20.0% [8.9, 31.1] | 37.8% [24.4, 51.1] | 44.4% [28.9, 57.8] | 46.7% [31.1, 60.0] | +0.0 pp [0.0, 0.0] | 1270 | 1565 |

**narrative** (hit@k, 95% CI)

| component | n | @1 | @3 | @5 | @8 | vs row above (@8, paired) | p50 ms | p95 ms |
|---|---:|---|---|---|---|---|---:|---:|
| lexical only | 15 | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | - | 19844 | 33756 |
| dense only | 15 | 20.0% [0.0, 40.0] | 26.7% [6.7, 46.7] | 26.7% [6.7, 46.7] | 33.3% [13.3, 60.0] | +33.3 pp [13.3, 60.0] | 572 | 6633 |
| hybrid (RRF) | 15 | 0.0% [0.0, 0.0] | 13.3% [0.0, 33.3] | 13.3% [0.0, 33.3] | 13.3% [0.0, 33.3] | -20.0 pp [-40.0, 0.0] | 21464 | 34267 |
| hybrid + rerank | 15 | 13.3% [0.0, 33.3] | 20.0% [0.0, 40.0] | 20.0% [0.0, 40.0] | 33.3% [13.3, 60.0] | +20.0 pp [0.0, 40.0] | 19265 | 34973 |
| hybrid + rerank + filters | 15 | 40.0% [13.3, 66.7] | 53.3% [26.7, 80.0] | 60.0% [33.3, 86.7] | 66.7% [40.0, 86.7] | +33.3 pp [13.3, 60.0] | 1192 | 1574 |
| hybrid + rerank + filters + section boost (oracle hint) | 15 | 40.0% [13.3, 66.7] | 53.3% [26.7, 80.0] | 60.0% [33.3, 86.7] | 66.7% [40.0, 86.7] | +0.0 pp [0.0, 0.0] | 1270 | 1565 |

Read with care. FinanceBench hit@k counts only the chunks of the FinanceBench PDF copy: the same passage retrieved from the EDGAR copy of that filing counts as a miss, so these understate evidence retrieval. Each row searches one query (the question); the engine adds a company-and-topics query when its planner returns topics. Rows run in a fixed order with no warm-up, so latency differences between rows do not isolate a component's cost. 'vs row above' compares neighbouring rows, which are not each one added component (dense only does not add to lexical only).

### Retrieval components with the company and year filter on (dev split)

Run `2026-11-25T12-43-19Z_retrieval-ablation-filtered`, corpus version 1, 60 questions, plan: oracle: the gold company and fiscal year, not the planner; narrative rows also get their gold chunk's section as the hint (a best case for the boost; FinanceBench PDFs have no section, so that row repeats the one before); reranker `Xenova/ms-marco-MiniLM-L-6-v2`. Latency is client-side wall time per call, including the tunnel and the query embedding; not the serving box's own speed.


**financebench** (hit@k, 95% CI)

| component | n | @1 | @3 | @5 | @8 | vs row above (@8, paired) | p50 ms | p95 ms |
|---|---:|---|---|---|---|---|---:|---:|
| lexical + filters | 45 | 4.4% [0.0, 11.1] | 8.9% [2.2, 17.8] | 13.3% [4.4, 24.4] | 22.2% [11.1, 35.6] | - | 371 | 614 |
| dense + filters | 45 | 28.9% [15.6, 42.2] | 35.6% [22.2, 51.1] | 37.8% [24.4, 53.3] | 46.7% [31.1, 62.2] | +24.4 pp [11.1, 37.8] | 347 | 387 |
| hybrid + filters | 45 | 11.1% [2.2, 22.2] | 17.8% [6.7, 28.9] | 24.4% [13.3, 37.8] | 26.7% [13.3, 40.0] | -20.0 pp [-35.6, -4.4] | 696 | 926 |
| lexical + filters + rerank | 45 | 17.8% [6.7, 28.9] | 22.2% [11.1, 35.6] | 22.2% [11.1, 35.6] | 24.4% [13.3, 37.8] | -2.2 pp [-6.7, 0.0] | 1072 | 1328 |
| hybrid + filters + rerank | 45 | 20.0% [8.9, 31.1] | 37.8% [24.4, 51.1] | 44.4% [28.9, 57.8] | 46.7% [31.1, 60.0] | +22.2 pp [8.9, 35.6] | 1349 | 1627 |

**narrative** (hit@k, 95% CI)

| component | n | @1 | @3 | @5 | @8 | vs row above (@8, paired) | p50 ms | p95 ms |
|---|---:|---|---|---|---|---|---:|---:|
| lexical + filters | 15 | 40.0% [13.3, 66.7] | 80.0% [60.0, 100.0] | 86.7% [66.7, 100.0] | 86.7% [66.7, 100.0] | - | 371 | 614 |
| dense + filters | 15 | 46.7% [20.0, 73.3] | 46.7% [20.0, 73.3] | 66.7% [40.0, 86.7] | 66.7% [40.0, 86.7] | -20.0 pp [-46.7, 6.7] | 347 | 387 |
| hybrid + filters | 15 | 46.7% [20.0, 73.3] | 66.7% [40.0, 86.7] | 73.3% [53.3, 93.3] | 80.0% [60.0, 100.0] | +13.3 pp [0.0, 33.3] | 696 | 926 |
| lexical + filters + rerank | 15 | 46.7% [20.0, 73.3] | 46.7% [20.0, 73.3] | 60.0% [33.3, 86.7] | 66.7% [40.0, 86.7] | -13.3 pp [-46.7, 20.0] | 1072 | 1328 |
| hybrid + filters + rerank | 15 | 40.0% [13.3, 66.7] | 53.3% [26.7, 80.0] | 60.0% [33.3, 86.7] | 66.7% [40.0, 86.7] | +0.0 pp [-20.0, 20.0] | 1349 | 1627 |

Read with care. FinanceBench hit@k counts only the chunks of the FinanceBench PDF copy: the same passage retrieved from the EDGAR copy of that filing counts as a miss, so these understate evidence retrieval. Each row searches one query (the question); the engine adds a company-and-topics query when its planner returns topics. Rows run in a fixed order with no warm-up, so latency differences between rows do not isolate a component's cost. 'vs row above' compares neighbouring rows, which are not each one added component (dense only does not add to lexical only).
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
