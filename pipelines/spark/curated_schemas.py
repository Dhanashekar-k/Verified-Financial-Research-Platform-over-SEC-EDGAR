"""Explicit schemas of the curated fact tables (`docs/plan/05_data_contracts.md` section 2.2).

Column names, order and types follow the contract. `facts_history` also carries `fact_id` and
`dq_flags` (added with the derived-Q4 decision) so `facts` can be rebuilt from it.
"""

from __future__ import annotations

from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DateType,
    DecimalType,
    FloatType,
    IntegerType,
    LongType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

_S, _I, _L, _B, _D = StringType(), IntegerType(), LongType(), BooleanType(), DateType()
_VALUE = DecimalType(28, 4)
_FLAGS = ArrayType(StringType())


def _schema(*spec: tuple[str, object]) -> StructType:
    return StructType([StructField(name, dtype, True) for name, dtype in spec])  # type: ignore[arg-type]


FACTS_HISTORY = _schema(
    ("fact_id", _L), ("fact_key", _L),
    ("cik", _I), ("tag", _S), ("taxonomy", _S), ("is_custom", _B), ("uom", _S), ("qtrs", _I),
    ("period_start", _D), ("period_end", _D),
    ("fiscal_year", _I), ("fiscal_period", _S), ("calendar_frame", _S),
    ("value", _VALUE),
    ("accession", _S), ("form", _S), ("filed", _D), ("is_amendment", _B),
    ("dq_flags", _FLAGS), ("source", _S), ("derived_from", ArrayType(_L)),
)  # fmt: skip

FACTS = _schema(
    ("fact_id", _L), ("fact_key", _L),
    ("cik", _I), ("tag", _S), ("taxonomy", _S), ("is_custom", _B), ("uom", _S), ("qtrs", _I),
    ("period_start", _D), ("period_end", _D),
    ("fiscal_year", _I), ("fiscal_period", _S), ("calendar_frame", _S),
    ("value", _VALUE), ("accession", _S), ("form", _S), ("filed", _D),
    ("is_latest", _B), ("is_original", _B), ("n_versions", _I), ("is_restated", _B),
    ("dq_flags", _FLAGS), ("derived_from", ArrayType(_L)), ("anomaly_score", FloatType()),
)  # fmt: skip

FACTS_DIMENSIONAL = _schema(
    ("fact_id", _L), ("fact_key", _L),
    ("cik", _I), ("tag", _S), ("taxonomy", _S), ("is_custom", _B), ("uom", _S), ("qtrs", _I),
    ("segments", _S),
    ("period_start", _D), ("period_end", _D),
    ("fiscal_year", _I), ("fiscal_period", _S), ("calendar_frame", _S),
    ("value", _VALUE), ("accession", _S), ("form", _S), ("filed", _D),
    ("is_latest", _B), ("is_original", _B), ("n_versions", _I), ("is_restated", _B),
    ("dq_flags", _FLAGS),
)  # fmt: skip

# `docs/plan/05_data_contracts.md` section 2.2. `in_financebench` stays NULL until the FinanceBench
# documents are loaded (M2/M4): a guess would claim a company is not in the benchmark.
COMPANIES = _schema(
    ("cik", _I), ("name", _S), ("ticker", _S), ("sic", _I), ("fye_mmdd", _S),
    ("in_sp500", _B), ("in_financebench", _B), ("aliases", ArrayType(_S)),
)  # fmt: skip


# `curated.filings` and `curated.chunks` (contract section 2.2), in contract column order. The row
# models in `pipelines/parsing/document.py` carry the same fields; the schema test pins both.
FILINGS = _schema(
    ("accession", _S), ("cik", _I), ("form", _S), ("filed", _D), ("accepted_at", TimestampType()),
    ("period_of_report", _D), ("fiscal_year", _I), ("fiscal_period", _S),
    ("primary_doc_url", _S), ("html_s3_uri", _S), ("parse_status", _S), ("parse_error", _S),
    ("n_sections", _I), ("n_chunks", _I), ("doc_source", _S), ("dq_flags", _FLAGS),
)  # fmt: skip

CHUNKS = _schema(
    ("chunk_id", _S), ("cik", _I), ("accession", _S), ("form", _S),
    ("fiscal_year", _I), ("fiscal_period", _S), ("section", _S), ("section_title", _S),
    ("chunk_index", _I), ("text", _S), ("n_tokens", _I), ("is_table", _B),
    ("source_url", _S), ("anchor", _S), ("page", _I), ("doc_source", _S), ("corpus_version", _I),
)  # fmt: skip

# `curated.chunk_embeddings` (contract section 2.2, ADR-032): one L2-normalized float32 vector per
# chunk, from the quantized bge-small model. Partitioned by `bucket(16, chunk_id)`.
CHUNK_EMBEDDINGS = _schema(
    ("chunk_id", _S), ("model_name", _S), ("dim", _I),
    ("embedding", ArrayType(FloatType())), ("embedded_at", TimestampType()),
)  # fmt: skip
