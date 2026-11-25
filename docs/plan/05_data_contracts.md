# 05: Data contracts (inputs, tables, API, eval files)

Keep this open while building. Contracts are what Claude Code builds against, so if you change one, change it here first, then in code, then add an ADR.

Sample values marked *(illustrative)* show the shape. Verify real values against your data in M1. Apple (CIK 320193) is the running example because the M1 spot check uses it.

---

## 1. Inputs

### 1.1 Financial Statement Data Sets (FSDS), the big-data input

- **Where:** `https://www.sec.gov/files/dera/data/financial-statement-data-sets/{YYYY}q{N}.zip` (2009q2 onward; **2009q1 is headers only**) [Certain]. Check the SEC page for the latest quarter released.
- **What changed:** in **December 2024** the data sets were reprocessed to include only submissions and numeric data **from the primary financial statements**, and a **`segments` field was added to NUM** [Certain]. Older "archive" versions exist; use the current ones.
- **Format:** each ZIP holds `sub.txt`, `num.txt`, `tag.txt`, `pre.txt` (+ `readme.htm`). They're UTF-8, tab-delimited, with a header row of lowercase column names [Certain].
- **NUM key** (per the SEC docs): `adsh, tag, version, ddate, qtrs, uom, segments, coreg` [Certain for most fields; read the bundled readme for the exact current list].

| Table | Columns you'll use | Notes |
|---|---|---|
| `sub` (one row per submission) | `adsh, cik, name, sic, countryba, fye, form, period, fy, fp, filed, accepted, prevrpt, afs, wksi, instance` | `fye` = fiscal-year-end MMDD; `period` = balance-sheet date (yyyymmdd); `fy/fp` describe the **filing**, not each fact |
| `num` (every numeric fact) | `adsh, tag, version, ddate, qtrs, uom, segments, coreg, value, footnote` | `ddate` = period end yyyymmdd; `qtrs` 0 = instant, 1 = quarter, 2–3 = YTD, 4 = annual; `version` = taxonomy (e.g. `us-gaap/2023`) or the adsh for custom tags |
| `tag` (tag definitions) | `tag, version, custom, abstract, datatype, iord, crdr, tlabel, doc` | `tlabel` gives labels for concept-mapping synonyms |
| `pre` (where facts sit in statements) | `adsh, report, line, stmt, inpth, tag, version, plabel, negating` | `stmt` ∈ {BS, IS, CF, EQ, CI, …}; `plabel` = the filer's own label |

*(illustrative)* `num.txt` rows:
```
adsh                  tag                                              version       ddate     qtrs uom segments coreg value          footnote
0000320193-23-000106  RevenueFromContractWithCustomerExcludingAssessedTax us-gaap/2023 20230930  4    USD                    383285000000.0000
0000320193-23-000106  RevenueFromContractWithCustomerExcludingAssessedTax us-gaap/2023 20220930  4    USD                    394328000000.0000
0000320193-23-000106  Assets                                           us-gaap/2023  20230930  0    USD                    352583000000.0000
```
Note the second row: the FY2023 10-K also reports **FY2022** (a comparative). Your fiscal-period logic must assign that row to FY2022, not FY2023.

**On-disk layout written by `pipelines/fetch/download_fsds.py`** (local `--dest`, default `data/raw/fsds`; S3 is not supported yet):
```
{dest}/{q}/sub.txt.gz  num.txt.gz  tag.txt.gz  pre.txt.gz  readme.htm
{dest}/_manifest.json
```
The `.gz` files are deterministic (no name or mtime in the header). `_manifest.json` has one key per quarter, and the keys are sorted:
```json
{"2023q3": {"source_url": "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2023q3.zip",
            "zip_bytes": 0, "zip_sha256": "<hex>", "downloaded_at": "<ISO-8601 UTC>",
            "files": {"sub": {"bytes": 0, "sha256": "<hex>"}, "num": {...}, "tag": {...}, "pre": {...}}}}
```
`bytes` and `sha256` under `files` describe the **gzipped** file on disk. A quarter is "complete" (and skipped on rerun) when its entry is well-formed and all four `.gz` files exist with the recorded `bytes`. `downloaded_at` is the only non-deterministic field. A manifest that cannot be parsed is moved aside to `_manifest.json.corrupt-<timestamp>`, never overwritten.

### 1.2 Company Facts (cross-check and daily refresh)

- **Per company:** `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` (CIK zero-padded to 10 digits) [Likely]. Bulk: `companyfacts.zip` from the EDGAR APIs page.
- **Shape** *(illustrative)*:
```json
{"cik": 320193, "entityName": "Apple Inc.",
 "facts": {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {
   "label": "Revenue from Contract with Customer, Excluding Assessed Tax",
   "units": {"USD": [
     {"start": "2022-09-25", "end": "2023-09-30", "val": 383285000000,
      "accn": "0000320193-23-000106", "fy": 2023, "fp": "FY", "form": "10-K",
      "filed": "2023-11-03", "frame": "CY2023"}]}}}}}
```
- `frame` (for example `CY2023`, `CY2023Q3`, `CY2023Q3I` for instants) is SEC's calendar-aligned de-duplicated view. Store it; it's useful for cross-company comparisons [Likely].

### 1.3 Submissions (which documents to fetch)

- `https://data.sec.gov/submissions/CIK##########.json` → `filings.recent.{accessionNumber, form, filingDate, reportDate, primaryDocument}` arrays, plus `filings.files` for older pages [Likely].
- **Document URL:** `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primaryDocument}` *(illustrative: `.../320193/000032019323000106/aapl-20230930.htm`)*.

### 1.4 Daily index (incremental)

- `https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{n}/master.{YYYYMMDD}.idx`, pipe-delimited `CIK|Company Name|Form Type|Date Filed|Filename` after a header block [Likely; confirm the format in M10]. Filter forms `10-K, 10-K/A, 10-Q, 10-Q/A` and tracked CIKs.

### 1.5 Ticker map and S&P 500 list

- `https://www.sec.gov/files/company_tickers.json` → `{"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}` [Likely].
- S&P 500 constituents: any public list, frozen to a dated CSV `data/sp500_2026-11-16.csv` (ticker, name). Membership changes over time; say in the README that you used a single snapshot.

### 1.6 FinanceBench (open sample)

- Hugging Face `PatronusAI/financebench`: **150 annotated questions**, license **CC-BY-NC-4.0**; source PDFs in the GitHub repo `patronus-ai/financebench` under `pdfs/` [Certain].
- Fields you'll use: `financebench_id, company, doc_name, question, answer, evidence[] (evidence_text, page), question_type, question_reasoning` [Likely; verify the JSONL keys].
- Measured 2026-11-17 on all 150 questions (189 evidence items): the evidence keys are `evidence_text`, `evidence_page_num` and `evidence_text_full_page`, and `evidence_page_num` is the **0-based** PDF page index (189/189 found the text on that index). `curated.chunks.page` for PDFs is **1-based** (PyMuPDF index + 1, the number a PDF viewer shows and `#page=N` uses), so a gold `page` converts as `evidence_page_num + 1`.
- The non-commercial license is fine for a portfolio. Attribute it in the README, and don't use it in anything sold.

