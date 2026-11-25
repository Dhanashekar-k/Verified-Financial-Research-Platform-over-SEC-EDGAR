# M5: Query engine (typed plan → compiler → retrieval → composer)

**Time:** about 22 h · **Roles:** AI engineer (core), SDE (compiler design, safety) · **Depends on:** M4 (you need the baseline first)

## 1. Goal

A router + plan + SQL + retrieval pipeline that beats the baseline with statistical significance, in which **every claim carries provenance** and **the LLM never does arithmetic or writes SQL on the main path**.

## 2. Inputs → outputs

| Inputs | Outputs |
|---|---|
| Postgres `facts`, `chunks`, `companies` | `app/engine/entities/resolve_company.py` + `aliases.csv` |
| `metric_catalog.yaml` (you write ~50 metrics) | `app/engine/plan/{schema.py, compiler.py, periods.py}` (**you**) |
| M4 harness + baseline | `app/engine/retrieval/{lexical,dense,fuse,rerank,query.py}` |
| — | `app/engine/compose/composer.py` + prompts |
| — | `app/engine/sql_guard/guard.py` (**you**, fallback behind a flag) |
| — | `app/engine/engine.py` orchestrator (async, spans, usage) |
| — | Results: engine vs baseline (paired bootstrap), retrieval ablation table, Haiku-vs-Sonnet composer Pareto |

## 3. Steps

### 3.1 Metric catalog (3 h; **you**)
Write `metric_catalog.yaml` (format in `05_data_contracts.md` §4.2) for about 50 metrics: income statement (revenue, cost of revenue, gross profit, R&D, SG&A, operating income, interest expense, pre-tax income, income tax, net income, EPS basic/diluted, diluted shares), balance sheet (cash, receivables, inventory, current assets, PP&E net, goodwill, total assets, current liabilities, long-term debt, total liabilities, equity), cash flow (CFO, capex, CFI, CFF, dividends paid, buybacks, D&A) and derived (gross/operating/net margin, free cash flow = CFO − capex, current ratio, debt-to-equity, ROE, effective tax rate, capex intensity, YoY growth).
- **Test:** every listed tag exists in `raw.fsds_tag` (this catches invented tags).
- **Coverage report:** for each metric, the share of (S&P 500 company, FY) pairs where the compiler finds a value. Put it in the M5 report. Low coverage means you add fallback tags or formulas.

### 3.2 Entity and period resolution (2 h)
- `resolve_company(mention) -> list[CompanyMatch]`: exact ticker → exact name → aliases → rapidfuzz `token_set_ratio ≥ 90` over tracked companies → otherwise ambiguous (`needs_clarification`). Tests: Apple vs Applied Materials; Alphabet/Google; Meta/Facebook; JPM; Berkshire Hathaway classes; "Coke" → Coca-Cola.
- `periods.py` (**you**): normalize "FY2023", "fiscal 2023", "2023", "Q3 2022", "last year", "most recent quarter", "trailing" (unsupported → clarify). Relative periods resolve against **the company's latest filed period**, not today's date. When no period is given, use the latest FY and state it in the answer.

### 3.3 Planner (2 h)
- Haiku 5.5 with structured output → `QueryPlan` (`05_data_contracts.md` §4.1). The `metrics` field is an enum generated from the catalog keys, so **the model can't invent a metric**.
- System prompt (cached): the catalog (key, label, synonyms), period conventions, intent definitions and 8 few-shot examples. Companies are returned as `mention` only; code resolves CIKs.
- Until M7 exists, `plan.intent` is the router. Measure intent accuracy on whatever labels you have (numeric set = numeric; your narrative set = narrative). **Start hand-labeling `eval/sets/router_test.jsonl` now** (M7 needs about 250 real questions).

