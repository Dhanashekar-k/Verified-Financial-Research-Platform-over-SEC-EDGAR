# M4 catalog — review decisions & change list

*Distilled from the accounting reviewer's memo (23 Nov 2026). Confidence labels are hers.
This is the working change-list for fixing `metric_catalog.yaml`, the numeric generator, and the
verified-metrics allowlist.*

## Headline

**[Likely] Do not approve the dictionary for benchmark generation unchanged.** A "first-populated-tag
wins" rule does not guarantee the number is the financial concept the question asks. Verifying that a
number *exists* in EDGAR is not the same as verifying it *means what the question asks*. (She calls a
source-only match **"source-matched"**, reserving **"verified"** for when scope, period, unit, entity,
and interpretation also match.)

## The 6 decisions (final)

| Call | Decision | Why | Catalog / generator action |
|---|---|---|---|
| **1a. Tax-inclusive revenue** (`RevenueFromContractWithCustomerIncludingAssessedTax`) | **DROP** from the generic revenue chain; keep as a *separate named* metric | Includes excise/sales tax → not comparable (Philip Morris $29.6B excl. vs $79.8B incl., 2.69×) | Remove tag from `revenue.tags`. Optionally add `revenue_including_assessed_tax` as its own metric |
| **1b. Bank revenue** (`RevenuesNetOfInterestExpense`) | **KEEP, but as a distinct concept** for financial institutions | Net interest income + fees is a real bank revenue measure, not comparable to industrial sales | Remove from generic `revenue.tags`; add `bank_revenue_net_of_interest_expense` with `presentation_basis: net_of_interest_expense` |
| **2. Cost of revenue** | **Prefer `CostOfRevenue` (consolidated total), conditionally** | First tag isn't right just because it's first (Block $6.76B vs $143.9M) | Keep order, but add an **ambiguity guard**: if the two tags disagree materially and can't be reconciled, mark the metric *ambiguous*, don't silently take the first |
| **3. Gross profit = revenue − cost** | **YES, but derived from compatible verified inputs only** | Standard arithmetic; validity depends on the inputs | Keep `fallback_formula`; add `only_if` conditions (same entity/period/currency/scope, inputs verified); mark result **`derived`** not `reported`; abstain if inputs incompatible |
| **4. Total liabilities** (`Liabilities`) | **KEEP** | Correct consolidated total | Verify as-is; enforce **instant** (`qtrs=0`), consolidated, balance-sheet date |
| **5. Shares outstanding** (`CommonStockSharesOutstanding`) | **KEEP** | Correct balance-sheet-date share count | Verify as-is; instant. Distinguish from cover-page (`dei:`) and weighted-average (EPS denominator) |
| **6. Long-term debt** (`LongTermDebtNoncurrent`) | **KEEP, under its precise name** | Excludes current portion + leases ($175M, not $200M, in her example) | Rename output label to **"noncurrent long-term debt"**; optionally build a separate `total_debt` metric |

## The bigger structural finding — revenue isn't a simple chain

Even the **top two** revenue tags aren't universal synonyms: `...ExcludingAssessedTax` vs `Revenues`
differ by >1% in **849 of 1,328 same-period pairs (63.9%)**. So an unconditional priority chain is
risky *specifically for revenue*.

**Better rule [Likely]:** for "total revenue," select the **consolidated income-statement top line as
presented** for that filing/period; use the tag as *evidence*, not the sole arbiter. For ASC-606
contract revenue specifically, use the narrower tag. For banks, use the bank-specific presentation and
name it.

**Selection sequence to implement in the generator:**
1. Parse the question's metric definition (total reported revenue vs ASC-606 vs bank revenue).
2. Identify CIK, financial statement, exact fiscal period, `form`, filing accession.
3. Collect candidate facts with taxonomy tag, raw units, start/end dates, filing metadata, **XBRL
   dimensions**.
4. Prefer the consolidated **statement presentation**; validate against calculation relationships.
5. Resolve amendments by a declared **as-filed** or **latest-restated** policy.
6. If two incompatible candidates remain, **expose the ambiguity or abstain — never take the first
   populated value.**

## Data-quality safeguards to bake into the generator