### 1.7 SEC access rules (shared HTTP client, M1)

- **User-Agent** with your name and contact email on every request; **≤ 10 requests/second** in total across all your machines [Certain]. Run at **≤ 8 req/s** for headroom.
- Send `Accept-Encoding: gzip, deflate`; retry 429/5xx with exponential backoff + jitter and honor `Retry-After`; cap concurrency.
- Configure it as `SEC_USER_AGENT="Dhanashekar Kandaswamy <your-email>"` in `.env`. Never hard-code it.

---

## 2. Lakehouse tables (Iceberg; Glue database = namespace)

### 2.1 Raw

```sql
-- Spark SQL; catalog name `lake`
CREATE TABLE lake.raw.fsds_sub (
  adsh string, cik int, name string, sic int, countryba string, fye string, form string,
  period int, fy int, fp string, filed int, accepted timestamp, prevrpt int, afs string,
  wksi int, instance string, src_quarter string, ingested_at timestamp
) USING iceberg PARTITIONED BY (src_quarter);

CREATE TABLE lake.raw.fsds_num (
  adsh string, tag string, version string, ddate int, qtrs int, uom string,
  segments string, coreg string, value decimal(28,4), footnote string,
  src_quarter string, ingested_at timestamp
) USING iceberg PARTITIONED BY (src_quarter);
-- fsds_tag and fsds_pre: mirror the source columns + src_quarter, ingested_at
-- fsds_sub keeps 16 of the file's 36 columns. `accepted` is US Eastern wall-clock time as EDGAR writes it,
-- stored as-is (not converted to UTC); `period`, `filed`, `ddate` are yyyymmdd ints, month-end rounded for ddate/period.

CREATE TABLE lake.raw.filings_html (          -- metadata only; the HTML itself lives in S3
  accession string, cik int, form string, filed date, report_date date, url string,
  s3_uri string, bytes bigint, sha256 string, http_status int, fetched_at timestamp
) USING iceberg PARTITIONED BY (form);
-- One row per target filing, merged on accession. `report_date` is the submissions API's reportDate
-- (curated.filings.period_of_report); NULL if the SEC gave none. `bytes` and `sha256` describe the STORED object
-- (the gzip at s3_uri). A failed fetch is a row too: http_status set (NULL if no response) and
-- s3_uri, bytes, sha256 NULL; it is retried on the next fetch run. Loaded from the fetcher's
-- `_manifest.jsonl` by pipelines/spark/load_filings_html.py.
```

### 2.2 Curated

