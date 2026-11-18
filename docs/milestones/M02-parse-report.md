# M2 parse report (measured by `eval/parse_report.py`)

Numbers below are copied from `docs/milestones/M02-parse-metrics.json` (seed 20261117).
They describe the 135 filings fetched into `data/raw/filings`, NOT the full
S&P 500 target list: the full fetch and parse are NOT MEASURED (cloud sprint).

## Parse status

| form | status | filings |
|---|---|---:|
| 10-K | ok | 32 |
| 10-Q | ok | 61 |
| 10-Q | partial | 42 |

Chunks: 38922 (15062 table chunks, 12971 cited by element id). Max tokens 400; chunks over 400: 0.

| tokens | chunks |
|---|---:|
| 1-50 | 3324 |
| 51-100 | 4799 |
| 101-150 | 3939 |
| 151-200 | 3141 |
| 201-250 | 2812 |
| 251-300 | 2057 |
| 301-350 | 2717 |
| 351-400 | 16133 |

## Filings that are not `ok`

| cik | form | status | detail | filings |
|---:|---|---|---|---:|
| 19617 | 10-Q | partial | missing sections: P1-1, P1-2 | 21 |
| 1067983 | 10-Q | partial | missing sections: P1-2 | 21 |

## FinanceBench PDFs

84 documents cited by 150 questions: {'ok': 84}; 11948 pages with text; 29620 chunks; max tokens 400. CIK: placeholder -1 (extraction only; real CIKs: data/financebench_companies.csv).
Evidence page convention (measured): {'evidence_page_num_is_0_based_index': 189}.

## 20 random chunks (to read; seed 20261117)

### `0000019617-24-000225#1A#0025` (352 tokens)

```text
If a client for which JPMorgan Chase provides these services becomes bankrupt or insolvent, JPMorgan Chase may incur losses, become involved in disputes and litigation with one or more CCPs, the client’s bankruptcy estate and other creditors, or be subject to regulatory investigations. All of the foregoing events can increase JPMorgan Chase’s operational and litigation costs, and JPMorgan Chase may suffer losses to the extent that any collateral that it has received is insufficient to cover thos
```

### `0000019617-21-000236#15#0296` (110 tokens, table)

```text
Ratings profile of derivative receivables
2020(a) | 2019(a)
December 31, (in millions, except ratios) | Exposure net of collateral | % of exposure net of collateral | Exposure net of collateral | % of exposure net of collateral
Investment-grade | $37,013 | 63% | $27,851 | 80%
Noninvestment-grade | 21,789 | 37 | 7,026 | 20
Total | $58,802 | 100% | $34,877 | 100%
```

### `0000320193-20-000096#8#0032` (67 tokens, table)

```text
2020 | 2019
Cash and cash equivalents | $38,016 | $48,844
Restricted cash included in other current assets | 36 | 23
Restricted cash included in other non-current assets | 1,737 | 1,357
Cash, cash equivalents and restricted cash | $39,789 | $50,224
```

### `0000019617-25-000270#15#0502` (338 tokens, table)

```text
Free-standing derivative receivables and payables(a)
Gross derivative receivables | Gross derivative payables
December 31, 2024 (in millions) | Not designated as hedges | Designated as hedges | Total derivative receivables | Net derivative receivables(b) | Not designated as hedges | Designated as hedges | Total derivative payables | Net derivative payables(b)
Trading assets and liabilities
Interest rate | $290,734 | $— | $290,734 | $24,945 | $274,226 | $2 | $274,228 | $9,239
Credit | 11,087 | — 
```

### `0000019617-25-000421#P2-6#0399` (196 tokens, table)

```text
March 31, 2025
(in millions) | Gross amounts | Amounts netted on the Consolidated balance sheets | Amounts presented on the Consolidated balance sheets | Amounts not nettable on the Consolidated balance sheets(b) | Net amounts(c)
Assets
Securities purchased under resale agreements | $701,851 | $(272,345) | $429,506 | $(419,849) | $9,657
Securities borrowed | 285,230 | (46,528) | 238,702 | (187,332) | 51,370
Liabilities
Securities sold under repurchase agreements | $797,949 | $(272,345) | $525,60
```