- **`qtrs=0` on a duration metric** [Certain]: quarantine + inspect the 22 flagged FY rows (revenue 1,
  net_income 1, income_tax 2, capex 1, dividends 17). Never silently admit. Keep Q1 / H1 / 9M / Q4 / FY
  distinct; never use a year-to-date amount as a standalone quarter.
- **EPS units** [Unclear]: catalog shows `uom: USD` but EPS should be USD-per-shares. Check raw `uom`
  and dimensions before trusting EPS facts — the normalization may have dropped the denominator.
- **Periods / restatements**: preserve actual fiscal start/end dates (not just `fy` as an int),
  accession, filing date, original value, and later restated value. State an as-filed/latest-restated
  policy.
- **Multi-dimensional facts**: match the **consolidated total**, not a segment/geography/share-class;
  the join key needs dimensions + a consolidation policy, not just `(cik, fiscal_year, period, tag)`.
- **Margins**: don't clamp to 0–100%. Report undefined when revenue ≈ 0; add a near-zero-denominator
  flag. (Catalog shows operating margin −1,399% to 9,587% — these are denominators, not clamp targets.)
- **Scope honesty**: state exact datasets, fiscal coverage, taxonomy scope, filing types, exclusions.
  Don't claim "all XBRL facts." A US-GAAP dictionary does not cover IFRS filers.

## Evaluation implications (for the rest of M4)

- **[Certain] The 300 auto-generated numeric set is not independent** if gold and the system's answer
  use the same mapping — a mapping error appears in both and inflates accuracy.
- **Add 50–100 independently hand-audited numeric questions** — audit the actual SEC statement, not the
  normalized DB. Deliberately include the hard cases: bank revenue, tax-inclusive revenue, derived gross
  profit, parent vs group income, YTD vs quarter, FY crossing calendar year, restatements, zero revenue,
  contradictory tags.
- **Report metrics separately**: source-match accuracy, accounting-concept accuracy, period/scope
  accuracy, answer accuracy, citation quality, correct abstention / false refusal.
- **Metric-aware tolerances**, not a flat 1% — rounding- and scale-aware. 1% can hide material errors on
  large values or misclassify rounded per-share figures.
- The demo should include **one case where the system declines an attractive but semantically wrong
  SEC-tag value** — that's what proves the verifier earns its place.

## Scope note (mine, not the reviewer's) — don't rebuild the engine at 4 AM

Not all of this is M4. To finish the **M4 gold**, you need the subset:
- the 6 catalog decisions above,
- the `qtrs=0` quarantine,
- the derived-gross-profit guard,
- the consolidated-total / abstain-on-ambiguity rule for **revenue** and **cost_of_revenue**,
- metric-aware tolerances,
- the small independently hand-audited sanity set.

The fuller **typed registry** (presentation_basis, as-filed/latest-restated resolution, dimensional
consolidation policy, the four verification states) is largely **M5 (query engine) + M6 (verifier)**
work. Record those as ADRs now and implement them there — don't balloon M4.

## What changes, by file

- **`app/engine/plan/metric_catalog.yaml`** — remove `IncludingAssessedTax` and
  `RevenuesNetOfInterestExpense` from `revenue.tags`; drop the inert deprecated tags (`SalesRevenueNet`,
  `CostOfGoodsSold`); add `revenue_including_assessed_tax` and `bank_revenue_net_of_interest_expense` as
  separate metrics; add `gross_profit` `only_if` conditions + `derived` flag; rename `long_term_debt`
  label to noncurrent.
- **`eval/gen/make_numeric_set.py`** — consolidated-total preference + abstain-on-ambiguity for revenue
  and cost_of_revenue; `qtrs=0` quarantine; metric-aware tolerances; period/restatement policy; exclude
  non-consolidated dimensional facts.
- **`eval/gen/numeric_metrics_verified.txt`** — list the verified metrics. **Hold `revenue` and its
  dependents (margins, gross_profit) until the revised revenue resolver is tested** against a sample of
  the conflicting filings (the reviewer's explicit final step). The clean metrics can be listed now.
- **`docs/decisions.md`** — ADRs for the 6 calls, the revenue resolver, and the data-quality policies.

## Reviewer's final step (do this before generating revenue gold)

> "Test the revised revenue resolver against a deliberately chosen sample of the catalog's conflicting
> filings **before** populating the production numeric benchmark."
