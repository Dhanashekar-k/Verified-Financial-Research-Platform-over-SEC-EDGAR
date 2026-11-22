-- Built AFTER the bulk load. One statement per `-- name:` block; {v} version, {m} {ef} HNSW parameters.
-- name: facts_orig
CREATE INDEX facts_v{v}_orig_idx ON facts_v{v} (cik, tag, fiscal_year, fiscal_period, uom) WHERE is_original
-- name: facts_key
CREATE INDEX facts_v{v}_key_idx ON facts_v{v} (fact_key)
-- name: facts_tag
CREATE INDEX facts_v{v}_tag_idx ON facts_v{v} (tag, fiscal_year, fiscal_period)
-- name: chunks_cik_fy
CREATE INDEX chunks_v{v}_cik_fy_idx ON chunks_v{v} (cik, fiscal_year)
-- name: chunks_gin
CREATE INDEX chunks_v{v}_tsv_gin ON chunks_v{v} USING gin (tsv)
-- name: chunks_hnsw
CREATE INDEX chunks_v{v}_hnsw ON chunks_v{v} USING hnsw (embedding halfvec_cosine_ops) WITH (m = {m}, ef_construction = {ef})
