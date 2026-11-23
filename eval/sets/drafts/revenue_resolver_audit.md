# Revenue / cost-of-revenue resolver audit

482 materially conflicting (cik, period) cases; decisions by reason:

- conflicting_candidates: 434
- reconciles_with_gross_profit: 48

## conflicting_candidates

| company | FY | period | metric | candidates (tag = value, presented) | chosen |
|---|---|---|---|---|---|
| UDR, Inc. | 2025 | Q3 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 2,570,000.0000 (None); `Revenues` = 431,864,000.0000 (None) | ABSTAIN |
| CHEVRON CORP | 2021 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 36,117,000,000.0000 (None); `Revenues` = 37,597,000,000.0000 (None) | ABSTAIN |
| UDR, Inc. | 2024 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 8,317,000.0000 (None); `Revenues` = 1,671,842,000.0000 (None) | ABSTAIN |
| SBA COMMUNICATIONS CORP | 2025 | Q3 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 75,900,000.0000 (None); `Revenues` = 732,327,000.0000 (None) | ABSTAIN |
| UDR, Inc. | 2023 | Q1 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 1,242,000.0000 (None); `Revenues` = 399,549,000.0000 (None) | ABSTAIN |
| GENERAL ELECTRIC CO | 2024 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 8,223,000,000.0000 (None); `Revenues` = 9,094,000,000.0000 (None) | ABSTAIN |
| Extra Space Storage Inc. | 2023 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 22,206,000.0000 (True); `Revenues` = 511,386,000.0000 (True) | ABSTAIN |
| Diamondback Energy, Inc. | 2023 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 1,896,000,000.0000 (True); `Revenues` = 1,919,000,000.0000 (True) | ABSTAIN |
| EQT Corp | 2024 | Q1 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 1,303,905,000.0000 (None); `Revenues` = 1,412,268,000.0000 (None) | ABSTAIN |
| Walmart Inc. | 2022 | Q1 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 140,288,000,000.0000 (None); `Revenues` = 141,569,000,000.0000 (None) | ABSTAIN |
| Robinhood Markets, Inc. | 2020 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 720,133,000.0000 (None); `Revenues` = 958,833,000.0000 (None) | ABSTAIN |
| CHEVRON CORP | 2024 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 193,414,000,000.0000 (None); `Revenues` = 202,792,000,000.0000 (None) | ABSTAIN |
| LyondellBasell Industries N.V. | 2020 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 5,371,000,000.0000 (None); `Revenues` = 5,546,000,000.0000 (None) | ABSTAIN |
| Walmart Inc. | 2021 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 139,871,000,000.0000 (None); `Revenues` = 141,048,000,000.0000 (None) | ABSTAIN |
| UDR, Inc. | 2021 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 6,102,000.0000 (True); `Revenues` = 1,290,767,000.0000 (True) | ABSTAIN |
| CHEVRON CORP | 2023 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 47,216,000,000.0000 (True); `Revenues` = 48,896,000,000.0000 (True) | ABSTAIN |
| CHEVRON CORP | 2025 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 44,375,000,000.0000 (None); `Revenues` = 44,822,000,000.0000 (None) | ABSTAIN |
| Diamondback Energy, Inc. | 2020 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 2,756,000,000.0000 (None); `Revenues` = 2,813,000,000.0000 (None) | ABSTAIN |
| DEVON ENERGY CORP/DE | 2020 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 4,673,000,000.0000 (None); `Revenues` = 4,828,000,000.0000 (None) | ABSTAIN |
| ESSEX PROPERTY TRUST, INC. | 2025 | Q3 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 2,361,000.0000 (None); `Revenues` = 473,303,000.0000 (None) | ABSTAIN |

## reconciles_with_gross_profit

| company | FY | period | metric | candidates (tag = value, presented) | chosen |
|---|---|---|---|---|---|
| Viatris Inc | 2020 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 0.0000 (None); `Revenues` = 2,731,200,000.0000 (None) | Revenues |
| STERIS plc | 2023 | Q3 | cost_of_revenue | `CostOfRevenue` = 793,052,000.0000 (None); `CostOfGoodsAndServicesSold` = 737,698,000.0000 (None) | CostOfRevenue |
| Viatris Inc | 2021 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 17,813,600,000.0000 (True); `Revenues` = 17,886,300,000.0000 (True) | Revenues |
| STEEL DYNAMICS INC | 2021 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 18,376,743,000.0000 (True); `Revenues` = 18,408,850,000.0000 (True) | Revenues |
| Viatris Inc | 2024 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 14,692,800,000.0000 (None); `Revenues` = 14,739,300,000.0000 (None) | Revenues |
| Viatris Inc | 2022 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 16,218,100,000.0000 (True); `Revenues` = 16,262,700,000.0000 (True) | Revenues |
| Viatris Inc | 2020 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 11,819,900,000.0000 (None); `Revenues` = 11,946,000,000.0000 (None) | Revenues |
| STERIS plc | 2022 | Q2 | cost_of_revenue | `CostOfRevenue` = 668,182,000.0000 (None); `CostOfGoodsAndServicesSold` = 608,627,000.0000 (None) | CostOfRevenue |
| Viatris Inc | 2020 | Q3 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 0.0000 (None); `Revenues` = 2,972,100,000.0000 (None) | Revenues |
| INSULET CORP | 2022 | Q3 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 281,900,000.0000 (True); `Revenues` = 340,800,000.0000 (True) | Revenues |
| Viatris Inc | 2021 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 4,561,700,000.0000 (None); `Revenues` = 4,577,800,000.0000 (None) | Revenues |
| Viatris Inc | 2022 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 4,105,400,000.0000 (True); `Revenues` = 4,116,800,000.0000 (True) | Revenues |
| Viatris Inc | 2021 | Q3 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 4,520,500,000.0000 (None); `Revenues` = 4,536,600,000.0000 (None) | Revenues |
| INSULET CORP | 2023 | Q1 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 261,300,000.0000 (None); `Revenues` = 358,100,000.0000 (None) | Revenues |
| STEEL DYNAMICS INC | 2021 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 4,453,556,000.0000 (None); `Revenues` = 4,465,308,000.0000 (None) | Revenues |
| Viatris Inc | 2024 | Q2 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 3,785,900,000.0000 (None); `Revenues` = 3,796,600,000.0000 (None) | Revenues |
| STERIS plc | 2023 | Q1 | cost_of_revenue | `CostOfRevenue` = 711,081,000.0000 (None); `CostOfGoodsAndServicesSold` = 654,382,000.0000 (None) | CostOfRevenue |
| STEEL DYNAMICS INC | 2020 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 9,587,691,000.0000 (None); `Revenues` = 9,601,482,000.0000 (None) | Revenues |
| Viatris Inc | 2025 | FY | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 14,250,400,000.0000 (None); `Revenues` = 14,299,900,000.0000 (None) | Revenues |
| Viatris Inc | 2025 | Q3 | revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` = 3,747,500,000.0000 (None); `Revenues` = 3,759,900,000.0000 (None) | Revenues |