### 3.4 Plan compiler (6 h; **you**, the heart of the project)
> **Superseded in part (2026-11-25, 05 §4.5 + ADR-046):** the entry point is `compile_plan(plan, conn, ...) -> CompileOutcome` (results + `not_found`), not `compile(plan) -> list[NumericResult]`; "the first tag with a value wins" is replaced by ADR-043 resolve-or-abstain; the `sql` provenance type is deferred (fallback rows cite `expr`). Text below is kept as written.

`compile(plan) -> list[NumericResult]`, with `NumericResult{metric, cik, fiscal_year, fiscal_period, value: Decimal, unit, expr, fact_ids, notes}`.
- **Tag metrics:** a parameterized query over the ordered fallback tags. The first tag with a value wins:
  ```sql
  SELECT fact_id, tag, value, uom, accession, filed, is_restated
  FROM facts
  WHERE cik = $1 AND tag = ANY($2) AND fiscal_year = $3 AND fiscal_period = $4
    AND uom = $5 AND is_original
  ORDER BY array_position($2, tag)
  LIMIT 1;
  ```
- **Formula metrics:** parse `formula` into an AST (`+ - * /` and catalog keys only, using `ast.parse` with a node allow-list or a tiny Pratt parser) → compile the operands recursively → evaluate with `Decimal` → `expr` such as `f:111 / f:222`.
- **Operations:** `value`; `growth` = (v_t − v_{t−1}) / |v_{t−1}| (refuse when the base ≤ 0 or is missing); `compare` (one result per company); `trend` (series over N years); `rank` (v1: among the companies named, or among S&P 500 for one metric and year via `ORDER BY value DESC LIMIT 10`, with provenance per row).
- **Missing data** → `NotFound(metric, cik, period, reason)`. The composer turns it into a partial answer or a refusal; it never fills the gap.
- **Restated:** if `is_restated`, also fetch the latest version and add a note that the composer must mention.
- **Unit and scale:** values stay in base units (USD, not millions) internally; formatting happens only in the composer and UI.

### 3.5 Guarded text-to-SQL fallback (2 h; **you**; off by default)
`sql_guard.validate(sql) -> SafeSQL | Rejection`, using sqlglot:
- Exactly one `SELECT` statement; tables/views limited to `{facts, companies}`; functions limited to an allow-list (`sum, avg, min, max, count, round, abs, coalesce`); no comments, no `;`-chained statements, no CTEs that write.
- Inject or clamp `LIMIT 200`. Execute as the role `edgarqa_ro` (`GRANT SELECT` on views only) with `SET LOCAL statement_timeout = '2s'`.
- Used only when `compile()` raises `UnsupportedPlan` **and** `ENGINE_SQL_FALLBACK=on`. Its results get `provenance.type = "sql"`, carrying the row fact IDs, and are verified the same way.
- Tests: a corpus of 30 malicious or invalid SQL strings must all be rejected (DROP, UPDATE, `pg_sleep`, `COPY`, `;` chaining, comment tricks, system catalogs, `dblink`).

