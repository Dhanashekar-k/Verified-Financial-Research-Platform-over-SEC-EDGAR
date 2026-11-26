# M6 adversarial set: candidate base claims

Drafted 2026-11-26 by Claude for Shekar, who **writes `eval/sets/adversarial_claims.jsonl`**. This file suggests bases only: it contains no row and no changed text.

Source: `uv run python -m eval.verifier_adversarial --list-bases`, which lists the 216 dev claims the (uncalibrated) support judge marked supported (`eval/results/verifier/gate0_dev_pre.json`, `all_claims.supported`). Claim texts are copied from `gate0_dev_trace.json`. Every numeric base below has a unit and saved facts; every text base cites a saved passage its composer was shown. The numeric bases carry no compiler note.

**Not verified:** whether each base passes the cores, which don't exist yet. If the verifier already drops a base, a row that **changes** it scores `vacuous`; a row with an empty `change` (semantic rows and controls) scores `dropped`. Swap a vacuous base for another from the same table.

Row format: `docs/plan/05_data_contracts.md` section 6. For each base, `uv run python -m eval.verifier_adversarial --show <question_id> <claim_id>` prints the evidence; `--check` validates your file without the cores.

## `wrong_year` (expected: drop)

The text states a fiscal year in a form decision C pins (`fiscal YYYY` or `FYYYYY`). Change only that year.

| `question_id` | `claim_id` | Claim (copied from the trace) | Why this base |
|---|---|---|---|
| `num-0030` | `c1` | Genuine Parts Co operating cash flow for FY2023 was $1.4 billion. | `FY2023`, one reported fact |
| `num-0032` | `c1` | Vulcan Materials' total liabilities at the end of fiscal 2024 were $9.0 billion. | `fiscal 2024`, a balance-sheet figure |
| `num-0054` | `c1` | Cardinal Health's interest expense for fiscal 2024 was $51.0 million. | `fiscal 2024` |
| `num-0056` | `c1` | Cisco Systems' capital expenditures for fiscal 2025 were $905.0 million. | `fiscal 2025` |
| `num-0067` | `c1` | Net cash provided by operating activities for Salesforce, Inc. in FY2022 was $7.1 billion. | `FY2022` |

## `wrong_company` (expected: drop)

**Numeric claims only:** text claims are not checked by company name, so a renamed text claim is kept by design. Change only the name. The compare pairs test the hardest case: both companies are allowed, so only the named-company rule (D3) can catch a swap.

| `question_id` | `claim_id` | Claim (copied from the trace) | Why this base |
|---|---|---|---|
| `num-0202` | `c2` | McKesson Corp net cash from operating activities in FY2023 was $4.3 billion. | compare pair with `c3`: swap in TE Connectivity |
| `num-0202` | `c3` | TE Connectivity plc net cash from operating activities in FY2023 was $3.1 billion. | the other half of that pair |
| `num-0220` | `c1` | Costco Wholesale's total liabilities at the end of FY2022 were $43.5 billion. | compare pair with `c2` (Costco, Carvana) |
| `num-0230` | `c2` | PulteGroup total liabilities, FY2025: $5.1 billion | compare pair with `c1` (D.R. Horton, PulteGroup) |
| `num-0037` | `c1` | Cardinal Health Inc. capital expenditures in Q1 FY2021 were $78.0 million. | single company: name one outside the plan (a real record such as Microsoft) |
| `num-0057` | `c1` | Visa's income tax expense in Q1 FY2022 was $938.0 million. | single company, quarterly |

## `scale_million_billion` (expected: drop)

Change only the scale word (and `display` too, if you want the value check to see it). The text bases test decision E: the four table or prose bases rest on the scale their passage states; `fb-00216 c1` writes its scale inline, in the quote itself.

