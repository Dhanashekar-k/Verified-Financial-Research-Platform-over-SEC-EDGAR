-- facts_v{v}: contract section 3. No secondary indexes before the bulk load (indexes.sql).
CREATE TABLE facts_v{v} (
  fact_id bigint PRIMARY KEY, fact_key bigint NOT NULL, cik int NOT NULL, tag text NOT NULL,
  taxonomy text NOT NULL, uom text NOT NULL, qtrs smallint NOT NULL, period_start date,
  period_end date NOT NULL, fiscal_year smallint NOT NULL, fiscal_period text NOT NULL,
  calendar_frame text, value numeric NOT NULL, accession text NOT NULL, form text NOT NULL,
  filed date NOT NULL, is_latest boolean NOT NULL, is_original boolean NOT NULL,
  is_restated boolean NOT NULL DEFAULT false, anomaly_score real,
  derived_from bigint[]
)