### 3.6 Retrieval pipeline (3 h)
- **Query construction:** retrieval query = company name + `narrative_topics` (+ the question as a second query); filters `cik ∈ plan`, `fiscal_year ∈ plan periods` (the filing's fiscal year); boost `section ∈ section_hints` with an RRF bonus (not a hard filter).
- Lexical top-50 + dense top-50 (iterative scan) → RRF → **cross-encoder rerank** the top 30 → top 8. Cache the reranker model in the process.
- **Ablation on dev:** lexical only, dense only, hybrid, hybrid + rerank, hybrid + rerank + filters. Measure FinanceBench evidence-page recall@k, narrative gold-chunk recall@k and latency. Put the table in the report. This is where you justify each component.

### 3.7 Composer (2 h)
- Input: question, plan, `NumericResult`s (values, display strings and **ready-made provenance**), the top chunks (IDs + text), and any `NotFound`s.
- Output: `Answer` (`05_data_contracts.md` §4.3) via structured output. Rules in the (cached) system prompt:
  1. Every claim has exactly one provenance.
  2. **Numeric claims may only use values from the `NumericResult`s, with their provenance copied. Do no arithmetic.** If a number you'd like to state isn't provided, don't state it.
  3. Numbers that appear in chunk text can be stated only as text claims whose `quote` contains that number.
  4. `quote` must be copied verbatim from the chunk.
  5. If the provided material doesn't answer the question, set `refusal` (`reason`, `found`, `missing`; `05_data_contracts.md` §4.3, ADR-041) with what was found.
- **Pareto:** run dev with Haiku 5.5 and Sonnet 5.5 composers. Record accuracy, cost/question and p95 latency for both, and pick the default (ADR-012).

### 3.8 Orchestrator (2 h)
`engine.answer(question, cfg) -> Answer + Usage + spans`:
- `plan` → in parallel (`asyncio.gather`): `compile(plan)` if numeric/hybrid; `retrieve(plan)` if narrative/hybrid → `compose` → (`verify`: pass-through until M6).
- `out_of_scope` → a templated refusal (no composer call; §4.3 shape with `found`/`missing` empty).
- OpenTelemetry spans per stage, with token/cost attributes (exporter wired in M8; spans can exist now).

## 4. Claude Code session plan

```
/milestone M5
Step 1: Use the test-writer subagent to write tests for the plan compiler from M05 §3.4 using a
Testcontainers Postgres seeded with ~200 real facts for 5 companies (fixture script from curated.facts).
Cases: tag fallback order, formula metrics, growth with a negative base (refuse), missing data
→ NotFound, restated note, quarterly vs annual, unit preservation. Don't implement the compiler.
```
```
Step 2: resolve_company + aliases per §3.2 with tests. periods.py: write tests only (I implement it).
```
```
Step 3: planner per §3.3: schema generation from the catalog, cached system prompt, 8 few-shots
(draft them; I'll edit). Measure intent accuracy on dev with the harness.
```
```
Step 4: retrieval pipeline + ablation runner per §3.6. Output an ablation JSON, then a table via report.py.
```
```
Step 5: composer + orchestrator per §3.7–3.8. Then run: make bench SYSTEM=engine SPLIT=dev for
haiku and sonnet composers (mode replay where possible) and generate the Pareto table.
```
Then you implement `compiler.py`, `periods.py` and `sql_guard/guard.py` until the tests pass, and run `/interview-me app/engine/plan`.

## 5. You write

`metric_catalog.yaml`, `compiler.py`, `periods.py`, `sql_guard/guard.py`, the planner few-shots (final edit), and ADR-002 / ADR-012.

## 6. Definition of done

- [ ] Engine beats baseline on numeric and FinanceBench **test** splits: deltas with paired-bootstrap 95% CIs recorded.
- [ ] 100% of claims carry provenance (asserted in the harness).
- [ ] Metric coverage report and intent accuracy recorded.
- [ ] Retrieval ablation table and composer Pareto table in `docs/results.md` (auto-generated).
- [ ] The SQL guard rejects 30/30 malicious cases; the fallback is off by default and measured separately if enabled.

## 7. Pitfalls

- The composer "helpfully" computing a ratio. Rule 2 plus the verifier (M6) catch it; add a test case.
- Using the filing's fiscal year for a fact (M1 bug resurfacing): numeric-set failures cluster on comparatives.
- Hard-filtering on section hints (it kills recall when the planner guesses wrong). Boost instead.
- A reranker on the full top-100 makes latency explode. Cap it at 30.

## 8. Interview prep

- Why a typed plan beats text-to-SQL here, and when you'd use text-to-SQL anyway.
- Walk through compiling "Did Tesla's gross margin fall in 2023?" end to end, including provenance.
- Your ablation: which retrieval component mattered most, by how much, at what latency.
- Your Pareto: why you chose that composer.
