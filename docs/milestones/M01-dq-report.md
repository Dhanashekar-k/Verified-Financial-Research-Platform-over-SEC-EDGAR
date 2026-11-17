# M1 data-quality report

Generated 2026-11-17T18:38:32+00:00 at git `ec6ff43`. Every number below comes from a query run by `pipelines/spark/dq_checks.py` (or from `M01-spotcheck.json`); `NOT MEASURED` means it could not be computed yet, and the reason is given.

Iceberg snapshot ids (latest): `lake.raw.fsds_num` = 3480242806109852370, `lake.curated.facts` = 2440871186715124453, `lake.curated.facts_history` = 330196427142494808

## Rows per source quarter (`raw.fsds_*`)

Quarters present: 2022q1 to 2024q1.
Starts at 2009q2: **False**. Missing quarters in range: **['2022q2', '2023q2']**.

| quarter | sub | num | tag | pre |
|---|---:|---:|---:|---:|
| 2022q1 | 7,237 | 3,264,632 | 105,253 | 832,867 |
| 2022q3 | 7,474 | 3,229,151 | 93,528 | 807,465 |
| 2022q4 | 7,280 | 3,543,392 | 95,778 | 806,371 |
| 2023q1 | 6,754 | 3,428,670 | 103,115 | 804,536 |
| 2023q3 | 7,067 | 3,572,434 | 89,580 | 774,797 |
| 2023q4 | 6,882 | 3,699,124 | 88,228 | 769,208 |
| 2024q1 | 6,028 | 3,428,694 | 86,529 | 726,155 |

## Null rates, `raw.fsds_num`

24,166,097 rows.

| column | null rate |
|---|---:|
| `adsh` | 0.00% |
| `tag` | 0.00% |
| `version` | 0.00% |
| `ddate` | 0.00% |
| `qtrs` | 0.00% |
| `uom` | 0.00% |
| `segments` | 48.76% |
| `coreg` | 98.73% |
| `value` | 4.28% |
| `footnote` | 99.83% |
| `src_quarter` | 0.00% |
| `ingested_at` | 0.00% |

## Unit-of-measure mix, `raw.fsds_num`

157 distinct units; top 15:

| uom | rows | share |
|---|---:|---:|
| `USD` | 20,848,963 | 86.27% |
| `shares` | 1,876,536 | 7.77% |
| `pure` | 655,102 | 2.71% |
| `CAD` | 218,357 | 0.90% |
| `EUR` | 184,812 | 0.76% |
| `GBP` | 120,330 | 0.50% |
| `CNY` | 73,032 | 0.30% |
| `BRL` | 38,414 | 0.16% |
| `CHF` | 26,363 | 0.11% |
| `JPY` | 20,468 | 0.08% |
| `AUD` | 16,084 | 0.07% |
| `Rate` | 13,615 | 0.06% |
| `INR` | 8,130 | 0.03% |
| `MXN` | 7,782 | 0.03% |
| `ARS` | 6,937 | 0.03% |

## Custom tags, segments, co-registrants, NULL values (`raw.fsds_num`)

| what | rows | share |
|---|---:|---:|
| custom tag (`version == adsh`) | 2,216,468 | 9.17% |
| segmented (`segments` set) | 12,383,659 | 51.24% |
| co-registrant (`coreg` set) | 307,913 | 1.27% |
| `value` is NULL (dropped from curated) | 1,033,878 | 4.28% |

## Raw `num` rows that `normalize_facts` drops (a row can have more than one cause)

| cause | rows | share |
|---|---:|---:|
| `value` is NULL | 1,033,878 | 4.28% |
| `ddate` is not a valid yyyymmdd date | 0 | 0.00% |
| no `sub` row for the accession | 0 | 0.00% |
| no fiscal year end (`sub.fye` NULL or empty) | 19,300 | 0.08% |
| **dropped for at least one cause** (each row once) | 1,050,644 | 4.35% |

Of 24,166,097 raw rows.

## Companies whose fiscal-year end changed (`sub.fye`)