```sql
CREATE TABLE lake.curated.companies (
  cik int, name string, ticker string, sic int, fye_mmdd string,
  in_sp500 boolean, in_financebench boolean, aliases array<string>
) USING iceberg;

CREATE TABLE lake.curated.facts_history (       -- every reported version of every fact
  fact_id bigint,             -- xxhash64(fact_key, accession): unique per version (same as facts.fact_id). A derived Q4 (derived_from not null) uses xxhash64(fact_key, accession, nine_month_accession) instead, so it can never equal a real fact's fact_id (ADR-022)
  fact_key bigint,            -- xxhash64(cik, tag, taxonomy, period_end, qtrs, uom); taxonomy is the year-stripped family (see below)
  cik int, tag string, taxonomy string, is_custom boolean, uom string, qtrs int,
  period_start date, period_end date,
  fiscal_year int, fiscal_period string,       -- FY | Q1..Q4 | H1 | 9M; an instant (qtrs = 0) is FY at the year end, else Q1..Q3
  calendar_frame string,                       -- e.g. CY2023, CY2023Q3, CY2023Q3I; the trailing I (with qtrs = 0) marks an instant
  value decimal(28,4),
  accession string, form string, filed date, is_amendment boolean,
  dq_flags array<string>,     -- same meaning as in facts; carried here so rebuilding facts from history keeps them
  source string,              -- 'fsds' in M1. 'companyfacts' is reserved for the M10 incremental path; M1 uses companyfacts only as a cross-check
  derived_from array<bigint>  -- null unless this row is a derived Q4: then [fact_id of the FY fact, fact_id of the 9M fact]
) USING iceberg PARTITIONED BY (fiscal_year);
-- Consolidated facts only (coreg IS NULL and segments null/empty). Segmented rows go to facts_dimensional.
-- taxonomy = the prefix of FSDS `version` with the year stripped: 'us-gaap/2023' -> 'us-gaap', 'dei/2023' -> 'dei';
-- 'custom' when version == adsh. The year is NOT part of fact_key: restating a filing under a newer taxonomy year
-- must land on the same fact_key as the original, otherwise a fact and its restatement would never be grouped.
-- Derived Q4 rows (M01 §3.5 rule 5; flow concepts only, never instants, nor per-share ('/'), 'pure' or 'shares' units):
--   source = 'fsds', form and accession = those of the FY parent row, which must come from a 10-K-family filing
--   (form in '10-K', '10-KT', '10-K/A'; ADR-022): an FY (qtrs = 4) row of any other form, such as a 10-Q's
--   trailing-twelve-month figure or a registration statement, is never a parent. The 9M parent may have any form.
--   fact_id = xxhash64(fact_key, accession, nine_month_accession): a derived row can never equal a real fact's fact_id.
--   qtrs = 1, period_end = the FY end, fiscal_period = 'Q4', dq_flags contains 'q4_derived',
--   derived_from = [FY fact_id, 9M fact_id] so M6 can re-execute FY - 9M.
--   Derived per provenance tier within (cik, tag, taxonomy, uom, fiscal_year): the original Q4 from the original FY and 9M,
--   the latest Q4 from the latest FY and 9M. derived_from points only at parent rows that `facts` retains (a parent whose
--   versions all carry one value is retained as its ORIGINAL version, see `facts`). Ties between candidate parents go to the
--   latest filed (the original tier: earliest filed); a tie on (filed, accession) goes to the row that sorts first by (value
--   ascending, NULLs last, period_end, qtrs), and 9M period-end groups that still tie go to the group whose first row sorts
--   first (ADR-022: the reference sorts its input by accession, value, period_end, ... before any tie-sensitive step).
--   Tiers that land on the same fact_id (same FY parent accession, i.e. only the 9M was restated) are deduplicated: the row
--   whose 9M parent was filed latest survives, with is_latest = is_original = true, is_restated = false, n_versions = 1.
--   When the FY itself was restated the tiers have different FY accessions: both rows are kept (latest and original).
--   Their fact_key differs from the FY fact's (qtrs 1 vs 4). A directly reported Q4 (qtrs = 1, fiscal_period Q4) for the same
--   (cik, tag, taxonomy, uom, fiscal_year), in ANY accession, always wins: nothing is derived for that fiscal year, and the
--   direct row does not get 'q4_derived'.

CREATE TABLE lake.curated.facts (               -- latest version per fact_key, plus the original when it differs (M1 §3.5); one row, the ORIGINAL version (the native filing), when all versions share a value
  fact_id bigint,             -- xxhash64(fact_key, accession): unique per version; a derived Q4 uses xxhash64(fact_key, accession, nine_month_accession), see facts_history
  fact_key bigint,            -- groups versions of the same fact; same definition as facts_history.fact_key
  cik int, tag string, taxonomy string, is_custom boolean, uom string, qtrs int,
  period_start date, period_end date, fiscal_year int, fiscal_period string, calendar_frame string,
  value decimal(28,4), accession string, form string, filed date,
  is_latest boolean, is_original boolean,      -- both true on the single collapsed row; that row carries the ORIGINAL version's accession, form and filed, so the default answer cites the native filing
  n_versions int, is_restated boolean,         -- restated = more than one distinct value across versions
  dq_flags array<string>,                      -- e.g. ['fy_mismatch_companyfacts','q4_derived']
  derived_from array<bigint>,                  -- same meaning as in facts_history (derived Q4 only)
  anomaly_score float                          -- filled by S1 (null until then)
) USING iceberg PARTITIONED BY (fiscal_year);
-- Consolidated facts only: coreg IS NULL and segments null/empty. This is the table the plan compiler reads.

CREATE TABLE lake.curated.facts_dimensional (   -- segmented (dimensional) rows, kept out of facts so they can't leak into company totals
  fact_id bigint,             -- xxhash64(fact_key, accession); a derived Q4 uses xxhash64(fact_key, accession, nine_month_accession)
  fact_key bigint,            -- xxhash64(cik, tag, taxonomy, period_end, qtrs, uom, segments)
  cik int, tag string, taxonomy string, is_custom boolean, uom string, qtrs int,
  segments string,            -- FSDS `segments` value, non-empty by definition
  period_start date, period_end date, fiscal_year int, fiscal_period string, calendar_frame string,
  value decimal(28,4), accession string, form string, filed date,
  is_latest boolean, is_original boolean, n_versions int, is_restated boolean,   -- same semantics as facts
  dq_flags array<string>
) USING iceberg PARTITIONED BY (fiscal_year);
-- Rows with segments non-empty and coreg IS NULL. Not loaded into Postgres (M3) until a milestone needs them.
-- Co-registrant rows (coreg IS NOT NULL) are not curated; they stay in raw.fsds_num.

CREATE TABLE lake.curated.filings (
  accession string, cik int, form string, filed date, accepted_at timestamp, period_of_report date,
  fiscal_year int, fiscal_period string, primary_doc_url string, html_s3_uri string,
  parse_status string, parse_error string, n_sections int, n_chunks int, doc_source string,
  dq_flags array<string>
) USING iceberg PARTITIONED BY (fiscal_year);
-- fiscal_year / fiscal_period use the SAME rules as curated.facts (ADR-023), so text and numbers agree:
--   fiscal_year   = y_end(period_of_report, fye_mmdd) + label_offset(cik)    (fiscal_ref.y_end, label_offsets)
--   fiscal_period = 'FY' for 10-K and 10-K/A; Q1..Q3 for 10-Q and 10-Q/A    (fiscal_ref.fiscal_period_label)
-- fye_mmdd is the fsds_sub.fye of the same accession, else curated.companies.fye_mmdd.
-- If period_of_report or fye_mmdd is missing, fiscal_year / fiscal_period fall back to fsds_sub.fy / fp
-- of the same accession. If that is missing too they are NULL. dq_flags (empty array, never NULL):
--   fiscal_from_fsds_sub          labels taken from fsds_sub (the derivation was impossible)
--   fiscal_unresolved             no labels at all (both sources missing)
--   fiscal_differs_from_fsds_sub  derived labels differ from fsds_sub.fy/fp (derived ones are kept)
--   fye_from_companies            fye_mmdd came from curated.companies, not from this accession's sub row
--   label_offset_default          the company has no 10-K in fsds_sub, so offset 0 was assumed (as in facts)
--   label_offset_varies           the company's label offset is not constant (same flag as in facts)
-- PDF documents (FinanceBench): fiscal_year / fiscal_period come from doc_name (e.g. 3M_2018_10K -> 2018, FY).

CREATE TABLE lake.curated.chunks (
  chunk_id string,            -- {accession}#{section}#{chunk_index:04d}   (PDF: fb:{doc_name}#p{page}#{idx})
  cik int, accession string, form string, fiscal_year int, fiscal_period string,
  section string,             -- '1','1A','1C','7','7A','8',... or 'PDF'
  section_title string, chunk_index int, text string, n_tokens int, is_table boolean,
  source_url string, anchor string, page int, doc_source string,   -- edgar_html | financebench_pdf
  corpus_version int
) USING iceberg PARTITIONED BY (doc_source, fiscal_year);

CREATE TABLE lake.curated.chunk_embeddings (
  chunk_id string, model_name string, dim int, embedding array<float>, embedded_at timestamp
) USING iceberg PARTITIONED BY (bucket(16, chunk_id));
-- model_name = 'BAAI/bge-small-en-v1.5', the name the contract and the manifest use. fastembed resolves that name to the
-- QUANTIZED ONNX 'Qdrant/bge-small-en-v1.5-onnx-Q' (model_optimized.onnx), not the fp32 model; the exact artifact is pinned by
-- sha256 in the export manifest (`embedding_artifact`). Vectors are L2-normalized float32, dim 384. EVERY chunk IN THE
-- EMBEDDING SCOPE is embedded, including degenerate ones (n_tokens <= 5), so "missing = 0" stays checkable over that scope;
-- degenerate chunks are dropped only at export (section 2.3).
-- Embedding scope (ADR-037): `form IN ('10-K','10-K/A') OR doc_source = 'financebench_pdf'`. EDGAR 10-Q and 10-Q/A chunks
-- (doc_source = 'edgar_html') are deliberately NOT embedded: serving is 10-K-only and quarterly numbers come from curated.facts.
-- "Complete" therefore means: no in-scope chunk lacks a row here. Adding 10-Q later is an incremental run, not a rebuild.
-- The query side must use the same artifact; fastembed's query_embed adds NO BGE query instruction for this model (query_embed == passage_embed).

CREATE TABLE lake.curated.corpus_versions (     -- one row per serving export (M3); corpus_version = previous max + 1
  corpus_version int, created_at timestamp, git_sha string,
  facts_rows bigint, chunks_rows bigint, chunks_excluded bigint,
  embedding_model string, embedding_artifact_sha256 string, dim int,
  facts_snapshot_id bigint, chunks_snapshot_id bigint, chunk_embeddings_snapshot_id bigint,
  export_uri string
) USING iceberg;
-- Tiny table. The export job appends one row after it has written the Parquet files and _MANIFEST.json (same values as the manifest).
-- Re-exporting version N replaces its row (MERGE on corpus_version), never appends a second one.
```

### 2.3 Exports (lake → serving)

