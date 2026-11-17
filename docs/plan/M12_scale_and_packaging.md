# M12: Scale run and packaging (the evidence pack)

**Time:** about 10 h · **Roles:** all; this is what recruiters and interviewers see · **Depends on:** everything

## 1. Goal

Final numbers from final runs, a README a non-engineer can follow, a 3-minute video, a fresh-clone quickstart under 30 minutes, and resume bullets filled in only from measured results.

## 2. Steps

### 2.1 Final scale runs (2 h + runtime)
- Full-history `fsds_to_iceberg` + `normalize_facts`, then `parse_filings` + `embed_chunks` (incremental: only new chunks) + `export_serving` on EMR Serverless. Record runtime and cost from **job metrics** (billed vCPU-hours and GB-hours × the published rates), and cross-check against Cost Explorer by tag a day later (Cost Explorer lags) → **T1, T2, T2b**.
- Load the final corpus version on EC2 and swap.

### 2.2 Final benchmark (1 h + batch time)
On the **test** split with `MODE=batch`, run every configuration: `baseline`, `engine (no verifier, planner routing)`, `engine + verifier`, `engine + verifier + ML router`, and both composer tiers for the chosen configuration. Then `eval/report.py` → `docs/results.md`, and `/results-audit` must pass.

### 2.3 README (3 h; you write the prose, Claude formats it)
Sections, in this order:
1. **One-line pitch + demo link + 3-min video link.**
2. **Results table** (an auto-generated snippet from `docs/results.md`): accuracy with CIs, unsupported-claim rate pre/post, refusal/false refusal, p50/p95 latency, cost/question, freshness, users.
3. **Architecture diagram** (export the mermaid diagram to PNG with mermaid-cli) + a 6-line request walkthrough.
4. **What's interesting here:** typed plan instead of text-to-SQL; claim-level verification; the learned router cascade; lakehouse → serving with an atomic swap; real-user flywheel. One paragraph each, linking the ADRs.
5. **Quickstart (< 30 min on sample data):** `make sample-data` (2 FSDS quarters, 20 filings, 15 FinanceBench questions) → `make up` → `make load-sample` → `make bench SAMPLE=1 MODE=replay` (uses committed cassettes, so **$0 and no API key needed**) → `make dev` (UI).
6. **Design decisions** (links to `docs/decisions.md`), including where you disagreed with your first plan.
7. **Limitations** (honest): metric-catalog coverage %, custom tags excluded, US filers only, 10-K/10-Q only, FinanceBench n=150, the LLM judge's κ, Postgres FTS ≠ BM25, single-box serving.
8. **Cost:** build-out cost and monthly demo cost.
9. **Data and licenses:** SEC EDGAR (public domain [Likely]); FinanceBench (CC-BY-NC-4.0, attributed); "not investment advice."

### 2.4 Demo video (2 h)
3-minute script:
| Time | Shot |
|---|---|
| 0:00 | The problem: analysts can't trust LLM numbers about filings |
| 0:20 | Numeric question → "verified against XBRL" badge → open the fact citation (tag, period, filing) |
| 0:50 | Hybrid question → narrative citation opens SEC.gov with the passage highlighted |
| 1:25 | Trick question (future guidance) → refusal that shows what *was* found |
| 1:50 | Jaeger trace: where the time and money go |
| 2:15 | Results table + architecture in 20 seconds |
| 2:40 | What users taught you (one fix, with numbers) → close |
Record at 1080p with captions. Upload unlisted to YouTube and link it from the README and the UI's About page.

### 2.5 Fresh-clone test (1 h)
Clone into a clean directory (or a clean container) on a different machine if you can, and follow only the README. Time it. Fix every stumble. **DoD: under 30 minutes.**

### 2.6 Resume and wrap-up (1 h)
- Fill in the bullet templates (`02_role_coverage.md` §4) **only** from `eval/results/*.json`, `load/results/*.json` and the M1/M3/M10 reports.
- Tag `v1.0.0`; GitHub release with the final results JSONs attached.
- Decide on post-project hosting (`04_aws_and_cost_plan.md` §3: keep / demo-on-demand / destroy) and execute it.
- Run `/interview-me` over the whole repo, using the question bank in `02_role_coverage.md` §5.

## 3. Claude Code session plan

```
/milestone M12
Build make sample-data and make load-sample per M12 §2.3.5 (small, deterministic, cassettes committed
for replay), and the mermaid → PNG export. Then draft README section skeletons with placeholders
for prose I'll write; numbers must be includes of auto-generated snippets, never typed.
```
```
/results-audit
```

## 4. Definition of done

- [ ] T1, T2 and T2b final, from job metrics, cross-checked with Cost Explorer.
- [ ] The final test-split results for all configurations are in `docs/results.md`; `/results-audit` is clean.
- [ ] README complete; video linked; the fresh-clone quickstart takes under 30 min.
- [ ] Resume bullets filled from measured numbers only.
- [ ] Hosting decision executed; `make cost-today` is checked a day later.