| `question_id` | `claim_id` | Claim (copied from the trace) | Why this base |
|---|---|---|---|
| `num-0029` | `c1` | Host Hotels & Resorts' stockholders' equity at the end of Q3 2022 was $6.8 billion. | numeric, billion |
| `num-0056` | `c1` | Cisco Systems' capital expenditures for fiscal 2025 were $905.0 million. | numeric, million |
| `fb-00517` | `c1` | Sales of products were $55,893 million in 2022. | text over a table declared `(Dollars in millions)` |
| `fb-00438` | `c2` | Operating income was 6,098 million in fiscal 2022 and 5,802 million in fiscal 2021. | text over a table declared `(In millions)`, two values |
| `fb-00216` | `c1` | Our cash and cash equivalents balance is $2.6 billion as of December 31, 2022. | text whose quote writes the scale itself |

## `sign_flip` (expected: drop)

Flip a direction word on a growth claim, the relation (`higher than` to `lower than`) on a comparison whose first-named company is clear, or the sign of a negative figure in the text. A comparison with no bindable subject, or worded with `exceeds`, is kept by design and would count as a miss.

| `question_id` | `claim_id` | Claim (copied from the trace) | Why this base |
|---|---|---|---|
| `num-0166` | `c1` | Newmont's dividends paid decreased 19.0% from FY2022 to FY2023. | `decreased 19.0%`: flip the direction word |
| `num-0135` | `c1` | EchoStar's operating cash flow grew 63.9% from Q1 FY2020 to Q1 FY2021. | `grew 63.9%` |
| `num-0146` | `c1` | AppLovin's operating cash flow grew 14.1% in FY2022, measured from FY2021. | `grew 14.1%` |
| `num-0139` | `c1` | Net cash from operating activities changed by -44.0% from Q1 FY2024 to Q1 FY2025. | signed `-44.0%` with neutral wording: flip the sign |
| `num-0083` | `c1` | Robinhood Markets, Inc.'s basic EPS for FY2022 was -$1.17 per share. | negative EPS, `-$1.17` |
| `num-0220` | `c3` | Costco Wholesale's total liabilities were $33.8 billion higher than Carvana's at the end of FY2022. | comparison `higher than`: flip to `lower than` |

## `absent_quote` (expected: drop)

Text claims with no digits, so only the quote rule can drop them. **Keep `chunk_id`** and replace the quote with words that are not in that passage (a paraphrase is the realistic case). A quote cut through a number (decision B) needs a base whose quote has one, such as `fb-00216 c1`.

| `question_id` | `claim_id` | Claim (copied from the trace) | Why this base |
|---|---|---|---|
| `fb-00603` | `c1` | Ulta Beauty continues to experience elevated levels of inventory shrink relative to historical levels. | one sentence, narrative |
| `fb-00711` | `c2` | Inventories are stated at the lower of cost or net realizable value under the first-in, first-out method. | an accounting-policy sentence |
| `fb-00735` | `c1` | Environmental remediation and indemnification obligations have not had, and are not expected to have, a material impact on capital expenditures, earnings or competitive position. | a materiality statement |
| `nar-002` | `c1` | People & Places Solutions delivers solutions as standalone professional service engagements, comprehensive program management partnerships, and selective progressive design-build and construction management at-risk delivery services. | a business description |
| `nar-025` | `c1` | Chesapeake determines the fair value of its derivative instruments using established index prices, volatility curves and discount factors, and compares these estimates to counterparty valuations for reasonableness. | a method description |

## `changed_number` (expected: drop)

Change one digit in the claim text only, by more than the written precision (values are compared, so a change inside the rounding is kept). Numeric bases test `numeric.py`; text bases test the number-in-quote rule.

