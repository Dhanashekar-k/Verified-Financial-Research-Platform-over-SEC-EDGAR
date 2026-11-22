-- chunks_v{v}: contract section 3. `tsv` is generated; the HNSW and GIN indexes come after the load.
CREATE TABLE chunks_v{v} (
  chunk_id text PRIMARY KEY, cik int, accession text, form text, fiscal_year smallint,
  fiscal_period text, section text, section_title text, chunk_index int, text text NOT NULL,
  n_tokens int, is_table boolean, source_url text, anchor text, page int, doc_source text,
  tenant_id int,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding halfvec(384) NOT NULL
)
