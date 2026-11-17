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
