# M5 report: query engine (typed plan, compiler, retrieval, composer)

Written 2026-11-26 by `/dod M5`. Numbers are copied from the files named beside them. Where no source exists the report says `NOT MEASURED`. The numbers table keeps the JSON values unrounded; percentages elsewhere are rounded from those JSON values. Command results are saved in `docs/m5/dod_checks.txt`, the test run's session output in `docs/m5/test_run_log.txt`, and box times (hand-recorded) in `docs/m5/box_sessions.json`.

**Verdict: M5 is NOT done against its own checklist.** Four of the five items pass. Item 1 fails: the engine beats the baseline on numeric, but **not on FinanceBench** (test: engine lower, difference not distinguishable from zero). Whether to close M5 under the CLAUDE.md completeness bar with this shortfall logged is Shekar's call; this report does not make it.

## Definition of done (M05 §6)

| # | Item | Result | Evidence |
|---|---|---|---|
| 1a | Engine beats baseline on numeric, test split, paired-bootstrap CI | **PASS** | `docs/results.md`, "Engine vs baseline": n 168, 96.4% vs 5.4%, +91.1 pp, 95% CI [86.3, 95.2]. Read with the independence caveat below |
| 1b | Engine beats baseline on FinanceBench, test split, paired-bootstrap CI | **FAIL** | Same table: n 105, 17.1% vs 23.8%, **−6.7 pp**, 95% CI [−15.2, 1.9], p 0.1658, "no difference shown" |
| 2 | 100% of claims carry provenance (asserted in the harness) | **PASS**, with limits | `eval/run.py` returns 2 on any engine claim with empty provenance, a numeric claim without fact ids, or a fact id the compiler did not return. The test run exited 0 (`docs/m5/test_run_log.txt`; the results file does not store the gate's outcome); `sets.claims.n` = 498. `uv run pytest tests/unit/eval -k provenance`: 24 passed (`docs/m5/dod_checks.txt`). **Limits:** it checks ids, not that the expression is the compiler's; for text claims it checks that a chunk id and quote exist, not that the quote is in the chunk. Unsupported-claim rate: `NOT MEASURED` (M6) |
| 3 | Coverage report and intent accuracy recorded | **PASS** | `eval/results/coverage/2026-11-26T02-19-54Z_coverage.json`; `eval/results/intent/2026-11-25T11-00-52Z_intent_planner-claude-haiku-5-5.json` (dev only; test intent accuracy `NOT MEASURED`) |
| 4 | Retrieval ablation and composer Pareto tables auto-generated in `docs/results.md` | **PASS** | `make report` wrote both inside the AUTO markers |
| 5 | SQL guard rejects 30/30; fallback off by default, measured separately if enabled | **PASS**, with a gap | `uv run pytest tests/unit/engine/test_sql_guard.py -k corpus`: 31 passed (30 cases + the count check); whole file 159 passed (`docs/m5/dod_checks.txt`). **The fallback is not built:** the engine never calls the guard, `ENGINE_SQL_FALLBACK` has no effect, and the read-only role and statement timeout do not exist. So it is off, and there is nothing to measure. Docstrings in `schema.py` and `guard.py` read as if the switch worked |

## Other checks (run 2026-11-26; output in `docs/m5/dod_checks.txt`)

| Check | Result |
|---|---|
| `make test` | 2992 passed |
| `make test-integration` | 748 passed (run before a two-blank-line format fix to `eval/run.py`) |
| `make lint` | passed, after formatting `eval/run.py` (the first run failed on a missing blank line) |
| `uv run mypy --strict app/engine` | no issues, 27 files |
| `make check-gold` | 6 gold files match CHECKSUMS. No gold file was edited |

## How to read the headline numbers

1. **Numeric accuracy is not independent evidence.** The numeric gold is generated from the same XBRL facts with closely related tag rules. A high score shows the planner, resolver, compiler and writer carry those values through; it does not show the tag rules are right. The dev score of 100% has a degenerate CI. Revenue and revenue-dependent margins are **not** on the gold-verified list: the engine reports them and nothing here scores them.
2. **All six numeric test misses are company-resolution clarifications** (`num-0052`, `0066`, `0075`, `0076`, `0107`, `0176`; `per_question[].refusal` in the test file). Nothing was changed in response.
3. **Company resolution was tuned on dev.** The untuned figure is the paid planner run's company field, 95.8%. The 100% after adding aliases is in-sample.
4. **The test comparison changes two things at once:** the system, and the writer (engine on Haiku, baseline on Sonnet). No Haiku baseline was run.
5. **FinanceBench.** On test the engine refused 79.0% (baseline 69.5%) although its recall@5 was higher (34.3% vs 17.1%). On **dev with the Sonnet writer on both sides** (so not the chosen writer), an ad-hoc comparison of `per_question` refusals found 9 questions the baseline got right and the engine did not, all refusals: 6 needed a unit conversion or calculation the engine did not supply (`fb-03282`, `fb-04171`, `fb-00517`, `fb-01346`, `fb-03718`, `fb-06272`); in 3 the writer judged the material insufficient (`fb-00494`, `fb-00746`, `fb-01319`). The writer is forbidden to calculate (CLAUDE.md rule 6) and the compiler covers only catalog metrics. In the other direction, the engine got 3 right that the baseline did not (`fb-01163`, `fb-03838`, `fb-04672`). The calculation limit is a likely contributor, **not** an established cause of the test gap; test refusals were not analysed.
6. **Test was run once**, after the reply limit, the writer choice and everything else had been fixed on dev. The only code change after it is the CLI default writer (ADR-012); that run passed `--composer haiku` explicitly.
7. **Judges.** The FinanceBench judge's kappa is 0.646 (target 0.70), measured on baseline answers, answered rows only. The narrative judge is uncalibrated.
8. **Latency** is a live sample of 32 dev questions per writer, timed from a laptop that runs embedding and reranking and reaches the box over a tunnel. It is not server latency. The p95 shown beside the test row in the Pareto table is that dev sample. Test latency: `NOT MEASURED`.
9. **Cost per question in the Pareto table is not like for like across rows.** The two dev runs replayed recorded planner replies, so their figures are writer-only. The test run paid for its planner calls, so its figure includes them.

## Retrieval

Tables are in `docs/results.md` (corpus version 1, oracle company and year, dev: 45 FinanceBench, 15 narrative).

- The company and year filter made the largest difference: hit@8 went from 17.8% to 46.7% on FinanceBench and from 33.3% to 66.7% on narrative.
- **With filters on, no setup wins both sets.** `hybrid + filters + rerank` ties `dense + filters` on FinanceBench (46.7%) and is below `lexical + filters` on narrative (66.7% vs 86.7%). The samples are small, and narrative gold was drafted from passages, which may favor lexical matching. The engine's setup was left unchanged; revisit on dev.
- The section boost showed no change, with an oracle hint.
- Unfiltered lexical search was very slow as timed from the laptop (query embedding and tunnel included, so not the box's own speed): p50 19844 ms, p95 33756 ms.

## Known gaps and deviations

- Metric catalog is 25 metrics, not the ~50 in M05 §3.1 (decided: only tag-chain-reviewed metrics).
- Compiler, periods and SQL guard were written by Claude under a one-time override (ADR-047), not by Shekar as M05 §5 planned.
- Grounding violations are not recorded in results files.
- The gold generator has no 10-Q filter. The check against the serving database found all 341 gold fact ids and no affected row (`docs/m5/gold_form_check.json`).
- Two replayed planner files are stamped with a commit that predates the resolver code they ran with (`11-06-14Z`, `11-15-59Z`).
- The first engine dev run had cut-off replies at a 2048-token limit. It is kept in `eval/results/superseded/`.
- Planner quality on messy, hand-typed input: `NOT MEASURED` (dev questions are the eval sets' own wording).
- The reranker checksum is recorded in ADR-012; no code checks it.
- `tests/unit/eval/test_default_composer.py` tests the default-writer helper, not the CLI path through it.

## Results audit (`/results-audit`, 2026-11-26)

**FAIL for the repo as a whole.** In this report the auditor found no mismatch; the untraced items were command output, box times and the budget limit. The command output and box times are now saved under `docs/m5/` (the box times are hand-recorded, not machine-written); the $1.50 limit was given in chat. The auditor did not see these edits or the new files. In older documents, left unchanged:

- **Mismatch:** `docs/milestones/M02-report.md:59` says 12971 chunks cited by element id; `docs/milestones/M02-parse-metrics.json` `html.n_chunks_with_anchor` says 12992.
- **Untraced:** numbers in `M00-report.md`, `M01-report.md`, `M01-dq-report.md`, `M02-report.md` and two hand-written lines of `docs/results.md` (command output, Athena queries, manual checks).

The AUTO blocks of `docs/results.md` were checked separately: `make report` produced no diff.

## Decisions that need Shekar

- Whether to close M5 with item 1b failing, or open a dev round on FinanceBench. Test has now been seen once; any later change should be developed on dev, and a second test run reported as a second run.
- ADR-012: status is `proposed`; its Consequences are a draft to rewrite. ADR-047 and ADR-048 have no new decision here.

## Spend

| Item | USD | Source |
|---|---:|---|
| Planner, dev | 0.0114844 | `intent/…11-00-52Z…` `cost_usd` |
| Engine dev, Haiku, first run (cut-off replies) | 0.0733298 | `superseded/…13-05-46Z…` `cost.total_usd` |
| Engine dev, Haiku 0.6.1 | 0.0717586 | `…00-27-09Z…haiku.json` `cost.total_usd` |
| Sonnet trial, 20 questions | 0.1261240 | `trial/…00-35-53Z…` `cost.total_usd` |
| Engine dev, Sonnet 0.6.1 | 0.6950794 | `…00-51-47Z…sonnet.json` `cost.total_usd` |
| Latency sample, Sonnet | 0.3822238 | `latency/…00-56-19Z…` `cost.total_usd` |
| Latency sample, Haiku | 0.0281887 | `latency/…00-59-58Z…` `cost.total_usd` |
| **Dev subtotal** (sum of the unrounded file values; the limit given in chat was $1.50) | **1.3881888** | |
| Engine test, Haiku (once) | 0.1988071 | `…01-30-25Z…haiku.json` `cost.total_usd` |
| **API total** (sum of the unrounded file values) | **1.5869959** | |

Costs are the harness's own accounting from token usage and `app/llm/prices.py`, not an invoice.

**Box:** two sessions (`docs/m5/box_sessions.json`). (1) Launched 2026-11-25T11:32:07Z; its automatic stop was scheduled for 17:32:35Z, the last successful database check was 17:31:52Z, and it was found stopped at 22:20:04Z (the stop time itself was not observed). (2) Start requested 22:21:26Z, stop requested 2026-11-26T02:25:24Z, confirmed stopped 02:25:57Z. Box dollars: `NOT MEASURED`.

## Numbers

All values are verbatim from the JSON. Paths are under `eval/results/` unless they start with `docs/`.

| What | Value | Source file | Key |
|---|---|---|---|
| Engine test (Haiku): numeric accuracy | `0.9642857142857143` | `2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` | `sets.numeric.accuracy` |
| Engine test (Haiku): numeric 95% CI | `[0.9345238095238095, 0.9880952380952381]` | `2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` | `sets.numeric.ci95` |
| Engine test (Haiku): financebench accuracy | `0.17142857142857143` | `2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` | `sets.financebench.accuracy` |
| Engine test (Haiku): financebench 95% CI | `[0.10476190476190476, 0.24761904761904763]` | `2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` | `sets.financebench.ci95` |
| Engine test (Haiku): FinanceBench refusal rate | `0.7904761904761904` | `2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` | `sets.financebench.refusal_rate` |
| Baseline test (Sonnet): numeric accuracy | `0.05357142857142857` | `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet.json` | `sets.numeric.accuracy` |
| Baseline test (Sonnet): numeric 95% CI | `[0.023809523809523808, 0.08928571428571429]` | `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet.json` | `sets.numeric.ci95` |
| Baseline test (Sonnet): financebench accuracy | `0.23809523809523808` | `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet.json` | `sets.financebench.accuracy` |
| Baseline test (Sonnet): financebench 95% CI | `[0.1619047619047619, 0.3238095238095238]` | `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet.json` | `sets.financebench.ci95` |
| Baseline test (Sonnet): FinanceBench refusal rate | `0.6952380952380952` | `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet.json` | `sets.financebench.refusal_rate` |
| Engine dev (Haiku): numeric accuracy | `1.0` | `2026-11-26T00-27-09Z_engine-0.6.1_haiku.json` | `sets.numeric.accuracy` |
| Engine dev (Haiku): numeric 95% CI | `[1.0, 1.0]` | `2026-11-26T00-27-09Z_engine-0.6.1_haiku.json` | `sets.numeric.ci95` |
| Engine dev (Haiku): financebench accuracy | `0.13333333333333333` | `2026-11-26T00-27-09Z_engine-0.6.1_haiku.json` | `sets.financebench.accuracy` |
| Engine dev (Haiku): financebench 95% CI | `[0.044444444444444446, 0.24444444444444444]` | `2026-11-26T00-27-09Z_engine-0.6.1_haiku.json` | `sets.financebench.ci95` |
| Engine dev (Haiku): FinanceBench refusal rate | `0.8444444444444444` | `2026-11-26T00-27-09Z_engine-0.6.1_haiku.json` | `sets.financebench.refusal_rate` |
| Engine dev (Sonnet): numeric accuracy | `1.0` | `2026-11-26T00-51-47Z_engine-0.6.1_sonnet.json` | `sets.numeric.accuracy` |
| Engine dev (Sonnet): numeric 95% CI | `[1.0, 1.0]` | `2026-11-26T00-51-47Z_engine-0.6.1_sonnet.json` | `sets.numeric.ci95` |
| Engine dev (Sonnet): financebench accuracy | `0.1111111111111111` | `2026-11-26T00-51-47Z_engine-0.6.1_sonnet.json` | `sets.financebench.accuracy` |
| Engine dev (Sonnet): financebench 95% CI | `[0.022222222222222223, 0.2]` | `2026-11-26T00-51-47Z_engine-0.6.1_sonnet.json` | `sets.financebench.ci95` |
| Engine dev (Sonnet): FinanceBench refusal rate | `0.8666666666666667` | `2026-11-26T00-51-47Z_engine-0.6.1_sonnet.json` | `sets.financebench.refusal_rate` |
| Baseline dev (Sonnet): numeric accuracy | `0.027777777777777776` | `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet.json` | `sets.numeric.accuracy` |
| Baseline dev (Sonnet): numeric 95% CI | `[0.0, 0.06944444444444445]` | `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet.json` | `sets.numeric.ci95` |
| Baseline dev (Sonnet): financebench accuracy | `0.24444444444444444` | `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet.json` | `sets.financebench.accuracy` |
| Baseline dev (Sonnet): financebench 95% CI | `[0.13333333333333333, 0.37777777777777777]` | `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet.json` | `sets.financebench.ci95` |
| Baseline dev (Sonnet): FinanceBench refusal rate | `0.6888888888888889` | `2026-11-25T05-47-32Z_baseline-0.2.0_sonnet.json` | `sets.financebench.refusal_rate` |
| Engine test: FinanceBench recall@5 | `0.34285714285714286` | `2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` | `sets.financebench.recall_at_5` |
| Baseline test: FinanceBench recall@5 | `0.17142857142857143` | `2026-11-25T05-48-47Z_baseline-0.2.0_sonnet.json` | `sets.financebench.recall_at_5` |
| Engine test: narrative coverage (judge uncalibrated) | `0.40714285714285714` | `2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` | `sets.narrative.coverage` |
| Engine test: narrative coverage 95% CI | `[0.2785714285714286, 0.5428571428571428]` | `2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` | `sets.narrative.coverage_ci95` |
| Engine test: unanswerable refusal rate | `1.0` | `2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` | `sets.unanswerable.refusal_rate` |
| Engine test: false refusal, answerable sets | `0.3344155844155844` | `2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` | `sets.answerable_false_refusal` |
| Engine dev (Haiku): narrative coverage (judge uncalibrated) | `0.5` | `2026-11-26T00-27-09Z_engine-0.6.1_haiku.json` | `sets.narrative.coverage` |
| Engine dev (Haiku): narrative coverage 95% CI | `[0.2833333333333333, 0.7166666666666667]` | `2026-11-26T00-27-09Z_engine-0.6.1_haiku.json` | `sets.narrative.coverage_ci95` |
| Engine dev (Haiku): unanswerable refusal rate | `1.0` | `2026-11-26T00-27-09Z_engine-0.6.1_haiku.json` | `sets.unanswerable.refusal_rate` |
| Engine dev (Haiku): false refusal, answerable sets | `0.3333333333333333` | `2026-11-26T00-27-09Z_engine-0.6.1_haiku.json` | `sets.answerable_false_refusal` |
| Engine dev (Sonnet): narrative coverage (judge uncalibrated) | `0.6166666666666667` | `2026-11-26T00-51-47Z_engine-0.6.1_sonnet.json` | `sets.narrative.coverage` |
| Engine dev (Sonnet): narrative coverage 95% CI | `[0.4, 0.8166666666666667]` | `2026-11-26T00-51-47Z_engine-0.6.1_sonnet.json` | `sets.narrative.coverage_ci95` |
| Engine dev (Sonnet): unanswerable refusal rate | `1.0` | `2026-11-26T00-51-47Z_engine-0.6.1_sonnet.json` | `sets.unanswerable.refusal_rate` |
| Engine dev (Sonnet): false refusal, answerable sets | `0.32575757575757575` | `2026-11-26T00-51-47Z_engine-0.6.1_sonnet.json` | `sets.answerable_false_refusal` |
| Engine test: claims | `498` | `2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` | `sets.claims.n` |
| Engine test: claim unsupported rate | `null` | `2026-11-26T01-30-25Z_engine-0.6.1_haiku.json` | `sets.claims.unsupported_rate_post` |
| Latency sample, claude-sonnet-5-5 (ms, laptop to box) | `{"p50": 5905.0, "p95": 15349.599999999999}` | `latency/2026-11-26T00-56-19Z_engine-0.6.1_sonnet.json` | `latency_ms` |
| Latency sample, claude-haiku-5-5 (ms, laptop to box) | `{"p50": 5735.5, "p95": 14099.65}` | `latency/2026-11-26T00-59-58Z_engine-0.6.1_haiku.json` | `latency_ms` |
| Planner intent accuracy, dev, paid run | `{"numeric": {"n": 72, "rate": 1.0, "ci95": [1.0, 1.0]}, "narrative": {"n": 15, "rate": 1.0, "ci95": [1.0, 1.0]}}` | `intent/2026-11-25T11-00-52Z_intent_planner-claude-haiku-5-5.json` | `intent_accuracy` |
| Planner field accuracy, numeric dev, paid run: metric | `{"n": 72, "rate": 1.0, "ci95": [1.0, 1.0]}` | `intent/2026-11-25T11-00-52Z_intent_planner-claude-haiku-5-5.json` | `numeric_plan_fields.metric` |
| Planner field accuracy, numeric dev, paid run: company | `{"n": 72, "rate": 0.9583333333333334, "ci95": [0.9027777777777778, 1.0]}` | `intent/2026-11-25T11-00-52Z_intent_planner-claude-haiku-5-5.json` | `numeric_plan_fields.company` |
| Planner field accuracy, numeric dev, paid run: period | `{"n": 72, "rate": 1.0, "ci95": [1.0, 1.0]}` | `intent/2026-11-25T11-00-52Z_intent_planner-claude-haiku-5-5.json` | `numeric_plan_fields.period` |
| Planner field accuracy, numeric dev, paid run: operation | `{"n": 72, "rate": 1.0, "ci95": [1.0, 1.0]}` | `intent/2026-11-25T11-00-52Z_intent_planner-claude-haiku-5-5.json` | `numeric_plan_fields.operation` |
| Company field after dev-driven aliases (in-sample replay) | `{"n": 72, "rate": 1.0, "ci95": [1.0, 1.0]}` | `intent/2026-11-25T11-15-59Z_intent_planner-claude-haiku-5-5.json` | `numeric_plan_fields.company` |
| Questions needing clarification, paid planner run | `{"numeric": 3, "narrative": 1, "financebench": 7, "unanswerable": 2}` | `intent/2026-11-25T11-00-52Z_intent_planner-claude-haiku-5-5.json` | `needs_clarification` |
| Coverage: companies per fiscal year | `{"2020": 482, "2021": 487, "2022": 493, "2023": 496, "2024": 496, "2025": 486}` | `coverage/2026-11-26T02-19-54Z_coverage.json` | `filers_per_year` |
| Gold 10-Q check: distinct_gold_fact_ids | `341` | `docs/m5/gold_form_check.json` | `distinct_gold_fact_ids` |
| Gold 10-Q check: found | `341` | `docs/m5/gold_form_check.json` | `found` |
| Gold 10-Q check: missing | `0` | `docs/m5/gold_form_check.json` | `missing` |
| Gold 10-Q check: affected_gold_rows | `[]` | `docs/m5/gold_form_check.json` | `affected_gold_rows` |

Paired differences are computed by `eval/report.py` from the two results files and exist only in `docs/results.md` (AUTO block).

## Metric coverage

Share of tracked companies (of 500) with a full-year 10-K fact in that fiscal year for which the compiler returns a value, fiscal 2020–2025 pooled. Source: `eval/results/coverage/2026-11-26T02-19-54Z_coverage.json`, `by_metric`. A low figure mixes companies that do not report an item with tag chains that miss it; the two were not separated.

| metric | found / pairs | coverage | not-found reasons |
|---|---|---|---|
| total_assets | 2922 / 2940 | `0.9938775510204082` | no_fact 13, conflicting_candidates 5 |
| operating_cash_flow | 2882 / 2940 | `0.9802721088435374` | no_fact 58 |
| income_tax | 2873 / 2940 | `0.9772108843537415` | no_fact 67 |
| eps_diluted | 2836 / 2940 | `0.964625850340136` | no_fact 104 |
| eps_basic | 2826 / 2940 | `0.9612244897959183` | no_fact 114 |
| stockholders_equity | 2767 / 2940 | `0.941156462585034` | no_fact 167, conflicting_candidates 6 |
| net_income | 2659 / 2940 | `0.904421768707483` | no_fact 281 |
| revenue | 2546 / 2940 | `0.8659863945578231` | no_fact 294, conflicting_candidates 100 |
| cash | 2515 / 2940 | `0.8554421768707483` | no_fact 422, conflicting_candidates 3 |
| net_margin | 2334 / 2940 | `0.7938775510204081` | missing_input 606 |
| operating_income | 2208 / 2940 | `0.7510204081632653` | no_fact 732 |
| total_liabilities | 2096 / 2940 | `0.7129251700680272` | no_fact 839, conflicting_candidates 5 |
| operating_margin | 1985 / 2940 | `0.6751700680272109` | missing_input 955 |
| capex | 1974 / 2940 | `0.6714285714285714` | no_fact 966 |
| shares_outstanding | 1757 / 2940 | `0.5976190476190476` | no_fact 1178, conflicting_candidates 5 |
| cost_of_revenue | 1721 / 2940 | `0.5853741496598639` | no_fact 1219 |
| sga | 1593 / 2940 | `0.5418367346938775` | no_fact 1347 |
| dividends_paid | 1411 / 2940 | `0.47993197278911565` | no_fact 1529 |
| interest_expense | 1401 / 2940 | `0.47653061224489796` | no_fact 1539 |
| long_term_debt | 1387 / 2940 | `0.4717687074829932` | no_fact 1550, conflicting_candidates 3 |
| gross_profit | 1038 / 2940 | `0.35306122448979593` | missing_input 1301, fallback_not_allowed 601 |
| gross_margin | 982 / 2940 | `0.3340136054421769` | missing_input 1958 |
| r_and_d | 816 / 2940 | `0.27755102040816326` | no_fact 2124 |
| revenue_including_assessed_tax | 187 / 2940 | `0.06360544217687075` | no_fact 2753 |
| bank_revenue_net_of_interest_expense | 48 / 2940 | `0.0163265306122449` | no_fact 2892 |