105 of 8,200 companies have more than one `fye` in the loaded quarters.
- CIK 14930 BRUNSWICK CORP: 0630, 0930, 1231
- CIK 16099 LUBYS INC: 0531, 0831
- CIK 66600 QUAD M SOLUTIONS, INC.: 0930, 1231
- CIK 87802 SCIENTIFIC INDUSTRIES INC: 0630, 1231
- CIK 93676 STARRETT L S CO: 0630, 0930
- CIK 315131 PHX MINERALS INC.: 0930, 1231
- CIK 351998 DATA I/O CORP: 1130, 1231
- CIK 719274 GIGA TRONICS INC: 0331, 1231
- CIK 728387 PERSPECTIVE THERAPEUTICS, INC.: 0630, 1231
- CIK 790273 CONECTISYS CORP: 0930, 1231
- CIK 817720 SYNAPTICS INC: 0630, 1231
- CIK 849146 LIFEVANTAGE CORP: 0630, 0930
- CIK 874292 ADDVANTAGE TECHNOLOGIES GROUP INC: 0930, 1231
- CIK 911216 PALATIN TECHNOLOGIES INC: 0630, 1231
- CIK 915779 DAKTRONICS INC /SD/: 0430, 0731
- CIK 924805 FREEDOM HOLDING CORP.: 0331, 0630
- CIK 1001614 RILEY EXPLORATION PERMIAN, INC.: 0930, 1231
- CIK 1005286 LIFECORE BIOMEDICAL, INC. DE: 0531, 0831
- CIK 1021096 TROIKA MEDIA GROUP, INC.: 0630, 1231
- CIK 1040470 AEHR TEST SYSTEMS: 0531, 1231

## Label-offset distribution (rule 3)

6,489 companies; offsets: {-1: 173, 0: 6315, 1: 1}; non-constant: 6.

## Curated facts (`curated.facts`)

8,250,114 rows covering 8,071,662 distinct facts (fact_key). Custom: 10.81% of rows. With dq flags: 9.59% of rows (790,827). Derived Q4 rows: 784,359.

Flag counts (rows): {'label_offset_varies': 7010, 'q4_derived': 784359}.
Restated facts: 183,653. Versions per fact (`n_versions`, counted once per fact): {1: 5436692, 2: 1797588, 3: 386682, 4: 343823, 5: 53470, 6: 21666, 7: 16550, 8: 6025, 9: 4474, 10: 1883, 11: 1336, 12: 744, 13: 309, 14: 170, 15: 89, 16: 83, 17: 46, 18: 10, 19: 17, 20: 3, 22: 2}.

Rows by fiscal year: 1932: 2, 1988: 1, 1993: 1, 1994: 3, 1996: 2, 1997: 3, 1998: 3, 1999: 2, 2002: 6, 2003: 8, 2004: 7, 2005: 4, 2006: 13, 2007: 16, 2008: 31, 2009: 22, 2010: 34, 2011: 107, 2012: 230, 2013: 207, 2014: 183, 2015: 530, 2016: 703, 2017: 1,050, 2018: 12,050, 2019: 253,553, 2020: 655,959, 2021: 2,147,553, 2022: 2,750,809, 2023: 2,250,520, 2024: 176,468, 2025: 8, 2026: 3, 2027: 5, 2028: 8, 2029: 2, 2032: 1, 2033: 1, 2034: 1, 2923: 5.

Null rates:

| column | null rate |
|---|---:|
| `fact_id` | 0.00% |
| `fact_key` | 0.00% |
| `cik` | 0.00% |
| `tag` | 0.00% |
| `taxonomy` | 0.00% |
| `is_custom` | 0.00% |
| `uom` | 0.00% |
| `qtrs` | 0.00% |
| `period_start` | 26.03% |
| `period_end` | 0.00% |
| `fiscal_year` | 0.00% |
| `fiscal_period` | 0.00% |
| `calendar_frame` | 28.86% |
| `value` | 0.00% |
| `accession` | 0.00% |
| `form` | 0.00% |
| `filed` | 0.00% |
| `is_latest` | 0.00% |
| `is_original` | 0.00% |
| `n_versions` | 0.00% |
| `is_restated` | 0.00% |
| `dq_flags` | 0.00% |
| `derived_from` | 90.49% |
| `anomaly_score` | 100.00% |

## Second-source spot check (companyfacts)

Compared: the 10-Ks of fiscal years 2023, 2022 (each company's own fy label).

58/60 match (outcomes {'match': 58, 'missing_in_curated': 2}). Frames agree: 1/1. Non-matches without a classification: 2. Run at 2026-11-17T13:18:53-05:00; details in `M01-spotcheck.json`.

## Apple FY2023 revenue (manual check against the 10-K income statement)

Period timing note: the 7-day tolerance in rule 2 matters for companyfacts dates, not for FSDS: FSDS rounds `ddate`/`period` to the nearest month end.

**From `raw.fsds_num` (no fiscal logic):**
- `RevenueFromContractWithCustomerExcludingAssessedTax` = 383285000000.0000 USD, accession 0000320193-23-000106

**From `curated.facts` (fiscal_year 2023, FY):**
- `RevenueFromContractWithCustomerExcludingAssessedTax` = 383285000000.0000 USD, accession 0000320193-23-000106

Compare these with the 10-K income statement by hand; this report does not assert a match.