`s3://<lake-bucket>/exports/serving/corpus_version=N/{companies,facts,chunks}/*.parquet` plus `_MANIFEST.json`:
```json
{"corpus_version": 7, "created_at": "2026-12-04T03:12:00Z", "git_sha": "abc1234",
 "facts_rows": 0, "chunks_rows": 0, "embedding_model": "BAAI/bge-small-en-v1.5", "dim": 384,
 "embedding_artifact": {"hf_repo": "Qdrant/bge-small-en-v1.5-onnx-Q", "hf_revision": "<40-hex commit>",
                        "onnx_file": "model_optimized.onnx", "onnx_sha256": "<hex>"},
 "chunk_exclusions": {"rule": "n_tokens <= 5", "count": 0},
 "companies_rows": 0,
 "files": {"companies": [{"path": "companies/part-00000.parquet", "rows": 0, "bytes": 0}], "facts": ["..."], "chunks": ["..."]},
 "filters": {"facts": "<the facts filter, human readable>", "chunks": "<the chunks filter, human readable>"},
 "embedding_scope": {"rule": "form IN ('10-K','10-K/A') OR doc_source = 'financebench_pdf'",
                     "not_embedded": {"rule": "doc_source = 'edgar_html' AND form IN ('10-Q','10-Q/A')", "count": 0,
                                      "reason": "serving is 10-K-only; quarterly numbers come from curated.facts (ADR-037)"}},
 "iceberg_snapshots": {"curated.facts": 0, "curated.chunks": 0, "curated.chunk_embeddings": 0}}
```
Record the Iceberg snapshot IDs. You can then reproduce any eval run against the exact corpus with time travel.
- `files` lists every Parquet file of each table with its footer row count and size (paths relative to the export prefix); `facts_rows`, `chunks_rows` and `companies_rows` equal the sums of those counts, and the loader checks both against the footers before loading. `filters` states the filters that produced the export, so a reader can reproduce it.
- `embedding_artifact` pins the ONNX file that produced the vectors (fastembed's name `BAAI/bge-small-en-v1.5` is the quantized Qdrant build, ADR-032). The loader and any query embedder must use the same file.
- `chunk_exclusions` records chunks that are embedded in the lake but NOT exported. Rule: `n_tokens <= 5` (degenerate, near-empty chunks whose vectors carry no meaning). `count` is the exact number dropped by this export; `chunks_rows` is the count AFTER the exclusion. The count is measured by the export and recorded for audit; whoever runs it compares it with the pre-flight count (ADR-035), the job itself does not fail on it.
- `embedding_scope` records the deliberate scope decision: 10-Q and 10-Q/A chunks of `doc_source = 'edgar_html'` are in `curated.chunks` but have no embedding and are not exported, so their absence is a scope decision, not a gap. `not_embedded.count` is the exact number of such chunks in `curated.chunks` at export time (1,759,310 when this was decided: 1,756,066 + 3,244).
- `chunks` exports only `form IN ('10-K','10-K/A')` or `doc_source = 'financebench_pdf'` until a milestone decides to serve 10-Q text; `curated.chunks.corpus_version` is not exported and `tenant_id` is exported as NULL.

---

## 3. Serving store (Postgres 17 + pgvector 0.8.x)

**Runtime (M3 onward).** The serving store runs on a **cloud** Postgres, not on the developer's laptop: the real load, the index builds and the retrieval bench run there. M3 uses Docker `pgvector/pgvector:pg17` on an EC2 `m7i.large` (non-burstable; flex instances throttle under sustained load and would distort build and latency numbers, ADR-034). Docker + Testcontainers on a laptop remain for unit and integration tests only.
**Version requirement (the loader asserts it and fails fast):** PostgreSQL **17**, pgvector **>= 0.8.0**, 0.8.x preferred. Why: `halfvec` needs >= 0.7, HNSW needs >= 0.5, iterative index scans (filtered ANN, `hnsw.iterative_scan`) need >= 0.8. Any managed Postgres used later (RDS, Aurora PostgreSQL) must meet the same floor; check `SELECT extversion FROM pg_extension WHERE extname = 'vector'` rather than trusting a release-note table (AWS lists pgvector 0.8.1 for Aurora 17.9, from the AWS docs via web search, not verified on the cluster). `database-1` (Aurora 17.9) was not used: AWS's Free-plan pages cap Aurora at 1 GiB storage and 4 ACU (ADR-034).

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE companies (cik int PRIMARY KEY, name text NOT NULL, ticker text,
  sic int, fye_mmdd char(4), aliases text[] NOT NULL DEFAULT '{}');

-- versioned tables; views point at the live version (atomic swap, M3)
CREATE TABLE facts_v7 (
  fact_id bigint PRIMARY KEY, fact_key bigint NOT NULL, cik int NOT NULL, tag text NOT NULL, taxonomy text NOT NULL,
  uom text NOT NULL, qtrs smallint NOT NULL, period_start date, period_end date NOT NULL,
  fiscal_year smallint NOT NULL, fiscal_period text NOT NULL, calendar_frame text,
  value numeric NOT NULL, accession text NOT NULL, form text NOT NULL, filed date NOT NULL,
  is_latest boolean NOT NULL, is_original boolean NOT NULL,
  is_restated boolean NOT NULL DEFAULT false, anomaly_score real,
  derived_from bigint[]                                           -- derived Q4 only; the verifier (M6) re-executes FY - 9M from these
);
CREATE INDEX ON facts_v7 (cik, tag, fiscal_year, fiscal_period, uom) WHERE is_original;   -- default answer path
CREATE INDEX ON facts_v7 (fact_key);
CREATE INDEX ON facts_v7 (tag, fiscal_year, fiscal_period);       -- for rank/compare across companies

CREATE TABLE chunks_v7 (
  chunk_id text PRIMARY KEY, cik int, accession text, form text,
  fiscal_year smallint, fiscal_period text, section text, section_title text,
  chunk_index int, text text NOT NULL, n_tokens int, is_table boolean,
  source_url text, anchor text, page int, doc_source text, tenant_id int,   -- tenant_id null = public corpus (S2)
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding halfvec(384) NOT NULL
);
CREATE INDEX ON chunks_v7 USING gin (tsv);
CREATE INDEX ON chunks_v7 USING hnsw (embedding halfvec_cosine_ops) WITH (m = 32, ef_construction = 128);   -- measured on the M3 corpus, ADR-040 (m = 16, ef_construction = 64 reached recall@10 0.92 at ef_search 800)
CREATE INDEX ON chunks_v7 (cik, fiscal_year);

CREATE OR REPLACE VIEW facts AS SELECT * FROM facts_v7;    -- swapped in one transaction
CREATE OR REPLACE VIEW chunks AS SELECT * FROM chunks_v7;
CREATE TABLE corpus_state (id int PRIMARY KEY DEFAULT 1 CHECK (id = 1), live_version int NOT NULL,
  previous_version int, swapped_at timestamptz);   -- previous_version = the rollback target; NULL before the first swap
-- Swap: ONE transaction replaces both views and sets live_version = N, previous_version = <old live_version>, swapped_at = now().
-- Rollback: the same transaction shape with the pair exchanged (live_version = old previous_version, previous_version = old live_version).
-- Retention: after a swap or rollback, drop every facts_vK / chunks_vK whose K is NOT in (live_version, previous_version). It is
-- never "N-2": after swap 1, swap 2, rollback to 1, load 3, version 1 is live and must survive. Loading N is refused only when N is the
-- LIVE version (its tables are serving); reloading the previous version drops and recreates it, which replaces the rollback target until the reload finishes (ADR-038). The loader is the single writer (one load at a time); corpus_version = max + 1 assumes that.

CREATE TABLE tenants (tenant_id serial PRIMARY KEY, name text, created_at timestamptz DEFAULT now(),
  daily_question_cap int DEFAULT 50);
CREATE TABLE api_keys (key_id serial PRIMARY KEY, tenant_id int REFERENCES tenants,
  key_hash bytea UNIQUE NOT NULL, label text, created_at timestamptz DEFAULT now(), revoked_at timestamptz);
CREATE TABLE invite_codes (code_hash bytea PRIMARY KEY, tenant_id int REFERENCES tenants,
  max_uses int DEFAULT 1, uses int DEFAULT 0, expires_at timestamptz);

CREATE TABLE usage_log (
  request_id text PRIMARY KEY, tenant_id int, created_at timestamptz DEFAULT now(),
  question text, route text, route_source text, route_conf real,
  verification_status text, n_claims int, n_dropped int, refused boolean,
  cached boolean, latency_ms int, input_tokens int, output_tokens int, cost_usd numeric(10,6),
  corpus_version int, engine_version text, trace_id text, error text
);
CREATE TABLE feedback (feedback_id serial PRIMARY KEY, request_id text REFERENCES usage_log,
  rating smallint CHECK (rating IN (-1, 1)), comment text, created_at timestamptz DEFAULT now(),
  triage_label text, triaged_at timestamptz);          -- data|mapping|retrieval|generation|verification|ux
```

Sizing [Guessing; measure in M3]: S&P 500 10-K + 10-Q text for FY2020–2025 is about 0.5–1M chunks of about 400 tokens. `halfvec(384)` is 768 bytes per vector, so about 0.4–0.8 GB of vectors plus the HNSW graph. That's tight but workable on 4 GiB RAM. The fast-track (10-K only) roughly halves it.

---

## 4. Engine contracts (Pydantic; used with Claude structured outputs)

### 4.1 QueryPlan (LLM output; you write the schema and compiler)

```python
class CompanyRef(BaseModel):
    mention: str                       # as written by the user: "Apple", "AAPL"
    cik: int | None = None             # filled in by code (resolve_company), never by the LLM

class PeriodRef(BaseModel):
    fiscal_year: int
    fiscal_period: Literal["FY", "Q1", "Q2", "Q3", "Q4"] = "FY"

MetricName = Literal[...]              # generated from metric_catalog.yaml keys at import time

class QueryPlan(BaseModel):
    intent: Literal["numeric", "narrative", "hybrid", "out_of_scope"]
    companies: list[CompanyRef] = []
    periods: list[PeriodRef] = []     # only fiscal periods the user names explicitly
    period_text: str | None = None     # relative or other period wording, resolved by code (periods.py), never by the LLM (ADR-048)
    metrics: list[MetricName] = []
    operation: Literal["value", "growth", "ratio", "compare", "rank", "trend"] | None = None
    narrative_topics: list[str] = []   # "supply chain risk", "drivers of gross margin decline"
    section_hints: list[Literal["1", "1A", "1C", "7", "7A", "8", "PART1-2", "PART2-1A"]] = []
    needs_clarification: str | None = None
```

**Periods (ADR-048).** The planner fills `periods` only with fiscal periods the user names. For relative wording it leaves `periods` empty and sets `period_text` ("last year", "prior year", "most recent quarter", else the user's own words). Code resolves it with `periods.resolve_period` against each named company's latest filed period, never today's date:
- both empty: a numeric or hybrid plan uses the latest fiscal year every named company has filed, and the answer says so; a text-only plan searches all years.
- `period_text` only: resolved per company; when the companies differ, the earliest of the resolved periods is used and the answer says so. If it cannot be resolved, **or any named company has no filing to anchor it**, a numeric plan asks (the question is the refusal reason); a text-only plan searches all years. A numeric plan whose question names a four-digit year that the plan dropped (empty `periods`, the year not in `period_text`) also asks.
- both set: accepted only when the wording restates one of `periods` (it resolves to that period and contains its year, e.g. "fiscal 2023"). Relative wording beside named periods makes a numeric plan ask which periods are meant (a text-only plan keeps `periods`).

**Front door (ADR-048).** `needs_clarification` is returned as a refusal whose `reason` is that question (§4.3 shape, no new field). A numeric or hybrid plan with an empty `metrics` list (no catalog key means what was asked) is not compiled: the engine retrieves filing text instead and the composer answers with text claims and verbatim quotes, or refuses. A metric that **is** mapped but that the compiler cannot support (`NotFound`, `UnsupportedPlan`) does **not** fall back to text for a numeric-only plan: text must not route around an abstention.

### 4.2 Metric catalog (`app/engine/plan/metric_catalog.yaml`; start with ~50 metrics)

```yaml
revenue:
  label: Revenue
  unit: USD
  period_type: duration
  tags:            # candidates in preference order; with 2+ tags, resolved or abstained (ADR-043)
    - RevenueFromContractWithCustomerExcludingAssessedTax
    - Revenues
  synonyms: [sales, net sales, total revenue, top line, turnover]
bank_revenue_net_of_interest_expense:   # its own concept, never a revenue fallback (ADR-042)
  label: Total net revenue (net of interest expense)
  unit: USD
  period_type: duration
  presentation_basis: net_of_interest_expense
  tags: [RevenuesNetOfInterestExpense]
operating_income:
  label: Operating income
  unit: USD
  period_type: duration
  tags: [OperatingIncomeLoss]
gross_profit:
  unit: USD
  period_type: duration
  tags: [GrossProfit]
  fallback_formula: revenue - cost_of_revenue
  only_if: [same_entity, same_period, same_currency, same_scope, inputs_verified]
  fallback_result: derived
operating_margin:
  label: Operating margin
  unit: ratio
  formula: operating_income / revenue
eps_diluted:
  unit: USD/shares
  period_type: duration
  tags: [EarningsPerShareDiluted]
total_assets:
  unit: USD
  period_type: instant
  tags: [Assets]
```

`RevenuesNetOfInterestExpense` comes from the M1 second-source spot check. JPMorgan's two 10-Ks (FY2022, FY2023) tag total net revenue that way and carry no `Revenues` tag, so those two comparisons are `missing_in_curated` by tag name even though the amounts are in `curated.facts` (`docs/milestones/M01-spotcheck.json`). The M4 catalog review made it its own metric, with `presentation_basis: net_of_interest_expense`, instead of a revenue fallback (ADR-042). Tax-inclusive revenue is likewise its own metric, `revenue_including_assessed_tax`. The spot-check's own tag list is deliberately not widened.

Fields added by the M4 catalog review (all optional; ADR-042):
- `presentation_basis`: `net_of_interest_expense` | `including_assessed_tax`. It marks a metric whose basis differs from the generic concept; such a metric is never a fallback of that concept.
- `only_if`: the conditions under which `fallback_formula` may be used: `same_entity`, `same_period`, `same_currency`, `same_scope` (consolidated totals only) and `inputs_verified`. A consumer that cannot check every listed condition must not use the fallback, and a fallback with no `only_if` is never used for gold.
- `fallback_result: derived`: a fallback value is labelled derived, never reported.
- With two or more `tags`, the M4 gold generator does **not** let the first populated tag win. Candidates that disagree materially are resolved by income-statement presentation first, then the gross-profit identity; otherwise the generator abstains (ADR-043). The engine's rule is M5's to decide (M05 §3.4 still says first-wins).
- Validation (`MetricDef`): `only_if` and `fallback_result` require a `fallback_formula`; `only_if` must be non-empty and free of duplicates; conditions outside the list are rejected. That a `presentation_basis` metric is never a fallback of the generic concept is a catalog convention, reviewed by hand, not validated. `same_scope` holds because the generator reads only the consolidated `facts` view; a consumer reading another table must check consolidation itself.
- These fields enter `definition_sha` (the verified-allowlist pin) only when set.

### 4.3 Answer and Claim (composer output, verifier input)

```json
{
  "answer_markdown": "Apple's FY2023 operating margin was **29.8%** [1]. Management attributed ... [2]",
  "claims": [
    {"id": "c1", "kind": "numeric_derived", "text": "Apple's FY2023 operating margin was 29.8%.",
     "value": 0.2982, "unit": "ratio", "display": "29.8%", "primary": true,
     "provenance": {"type": "expr", "expr": "f:111 / f:222", "fact_ids": [111, 222]}},
    {"id": "c2", "kind": "text", "text": "Management cited ... as the main driver.",
     "provenance": {"type": "chunk", "chunk_id": "0000320193-23-000106#7#0012",
                    "quote": "exact substring copied from the chunk"}}
  ],
  "refusal": null
}
```
Rules: every claim has exactly one provenance. `expr` may only use `f:<fact_id>`, numbers, and `+ - * /` and parentheses, and the `f:<id>` tokens must be exactly `fact_ids` (`app/engine/answer.py` parses it). A `fact_id` is a signed 64-bit hash (xxhash64), so it can be negative: `f:-6651281536542086215` is one token, and `f:5 - f:-3` is a subtraction of two facts (widened 2026-11-25, ADR-046; before that a negative id could not be cited, and 179 of the 360 `gold_fact_ids` in `numeric_auto.jsonl` are negative). The id is ASCII digits within the signed 64-bit range (a bigint lookup would overflow outside it); any name (letters, underscore) outside an `f:` token is rejected. Numeric literals (`100`, `1e3`) stay valid. `quote` must be an exact substring of the chunk text (after whitespace normalization).

**Refusal** (ADR-041): `refusal` is `null` or an object with three fields, the same shape everywhere it appears (composer output, the M6 verifier's sufficiency refusal, the API response):

```json
"refusal": {"reason": "No FY2027 guidance appears in Apple's filings.",
            "found": ["Apple's FY2023 revenue was $383.3 billion."],
            "missing": ["revenue guidance for fiscal 2027"]}
```
- `reason` (string, required): one sentence a user can read.
- `found` (list of strings, default `[]`): what the filings do support. When the verifier refuses, each entry is the `text` of a surviving verified claim; the claims themselves stay in `claims`, so `found` never carries provenance of its own.
- `missing` (list of strings, default `[]`): what would have been needed, e.g. one entry per (company, metric, period) or narrative topic left unsupported.
A refused answer may keep `claims` (partial answer) or leave it empty. Eval treats `refusal != null` as a refusal (`eval/metrics.py`).

### 4.4 Verifier output (per claim)

```json
{"claim_id": "c2", "status": "supported|unsupported|partial|error",
 "checks": {"provenance_exec": "n/a", "metadata_match": "pass", "number_in_text": "pass",
            "quote_substring": "pass", "entailment": "supported"},
 "action": "keep|drop|rewrite", "reason": "..."}
```

### 4.5 Plan compiler contract (M5, ADR-046; models in `app/engine/plan/schema.py`)

```python
def compile_plan(plan: QueryPlan, conn: psycopg.Connection[Any], *,
                 catalog: Mapping[str, MetricDef] | None = None,
                 universe: Collection[int] | None = None) -> CompileOutcome: ...
# raises UnsupportedPlan; every other failure is a NotFound inside the outcome

class CompileOutcome(BaseModel):
    results: list[NumericResult] = []
    not_found: list[NotFound] = []

class NumericResult(BaseModel):
    metric: str                        # catalog key
    cik: int
    fiscal_year: int                   # facts.fiscal_year, never the filing's (M1 bug)
    fiscal_period: Literal["FY", "Q1", "Q2", "Q3", "Q4"]
    operation: Literal["value", "growth", "difference"]
    value: Decimal                     # base units: USD, shares, USD/shares; ratios and growth are fractions
    unit: Literal["USD", "ratio", "USD/shares", "shares"]   # growth and ratio metrics: "ratio"
    expr: str                          # 4.3 grammar over fact ids: "f:111", "f:111 / f:222", "(f:2 - f:1) / f:1"
    fact_ids: list[int]                # the f: tokens of expr, as a set (a repeated id in expr is listed once)
    kind: Literal["reported", "derived"]   # derived = any arithmetic, incl. a fallback_formula (gold's value_kind)
    resolution: Literal["single_candidate", "candidates_agree", "reconciles_with_gross_profit"] | None = None
    compare_cik: int | None = None     # "difference" only: value = result for `cik` minus result for `compare_cik`
    rank: int | None = None            # "rank" rows only, 1 = best
    notes: list[Note] = []

class Note(BaseModel):
    code: Literal["restated", "derived_fallback", "q4_derived"]
    text: str
    must_mention: bool = True          # the composer must carry it into the answer
    latest_value: Decimal | None = None      # "restated": the latest restated value
    latest_fact_id: int | None = None        # "restated": cite with expr "f:<latest_fact_id>"

class NotFound(BaseModel):
    metric: str
    cik: int
    fiscal_year: int
    fiscal_period: Literal["FY", "Q1", "Q2", "Q3", "Q4"]
    reason: Literal["no_fact", "conflicting_candidates", "missing_input", "growth_base_missing",
                    "growth_base_nonpositive", "division_by_zero", "fallback_not_allowed"]
    detail: str = ""
    missing_inputs: list[str] = []     # formula operands that had no value ("missing_input")

class UnsupportedPlan(Exception):      # `reason: str`; the plan cannot be compiled at all
```
**Semantics (the compiler tests encode these):**
- **Facts read:** the consolidated `facts` view only, `is_original` rows (the value as originally filed, ADR-017), parameterized SQL. Period filter: `fiscal_year` and `fiscal_period` of the fact; instants use `qtrs = 0`; duration metrics use `qtrs = 4` for FY and `qtrs = 1` for Q1 to Q4 (a derived Q4 is a `facts` row with `q4_derived`); `uom` equals the metric's `unit`, except that a `USD/shares` metric (EPS) matches facts stored with `uom = 'USD'`: FSDS records per-share amounts as `USD` (`eval.gen.numeric_policy` carries the same alias).
- **Tag metrics (ADR-043, not first-wins).** One tag: that fact. Two or more tags: candidates that agree within the gold generator's tolerance (`eval.gen.numeric_policy.agree`: 4 significant figures for USD, ADR-044) are one number (`candidates_agree`, the chain order only picks the tag to cite); on a material disagreement the candidate that satisfies the gross-profit identity wins (`reconciles_with_gross_profit`); otherwise `NotFound(reason="conflicting_candidates")`. The presentation step of ADR-043 is **not available** in serving (`fsds_pre` is not exported) and the `Revenues`-as-top-line step (ADR-043 step 6) is **not applied**: it has not been re-audited (ADR-043, Path B). So the engine abstains in cases the gold generator may have resolved. The reverse can also happen: without presentation data the engine cannot reach gold's `presentation_contradicts_calculation` abstention. Gold's unconditional pair guard **is** applied: `Revenues` below contract revenue abstains before the identity is tried (ADR-047). Revenue is outside M4 gold, so no gold row is affected.
- **Formula metrics:** `formula` is parsed to an AST (`+ - * /`, numbers, catalog keys); operands compile recursively; `expr` substitutes leaf facts, parenthesizing each sub-expression; the value is `Decimal` arithmetic in the default context, never `float`. A missing operand gives `NotFound(missing_input, missing_inputs=[...])`; a zero divisor gives `division_by_zero`. `fallback_formula` is used only when no tag has a value, only when every `only_if` condition holds, and its result carries `kind="derived"` and a `derived_fallback` note; a fallback with no `only_if` is never used (`fallback_not_allowed`).
- **Operations:** `value` (and `ratio`, treated as `value` for a ratio-unit metric; `UnsupportedPlan` otherwise); `growth` = (v_t − v_{t−1}) / v_{t−1} with `expr = "(f:t - f:p) / f:p"`; one period in the plan means year over year (the same fiscal period of the previous year), two periods mean the later over the earlier in either list order (a trend of more is `trend`), refused when the base is missing (`growth_base_missing`) or ≤ 0 (`growth_base_nonpositive`); `compare` = one `value` result per company, plus one `difference` result (first company minus second) when exactly two companies are named; `trend` = one `value` result per period; `rank` = one `value` result per row, ordered, `rank` set, over the named companies or over `universe` (the tracked S&P 500 CIKs, passed in by the engine) with `LIMIT 10`.
- **Restated:** the `is_original` value is returned with a `restated` note carrying the latest value and its `fact_id`. A derived Q4 of a per-share metric is never returned (`NotFound(no_fact)`): FY − 9M is not valid for per-share amounts and 05 §2.2 excludes them, but FSDS records per-share facts as `uom = 'USD'`, so the lake's exclusion (a `/` in the uom) misses them and such rows exist; the data bug is logged separately. A derived Q4 of any other metric is one `facts` row, so it is `kind="reported"` with expr `f:<id>` and a `q4_derived` note (the verifier re-executes FY - 9M from `derived_from`).
- **UnsupportedPlan** is raised for: no operation on a numeric plan, no metric, no company without a CIK, no period, an unsupported operation for the metric. The engine then refuses. The guarded SQL fallback (`ENGINE_SQL_FALLBACK=on`, off by default) is **not wired in M5**: the engine defines the variable's name and never reads it, and there is no text-to-SQL generator. Fallback rows are fact lookups cited with `expr` provenance; a separate `sql` provenance type is **deferred** because adding a variant to `Answer` changes its JSON schema and with it every cassette key (ADR-046).
- **Units and display:** values stay in base units; formatting happens only in the composer and UI.
- **Rules fixed by the implementation (ADR-047):**
  - A full-year **duration** fact counts only from a `10-K` family form. The lake also labels a 10-Q's trailing-twelve-month figure `FY` with `qtrs = 4` (found in review; the gold generator's `screen()` does not filter forms). Quarters and instants are not filtered by form.
  - Growth is computed only when both periods are obtained the same way: both reported, or both computed from the same inputs, and each input uses one tag or two tags that reported the same number in at least one of the two periods. Otherwise `NotFound(conflicting_candidates)`.
  - A note names the metric and period it is about (on a ratio or growth result that is an input). `Note` carries no unit, so the composer does not show a `latest_value` on a ratio or growth result.
  - Only `taxonomy = 'us-gaap'` facts are read: a custom tag that shares a name is a different concept (the gold generator's scope, ADR-044).
  - Two or more facts for one (tag, period), or candidate tags or formula inputs that end on different dates, give `NotFound(conflicting_candidates)`.
  - The gross-profit identity uses `GrossProfit` facts only, never the fallback formula. A derived-Q4 `GrossProfit` row (FY − 9M) counts, as do derived-Q4 inputs of a fallback (`same_scope` means "a fact, not a formula result"). The gold generator excludes derived Q4 by default.
  - `inputs_verified` means every input came out of the tag rule above (`resolution` is set). The gold generator's allow-list (`eval/gen/numeric_metrics_verified.txt`), which §4.2's condition also refers to, is **not** consulted by the engine: no revenue-family metric is on it, so consulting it would turn the fallback off. `same_scope` additionally requires reported (not computed) inputs.
  - `UnsupportedPlan` is also raised for: a rank of exactly one company; a rank with no company and no `universe`; growth of a ratio metric, over more than two periods, or between unlike periods; a catalog formula cycle.
  - A rank over named companies lists all of them (a `NotFound` for each without a value). A rank over `universe` returns the top 10 and leaves out companies without a usable value **without listing them**, so "top 10" means top 10 among the companies whose figure could be resolved.

---

## 5. API

### `POST /v1/ask`

Request:
```json
{"question": "What was Apple's operating margin in fiscal 2023, and what drove it?",
 "options": {"composer": "auto", "max_citations": 8, "include_plan": true}}
```
Headers: `Authorization: Bearer <api key>`, optional `Idempotency-Key`.

Response 200 *(illustrative values)*:
```json
{
  "request_id": "req_01JF3K...", "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "route": {"label": "hybrid", "source": "classifier", "confidence": 0.91},
  "plan": {"intent": "hybrid", "companies": [{"mention": "Apple", "cik": 320193}],
           "periods": [{"fiscal_year": 2023, "fiscal_period": "FY"}],
           "metrics": ["operating_margin"], "operation": "value",
           "narrative_topics": ["drivers of operating margin"], "section_hints": ["7"]},
  "answer_markdown": "Apple's FY2023 operating margin was **29.8%** [1] ... [2]",
  "claims": [ "... as in §4.3, each with a verification status ..." ],
  "citations": [
    {"n": 1, "kind": "fact", "fact_ids": [111, 222], "label": "OperatingIncomeLoss / Revenue, FY2023 (10-K)",
     "source_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"},
    {"n": 2, "kind": "chunk", "chunk_id": "0000320193-23-000106#7#0012", "section": "Item 7",
     "quote": "...", "source_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"}],
  "verification": {"status": "verified", "claims_total": 2, "claims_verified": 2, "claims_dropped": 0},
  "refusal": null,
  "usage": {"llm_calls": 4, "input_tokens": 15200, "output_tokens": 1180, "cost_usd": 0.0213},
  "latency_ms": {"total": 6120, "route": 4, "plan": 820, "sql": 12, "retrieval": 140,
                 "rerank": 210, "compose": 3900, "verify": 1030},
  "corpus_version": 7, "engine_version": "0.6.0", "cached": false
}
```
`refusal` is `null` or `{"reason", "found", "missing"}` exactly as in §4.3 (ADR-041).

Errors use `application/problem+json` (RFC 9457): `400 invalid_question`, `401 invalid_key`, `429 rate_limited` (+ `Retry-After`), `503 spend_cap_reached`, `500 internal` (always with `request_id`).

Other endpoints: `POST /v1/feedback {request_id, rating, comment}`, `POST /v1/keys/redeem {invite_code}` → `{api_key}` (shown once), `GET /v1/health`, `GET /v1/chunks/{chunk_id}` (citation drawer), `GET /v1/facts/{fact_id}`.

---

## 6. Evaluation files (`eval/sets/*.jsonl`)

**Gold files are protected:** a Claude Code hook blocks edits to `eval/sets/*.jsonl` and `eval/judges/calibration.jsonl` (see `06_claude_code_playbook.md`).

```jsonc
// numeric_auto.jsonl (300; generated by eval/gen/make_numeric_set.py from curated.facts)
{"id": "num-0001", "question": "What was Apple's revenue in fiscal year 2023?",
 "qtype": "single_value", "cik": 320193, "metric": "revenue", "fiscal_year": 2023, "fiscal_period": "FY",
 "gold_value": 383285000000, "unit": "USD", "tolerance_rel": 0.0005, "tolerance_abs": 0.5,
 "tolerance_basis": "USD: 4 significant figures (abs 0.5)", "gold_fact_ids": [111],
 "template_id": "single_v1", "paraphrased": true, "second_source_check": "match|mismatch|unchecked",
 "value_basis": "as_filed", "latest_value": null, "is_restated": false, "value_kind": "reported",
 "source_tags": ["RevenueFromContractWithCustomerExcludingAssessedTax"],
 "resolution": "candidates_agree", "compare_cik": null, "period_end": "2023-09-30"}

// financebench.jsonl (all 150)
{"id": "fb-00123", "question": "...", "gold_answer": "...", "company": "3M", "doc_name": "3M_2018_10K",
 "evidence": [{"page": 59, "text": "..."}], "question_type": "metrics-generated", "reasoning": "..."}

// narrative.jsonl (50; drafted by an LLM, verified and edited by you)
{"id": "nar-001", "question": "What supply-chain risks did Nike disclose in its FY2022 10-K?",
 "cik": 320187, "fiscal_year": 2022, "gold_chunk_ids": ["...#1A#0004"],
 "gold_points": ["reliance on contract manufacturers", "..."], "drafted_by": "claude-sonnet-5-5",
 "verified_by": "shekar", "verified_at": "2026-11-30"}

// unanswerable.jsonl (50)
{"id": "un-001", "question": "What revenue guidance did Apple give for fiscal 2027?",
 "reason": "future_guidance_not_in_filings", "expected": "refuse"}

// router_test.jsonl (~250, hand-labeled; never used for training)
{"id": "rt-001", "question": "...", "label": "hybrid", "source": "financebench|narrative|user_log", "labeled_by": "shekar"}

// regressions.jsonl (grows during M11)
{"id": "reg-001", "question": "...", "failure_type": "mapping", "expected": {...}, "from_request_id": "req_..."}
```

`numeric_auto.jsonl` fields added by the M4 catalog review (ADR-044; every one has a default, so a row without them still validates):
- **Tolerance.** An answer `v` is correct when `|v - gold_value| <= max(tolerance_abs, tolerance_rel * |gold_value|)`, in the gold unit. The generator sets both per metric unit, and `tolerance_basis` says why:
  - USD and share amounts: 4 significant figures (`rel` 0.0005), with an absolute floor of half a base unit, so a gold of 0 needs an exact 0.
  - Per-share amounts: half a cent, absolute.
  - Ratios and growth (fractions): 0.05 percentage points, absolute.
  - Defaults: `tolerance_rel` 0.01 and `tolerance_abs` 0, the old flat rule. They apply only to rows written before ADR-044.
- **Versions.** `value_basis` is `as_filed` (gold = the originally filed value, ADR-017) or `latest_restated`. One policy holds per set. `latest_value` is the latest restated value on a `single_value` row whose reported fact was restated (null on derived rows, even when `is_restated` is true because an input was restated); a restated row's question says "as originally reported" (or "as most recently restated").
- **Provenance.** `value_kind` is `reported` (one reported fact) or `derived` (growth, ratio, compare, or a `fallback_formula` such as gross profit = revenue - cost of revenue). `source_tags` are the tags of `gold_fact_ids` in the same order. `resolution` says how a multi-tag metric was resolved (ADR-043). `compare_cik` is the second company of a `compare` row; `compare` gold is the first company's value minus the second's. `period_end` is the primary fact's period end.

### Results file (`eval/results/<run_id>.json`, the only source for `docs/results.md`)

```json
{"run_id": "2026-12-05T21-14-03Z_engine-0.5.0_sonnet", "git_sha": "abc1234", "corpus_version": 7,
 "config": {"composer": "claude-sonnet-5-5", "planner": "claude-haiku-5-5", "verifier": "on", "router": "onnx+llm@0.7", "mode": "batch"},
 "sets": {"numeric": {"n": 300, "accuracy": 0.0, "ci95": [0.0, 0.0]},
          "financebench": {"n": 150, "accuracy": 0.0, "ci95": [0.0, 0.0], "judge_kappa": 0.0},
          "unanswerable": {"n": 50, "refusal_rate": 0.0}, "answerable_false_refusal": 0.0,
          "claims": {"n": 0, "unsupported_rate_pre": 0.0, "unsupported_rate_post": 0.0}},
 "latency_ms": {"p50": 0, "p95": 0}, "cost": {"total_usd": 0.0, "per_question_usd": 0.0},
 "per_question": [{"id": "num-0001", "correct": true, "route": "numeric", "cost_usd": 0.0, "latency_ms": 0, "trace_id": "..."}]}
```

`config` fields added in M4 (all optional, so earlier files still validate): `sample` and `sample_seed` (a run on a seeded random sample of N questions per set, `--sample N --sample-seed S`; both null for a full run, so a 30-question long-context run is not mistaken for a full run and its questions can be reproduced), and `request_timeout_s` (the per-call API timeout when it was raised from the 120 s default, null otherwise). `sets.<name>` may also carry `scoring_errors`, the count of questions a metric rejected with `ValueError` (unscored, not counted wrong; a run with more than 10% of a set's rows in this state exits 2, ADR-044). A per-question `retrieved_top20` is null, and the `recall_at_k` keys are absent, for a system that does not retrieve (`longctx`).