| `question_id` | `claim_id` | Claim (copied from the trace) | Why this base |
|---|---|---|---|
| `num-0057` | `c1` | Visa's income tax expense in Q1 FY2022 was $938.0 million. | numeric, `$938.0 million` |
| `num-0093` | `c1` | NVR Inc.'s basic earnings per share for Q1 of fiscal year 2023 was $106.31 per share. | numeric, EPS |
| `num-0175` | `c1` | Yum Brands' income tax expense growth for Q1 FY2024, measured from Q1 FY2023, was -2.8%. | numeric growth, `-2.8%` |
| `fb-00499` | `c1` | In 2022, 3M expended approximately $317 million on capital projects for environmental purposes. | text, one figure |
| `fb-04672` | `c1` | Net property, plant and equipment totaled $8.7 billion as of December 31, 2018. | text, one figure with a date |
| `nar-003` | `c1` | Net cash used in financing activities was $295.6 million in 2020 compared with $733.6 million during 2019. | text, two figures: change one |

## `formula_not_built` (expected: drop)

Replace `provenance` (whole) with an expression the compiler did not build, **using fact ids the trace saved** (`--show` lists a claim's facts; `--check` rejects any other id). The strongest rows keep the arithmetic right.

| `question_id` | `claim_id` | Claim (copied from the trace) | Why this base |
|---|---|---|---|
| `num-0202` | `c1` | McKesson Corp generated $1.2 billion more net cash from operating activities than TE Connectivity plc in FY2023. | difference over two saved facts: reverse or recombine them |
| `num-0220` | `c3` | Costco Wholesale's total liabilities were $33.8 billion higher than Carvana's at the end of FY2022. | difference: the same facts in another expression |
| `num-0140` | `c1` | Interest expense changed by -0.3% from FY2023 to FY2024. | growth over two facts: divide by the other one |
| `num-0154` | `c1` | WEC Energy Group's operating income growth from FY2022 to FY2023 was -0.8%. | growth over two facts |
| `num-0172` | `c1` | Cardinal Health Inc's dividends paid changed by -5.0% in FY2024, measured from FY2023. | growth over two facts |

## Your 2 semantic rows (expected: keep, the documented limit)

Real over-claims the judge marked **partial**; leave `change` empty. Neither `fb-01328` claim is suitable: `c1` should be dropped for its unquoted "0", and `c2` for writing "411" over a table in millions.

| `question_id` | `claim_id` | Claim (copied from the trace) | Why this base |
|---|---|---|---|
| `fb-00723` | `c1` | Operating expenses increased 24 percent year-over-year in 2022, driven by net losses on Amex Ventures equity investments. | `added_cause`: one driver stated where the passage gives more |
| `fb-00651` | `c1` | The 2023 guidance reflects increased growth of 12.5% at the mid-point. | `invalid_inference`: drops which EPS grew 12.5% |

## Controls (`correct`, expected: keep)

Add 2 or 3, with an empty `change`. Without them, a verifier that drops too much looks good. These are cases a strict rule could wrongly drop.

| `question_id` | `claim_id` | Claim (copied from the trace) | Why this base |
|---|---|---|---|
| `fb-00517` | `c2` | Sales of services were 10,715 million in 2022. | scale declared only in the table header (decision E) |
| `num-0083` | `c1` | Robinhood Markets, Inc.'s basic EPS for FY2022 was -$1.17 per share. | a negative per-share value stored as `USD` |
| `num-0166` | `c1` | Newmont's dividends paid decreased 19.0% from FY2022 to FY2023. | an unsigned magnitude with `decreased` |
| `fb-00494` | `c2` | The 787 program increased the production rate to 5 per month beginning in October 2023. | a product number that is not a value |

## Notes before writing

- A `unit`-only change has no category of its own. The unit rules are covered by unit tests.
- Do not use `num-0213 c1` as a base: it has no unit, so the verifier drops it unchanged.
- A change to `display` alone can be missed by design: D1 gives the display 0.5% tolerance.
- Aim for 15 to 20 rows in total. Several bases may appear in two categories (`num-0056 c1`, `num-0057 c1`, `num-0166 c1`, `num-0083 c1`).
- Write the rows before reading the verifier's results on them. If a rule changes after you see a score, report that score as in-sample.
- When the file is in place, run `make gold-checksums`; `make check-gold` fails until then.