### `0000950170-25-025210#8#0075` (117 tokens, table)

```text
Origination Year
2024 | 2023 | 2022 | 2021 | 2020 | Prior | Total
Performing | $6,578 | $4,969 | $3,651 | $3,052 | $2,358 | $8,172 | $28,780
Non-performing | 13 | 22 | 16 | 11 | 10 | 45 | 117
$6,591 | $4,991 | $3,667 | $3,063 | $2,368 | $8,217 | $28,897
```

### `0000950170-23-054855#P1-1#0046` (74 tokens)

```text
Gains (losses), net of tax, on derivative instruments recognized in our consolidated comprehensive income statements were as follows:
(In millions)
Three Months Ended September 30, 2023 2022
Designated as Cash Flow Hedging Instruments
Foreign exchange contracts
Included in effectiveness assessment $(15) $(40)
NOTE 6 — INVENTORIES
The components of inventories were as follows:
```

### `0000019617-25-000421#P2-6#0497` (352 tokens)

```text
(a)Includes securities from U.S. GSEs and Ginnie Mae that are generally sold shortly after receipt or retained as part of the Firm’s investment securities portfolio.
(b)Included in level 2 assets.
(c)Excludes the value of MSRs retained upon the sale of loans.
(d)Gains/(losses) on loan sales include the value of MSRs.
(e)The carrying value of the loans accounted for at fair value approximated the proceeds received upon loan sale.
Options to repurchase delinquent loans
In addition to the Firm’s ob
```

### `0000950170-25-025210#8#0158` (399 tokens)

```text
* U.S. income tax credits derive primarily from production tax credits associated with wind-energy generation of BHE and tax credits arising from affordable housing investments.
We file income tax returns in the U.S. and in state, local and foreign jurisdictions. We have settled income tax liabilities with the U.S. federal taxing authority (“IRS”) for tax years through 2013, and the IRS is currently auditing tax years 2014 through 2019. We are also under audit or subject to audit with respect to
```

### `0001564590-20-019706#P2-1A#0029` (365 tokens)

```text
Our product distribution system relies on an extensive partner and retail network. OEMs building devices that run our software have also been a significant means of distribution. The impact of economic conditions on our partners, such as the bankruptcy of a major distributor, OEM, or retailer, could cause sales channel disruption.
Challenging economic conditions also may impair the ability of our customers to pay for products and services they have purchased. As a result, allowances for doubtful
```

### `0000019617-22-000272#15#0475` (398 tokens)

```text
(a)Excludes the current carrying values of the guarantee liability and the offsetting asset, each of which is recognized at fair value at the inception of the guarantees.
(b)Includes the wholesale allowance for lending-related commitments.
The Firm does not estimate the fair value of consumer off-balance sheet lending-related commitments. In many cases, the Firm can reduce or cancel these commitments by providing the borrower notice or, in some cases as permitted by law, without notice. Refer to
```

### `0000019617-20-000372#P2-1A#0342` (68 tokens, table)

```text
(in billions) | June 30, 2020 | December 31, 2019
Collateral permitted to be sold or repledged, delivered, or otherwise used | $1,282.3 | $1,282.5
Collateral sold, repledged, delivered or otherwise used(a) | 985.3 | 1,000.5
```

### `0000950170-25-100235#7#0027` (115 tokens)

```text
If our customers choose to license cloud-based versions of our products and services rather than licensing transaction-based products and services, the associated revenue will shift from being recognized at the time of the transaction to being recognized over the subscription period or upon consumption, as applicable. Refer to Note 12 – Unearned Revenue of the Notes to Financial Statements (Part II, Item 8 of this Form 10-K).
Material Cash Requirements and Other Obligations
Contractual Obligatio
```

### `0000950170-23-035122#7#0010` (344 tokens)

