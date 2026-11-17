# Verified-Financial-Research-Platform-over-SEC-EDGAR

## Reference data snapshots

`curated.companies` is built from two files frozen under `data/` (gitignored; `make fetch-reference` recreates them):

| File | Source | Fetched on |
|---|---|---|
| `data/company_tickers_<date>.json` | `https://www.sec.gov/files/company_tickers.json`, fetched through the shared SEC client | 2026-11-17 |
| `data/sp500_<date>.csv` (`ticker,name`) | the public GitHub dataset `datasets/s-and-p-500-companies` (`constituents.csv`) | 2026-11-17 |

**S&P 500 membership is a single snapshot.** The index changes over time, so `in_sp500` is the
list as fetched on 2026-11-17, not who was a member in each fiscal year of the facts. The
upstream dataset's own as-of date is NOT MEASURED (it is a community-maintained file and can lag
the real index). The fetch dates and the SHA-256 of both files are stored as table properties on
`curated.companies` (`edgarqa.sp500_snapshot_date`, `edgarqa.sp500_sha256`,
`edgarqa.ticker_map_snapshot_date`, `edgarqa.ticker_map_sha256`; see `SHOW TBLPROPERTIES`).
`in_financebench` is NULL until the FinanceBench documents are loaded.
