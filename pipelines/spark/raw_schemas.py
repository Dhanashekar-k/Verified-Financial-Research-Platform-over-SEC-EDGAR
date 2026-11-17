"""Explicit schemas for the FSDS source files (never inferred; M01 section 3.4).

Field names, order and types come from the 2023q4 `readme.htm` field tables (figures 2-5) and
were checked against the real file headers. `fsds_to_iceberg` compares the whole header line of
every file with these names, in order, so a column the SEC adds, drops, renames or reorders fails
the load instead of loading shifted data.

Types: `ddate`, `period`, `filed` are yyyymmdd integers as in the contract (`05_data_contracts.md`
section 2.1); they become dates in `curated.*`. `value` is `decimal(28,4)` as the readme says
(`NUMERIC(28,4)`). Boolean flags stay 0/1 integers, as in the source. `ein` is a string because
EINs have leading zeros.

`raw.fsds_sub` keeps the 16 columns the contract lists; the file has 36 (addresses, former name,
...). The other 20 are read and type-checked, then dropped. `accepted` is US Eastern wall-clock
time as EDGAR writes it, stored as-is (Spark treats it as UTC; nothing converts it).
"""

from __future__ import annotations

from pyspark.sql.types import (
    DecimalType,
    IntegerType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

_S = StringType()
_I = IntegerType()


def _fields(*spec: tuple[str, object]) -> StructType:
    return StructType([StructField(name, dtype, True) for name, dtype in spec])  # type: ignore[arg-type]


SUB_SOURCE = _fields(
    ("adsh", _S), ("cik", _I), ("name", _S), ("sic", _I),
    ("countryba", _S), ("stprba", _S), ("cityba", _S), ("zipba", _S),
    ("bas1", _S), ("bas2", _S), ("baph", _S),
    ("countryma", _S), ("stprma", _S), ("cityma", _S), ("zipma", _S),
    ("mas1", _S), ("mas2", _S),
    ("countryinc", _S), ("stprinc", _S), ("ein", _S), ("former", _S), ("changed", _S),
    ("afs", _S), ("wksi", _I), ("fye", _S), ("form", _S),
    ("period", _I), ("fy", _I), ("fp", _S), ("filed", _I),
    ("accepted", TimestampType()), ("prevrpt", _I), ("detail", _I),
    ("instance", _S), ("nciks", _I), ("aciks", _S),
)  # fmt: skip

NUM_SOURCE = _fields(
    ("adsh", _S), ("tag", _S), ("version", _S), ("ddate", _I), ("qtrs", _I), ("uom", _S),
    ("segments", _S), ("coreg", _S), ("value", DecimalType(28, 4)), ("footnote", _S),
)  # fmt: skip

TAG_SOURCE = _fields(
    ("tag", _S), ("version", _S), ("custom", _I), ("abstract", _I), ("datatype", _S),
    ("iord", _S), ("crdr", _S), ("tlabel", _S), ("doc", _S),
)  # fmt: skip

PRE_SOURCE = _fields(
    ("adsh", _S), ("report", _I), ("line", _I), ("stmt", _S), ("inpth", _I), ("rfile", _S),
    ("tag", _S), ("version", _S), ("plabel", _S), ("negating", _I),
)  # fmt: skip

SOURCE_SCHEMAS: dict[str, StructType] = {
    "sub": SUB_SOURCE,
    "num": NUM_SOURCE,
    "tag": TAG_SOURCE,
    "pre": PRE_SOURCE,
}

# Columns kept in raw.fsds_sub (contract section 2.1), in contract order. The other tables keep
# every source column.
RAW_SUB_COLUMNS = (
    "adsh", "cik", "name", "sic", "countryba", "fye", "form", "period", "fy", "fp", "filed",
    "accepted", "prevrpt", "afs", "wksi", "instance",
)  # fmt: skip
RAW_COLUMNS: dict[str, tuple[str, ...]] = {
    "sub": RAW_SUB_COLUMNS,
    "num": tuple(NUM_SOURCE.fieldNames()),
    "tag": tuple(TAG_SOURCE.fieldNames()),
    "pre": tuple(PRE_SOURCE.fieldNames()),
}
