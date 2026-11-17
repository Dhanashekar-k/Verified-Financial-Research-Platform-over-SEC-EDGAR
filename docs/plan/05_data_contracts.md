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
  accession string, cik int, form string, filed date, url string,
  s3_uri string, bytes bigint, sha256 string, http_status int, fetched_at timestamp
) USING iceberg PARTITIONED BY (form);
```

### 2.2 Curated

```sql
CREATE TABLE lake.curated.companies (
  cik int, name string, ticker string, sic int, fye_mmdd string,
  in_sp500 boolean, in_financebench boolean, aliases array<string>
) USING iceberg;

CREATE TABLE lake.curated.facts_history (       -- every reported version of every fact
  fact_id bigint,             -- xxhash64(fact_key, accession): unique per version (same as facts.fact_id)
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
--   source = 'fsds', form and accession = those of the FY parent row (normally a '10-K'; never hard-coded),
--   qtrs = 1, period_end = the FY end, fiscal_period = 'Q4', dq_flags contains 'q4_derived',
--   derived_from = [FY fact_id, 9M fact_id] so M6 can re-execute FY - 9M.
--   Derived per provenance tier within (cik, tag, taxonomy, uom, fiscal_year): the original Q4 from the original FY and 9M,
--   the latest Q4 from the latest FY and 9M. derived_from points only at parent rows that `facts` retains (when a parent's
--   latest and original values are equal, the single retained row carries the latest accession). Ties between candidate
--   parents go to the latest filed. Same Q4 value in both tiers collapses to one row, like any other fact.
--   Their fact_key differs from the FY fact's (qtrs 1 vs 4). A directly reported Q4 (qtrs = 1, same period_end) in the same
--   accession would collide on (fact_key, accession), so a direct row always wins and Q4 is derived only when none exists.

CREATE TABLE lake.curated.facts (               -- latest version per fact_key, plus the original when it differs (M1 §3.5)
  fact_id bigint,             -- xxhash64(fact_key, accession): unique per version
  fact_key bigint,            -- groups versions of the same fact; same definition as facts_history.fact_key
  cik int, tag string, taxonomy string, is_custom boolean, uom string, qtrs int,
  period_start date, period_end date, fiscal_year int, fiscal_period string, calendar_frame string,
  value decimal(28,4), accession string, form string, filed date,
  is_latest boolean, is_original boolean,      -- both true when the value was never restated
  n_versions int, is_restated boolean,         -- restated = more than one distinct value across versions
  dq_flags array<string>,                      -- e.g. ['fy_mismatch_companyfacts','q4_derived']
  derived_from array<bigint>,                  -- same meaning as in facts_history (derived Q4 only)
  anomaly_score float                          -- filled by S1 (null until then)
) USING iceberg PARTITIONED BY (fiscal_year);
-- Consolidated facts only: coreg IS NULL and segments null/empty. This is the table the plan compiler reads.

CREATE TABLE lake.curated.facts_dimensional (   -- segmented (dimensional) rows, kept out of facts so they can't leak into company totals
  fact_id bigint,             -- xxhash64(fact_key, accession)
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
  parse_status string, parse_error string, n_sections int, n_chunks int, doc_source string
) USING iceberg PARTITIONED BY (fiscal_year);

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
```

### 2.3 Exports (lake → serving)

`s3://<lake-bucket>/exports/serving/corpus_version=N/{companies,facts,chunks}/*.parquet` plus `_MANIFEST.json`:
```json
{"corpus_version": 7, "created_at": "2026-12-04T03:12:00Z", "git_sha": "abc1234",
 "facts_rows": 0, "chunks_rows": 0, "embedding_model": "BAAI/bge-small-en-v1.5", "dim": 384,
 "iceberg_snapshots": {"curated.facts": 0, "curated.chunks": 0}}
```
Record the Iceberg snapshot IDs. You can then reproduce any eval run against the exact corpus with time travel.

---

## 3. Serving store (Postgres 17 + pgvector 0.8.x)

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
CREATE INDEX ON chunks_v7 USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX ON chunks_v7 (cik, fiscal_year);

CREATE OR REPLACE VIEW facts AS SELECT * FROM facts_v7;    -- swapped in one transaction
CREATE OR REPLACE VIEW chunks AS SELECT * FROM chunks_v7;
CREATE TABLE corpus_state (id int PRIMARY KEY DEFAULT 1, live_version int NOT NULL, swapped_at timestamptz);

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
    periods: list[PeriodRef] = []
    metrics: list[MetricName] = []
    operation: Literal["value", "growth", "ratio", "compare", "rank", "trend"] | None = None
    narrative_topics: list[str] = []   # "supply chain risk", "drivers of gross margin decline"
    section_hints: list[Literal["1", "1A", "1C", "7", "7A", "8", "PART1-2", "PART2-1A"]] = []
    needs_clarification: str | None = None
```

### 4.2 Metric catalog (`app/engine/plan/metric_catalog.yaml`; start with ~50 metrics)

```yaml
revenue:
  label: Revenue
  unit: USD
  period_type: duration
  tags:            # ordered fallback; first tag with a value for (cik, period) wins
    - RevenueFromContractWithCustomerExcludingAssessedTax
    - Revenues
    - SalesRevenueNet
    - RevenueFromContractWithCustomerIncludingAssessedTax
  synonyms: [sales, net sales, total revenue, top line, turnover]
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
Rules: every claim has exactly one provenance. `expr` may only use `f:<fact_id>`, numbers, and `+ - * /` and parentheses. `quote` must be an exact substring of the chunk text (after whitespace normalization).

### 4.4 Verifier output (per claim)

```json
{"claim_id": "c2", "status": "supported|unsupported|partial|error",
 "checks": {"provenance_exec": "n/a", "metadata_match": "pass", "number_in_text": "pass",
            "quote_substring": "pass", "entailment": "supported"},
 "action": "keep|drop|rewrite", "reason": "..."}
```

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

Errors use `application/problem+json` (RFC 9457): `400 invalid_question`, `401 invalid_key`, `429 rate_limited` (+ `Retry-After`), `503 spend_cap_reached`, `500 internal` (always with `request_id`).

Other endpoints: `POST /v1/feedback {request_id, rating, comment}`, `POST /v1/keys/redeem {invite_code}` → `{api_key}` (shown once), `GET /v1/health`, `GET /v1/chunks/{chunk_id}` (citation drawer), `GET /v1/facts/{fact_id}`.

---

## 6. Evaluation files (`eval/sets/*.jsonl`)

**Gold files are protected:** a Claude Code hook blocks edits to `eval/sets/*.jsonl` and `eval/judges/calibration.jsonl` (see `06_claude_code_playbook.md`).

```jsonc
// numeric_auto.jsonl (300; generated by eval/gen/make_numeric_set.py from curated.facts)
{"id": "num-0001", "question": "What was Apple's revenue in fiscal year 2023?",
 "qtype": "single_value", "cik": 320193, "metric": "revenue", "fiscal_year": 2023, "fiscal_period": "FY",
 "gold_value": 383285000000, "unit": "USD", "tolerance_rel": 0.01, "gold_fact_ids": [111],
 "template_id": "single_v3", "paraphrased": true, "second_source_check": "match|mismatch|unchecked"}

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