```text
•
Gross margin percentage increased slightly. Excluding the impact of the change in accounting estimate, gross margin percentage decreased 1 point driven by declines in Intelligent Cloud and More Personal Computing, offset in part by sales mix shift between our segments.
•
Microsoft Cloud gross margin percentage increased 2 points to 72%. Excluding the impact of the change in accounting estimate, Microsoft Cloud gross margin percentage decreased slightly driven by a decline in Azure and other cl
```

### `0000950170-23-004451#8#0134` (209 tokens, table)

```text
Average Annual Percentage Payout of Incurred Losses by Age, Net of Reinsurance
In Year | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10
GEICO Physical Damage | 97% | 3%
GEICO Auto Liability | 41% | 31% | 13% | 8% | 4%
BH Primary Medical Professional Liability | 2% | 7% | 12% | 14% | 13% | 11% | 8% | 6% | 4% | 3%
BH Primary Workers’ Compensation and Other Casualty | 15% | 19% | 15% | 13% | 9% | 5% | 4% | 3% | 1% | 1%
BHRG Property | 22% | 36% | 15% | 8% | 4% | 4% | 2% | 1% | 1% | 0%
BHRG Casualty | 14% |
```

### `0001564590-21-002316#P1-1#0032` (184 tokens, table)

```text
(In millions) | Fair Value Level | Adjusted Cost Basis | Unrealized Gains | Unrealized Losses | Recorded Basis | Cash and Cash Equivalents | Short-term Investments | Equity Investments
December 31, 2020
Changes in Fair Value Recorded in Other Comprehensive Income
Changes in Fair Value Recorded in Net Income
Equity investments | Level 1 | $1,655 | $897 | $0 | $758
Equity investments | Other | 3,036 | 0 | 0 | 3,036
Total equity investments | $4,691 | $897 | $0 | $3,794
Cash | $5,565 | $5,565 | $0 
```

### `0000019617-23-000310#P2-6#0401` (383 tokens)

```text
(a)Represents the amount of portfolio layer method basis adjustments related to AFS securities hedged in a closed portfolio. Under U.S. GAAP portfolio layer method basis adjustments are not allocated to individual securities, however the amounts impact the unrealized gains or losses for the individual securities being hedged. Refer to Note 1 and Note 4 for additional information.
(b)The Firm purchased $3.6 billion and $13.2 billion of HTM securities for the three months ended March 31, 2023 and 
```

### `0001564590-22-002324#P1-1#0033` (174 tokens, table)

```text
(In millions) | Fair Value Level | Adjusted Cost Basis | Unrealized Gains | Unrealized Losses | Recorded Basis | Cash and Cash Equivalents | Short-term Investments | Equity Investments
June 30, 2021
Changes in Fair Value Recorded in Other Comprehensive Income
Changes in Fair Value Recorded in Net Income
Equity investments | Level 1 | $1,582 | $976 | $0 | $606
Equity investments | Other | 5,378 | 0 | 0 | 5,378
Total equity investments | $6,960 | $976 | $0 | $5,984
Cash | $7,272 | $7,272 | $0 | $0
```

### `0001018724-24-000083#P1-1#0006` (28 tokens)

```text
See accompanying notes to consolidated financial statements.
AMAZON.COM, INC.
CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME
(in millions)
(unaudited)
```

### `0001564590-20-034944#8#0073` (172 tokens, table)

```text
(In millions) | June 30, 2018 | Acquisitions | Other | June 30, 2019 | Acquisitions | Other | June 30, 2020
Productivity and Business Processes | $23,823 | $514 | $(60) | $24,277 | $7 | $(94) | $24,190
Intelligent Cloud | 5,703 | 5,605 | (a) | 43 | (a) | 11,351 | 1,351 | (5) | 12,697
More Personal Computing | 6,157 | 289 | (48) | 6,398 | 96 | (30) | 6,464
Total | $35,683 | $6,408 | $(65) | $42,026 | $1,454 | $(129) | $43,351
```
