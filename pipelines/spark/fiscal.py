"""Spark implementation of the fiscal-period rules (M01 section 3.5, rules 1-6).

`pipelines/common/fiscal_ref.py` is the pure-Python reference of the same rules and the
single place the rules live: this module runs it per company (`groupBy("cik").applyInPandas`)
instead of re-implementing it, so the two cannot diverge on rules 1-6. The differential test
(`tests/integration/test_differential.py`) still asserts they agree row for row, which now
guards the plumbing (row mapping, Q4 row construction, hashing) rather than the rules.

What stays native Spark: `fact_key` / `fact_id` (`xxhash64`, so no Python hashing), the
pass-through columns (they never enter pandas) and the `derived_from` ids.

Why per company: rule 3 (label offset) is a per-company statistic and rule 5 needs a fact's FY
and 9M rows together, so a company is the smallest independent unit. The cost is that one
company's facts are held in a single pandas frame; skew on the largest filers is the thing to
watch on EMR (M1 section 3.8).
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import Any

import pandas as pd
from pyspark.sql import DataFrame
from pyspark.sql import functions as F
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DateType,
    DecimalType,
    IntegerType,
    LongType,
    StringType,
    StructField,
    StructType,
)

from pipelines.common.fiscal_ref import FactInput, derive_fiscal


def _ensure_distutils() -> None:
    """PySpark 3.5's `pandas_udf` imports `distutils`, which Python 3.12 removed. `setuptools`
    supplies a shim, normally via `distutils-precedence.pth`; if `site` skipped that file (a macOS
    `hidden` flag does this) importing `setuptools` installs the shim itself. Runs on the workers
    too, because they import this module to unpickle `_derive_company`."""
    try:
        importlib.import_module("distutils")
    except ModuleNotFoundError:
        importlib.import_module("setuptools")


_ensure_distutils()

# Columns the reference reads (`FactInput`), in order; plus `fact_id` (row identity) and
# `_series` (the `extra_key_cols` values) which only the pandas step sees.
_REF_COLUMNS = [
    "cik",
    "adsh",
    "tag",
    "taxonomy",
    "uom",
    "qtrs",
    "period_end",
    "value",
    "form",
    "filed",
    "fye_mmdd",
    "sub_fy",
    "sub_period",
]
_SEP = "\x1f"  # joins tag and series so the reference pairs FY/9M rows only within one series

_RESULT_SCHEMA = StructType(
    [
        # original rows: their own fact_id. Derived Q4 rows: the FY parent's fact_id.
        StructField("fact_id", LongType()),
        StructField("is_derived", BooleanType()),
        StructField("nm_fact_id", LongType()),  # derived rows only: the 9M parent's fact_id
        StructField("derived_value", DecimalType(28, 4)),  # derived rows only: FY - 9M
        StructField("period_start", DateType()),
        StructField("fiscal_year", IntegerType()),
        StructField("fiscal_period", StringType()),
        StructField("calendar_frame", StringType()),
        StructField("dq_flags", ArrayType(StringType())),
    ]
)


def _none_if_na(v: Any) -> Any:
    return None if v is None or v is pd.NA or (isinstance(v, float) and v != v) else v


def _derive_company(pdf: pd.DataFrame) -> pd.DataFrame:
    """Apply the reference to one company's facts and key each output row by `fact_id`."""
    facts: list[FactInput] = []
    ids: list[int] = []
    for r in pdf.astype(object).to_dict("records"):
        r = {k: _none_if_na(v) for k, v in r.items()}
        sub_fy = r["sub_fy"]
        facts.append(
            FactInput(
                cik=int(r["cik"]),
                adsh=r["adsh"],
                tag=f"{r['tag']}{_SEP}{r['_series']}",
                taxonomy=r["taxonomy"],
                uom=r["uom"],
                qtrs=int(r["qtrs"]),
                period_end=r["period_end"],
                value=r["value"],
                form=r["form"],
                filed=r["filed"],
                fye_mmdd=r["fye_mmdd"],
                sub_fy=None if sub_fy is None else int(sub_fy),
                sub_period=r["sub_period"],
            )
        )
        ids.append(int(r["fact_id"]))

    position = {id(f): i for i, f in enumerate(facts)}
    # a parent is found by its natural key within the series (the tag carries the series)
    by_natural = {
        (f.tag, f.taxonomy, f.uom, f.period_end, f.qtrs, f.adsh): ids[i]
        for i, f in enumerate(facts)
    }

    out: list[tuple[Any, ...]] = []
    for ff in derive_fiscal(facts):
        if ff.derived_from is None:
            fact_id, is_derived, nm_id, value = ids[position[id(ff.fact)]], False, None, None
        else:
            (fy_end, fy_qtrs, fy_adsh), (nm_end, nm_qtrs, nm_adsh) = ff.derived_from
            f = ff.fact
            fact_id = by_natural[(f.tag, f.taxonomy, f.uom, fy_end, fy_qtrs, fy_adsh)]
            nm_id = by_natural[(f.tag, f.taxonomy, f.uom, nm_end, nm_qtrs, nm_adsh)]
            is_derived, value = True, f.value
        out.append(
            (
                fact_id,
                is_derived,
                nm_id,
                value,
                ff.period_start,
                ff.fiscal_year,
                ff.fiscal_period,
                ff.calendar_frame,
                list(ff.dq_flags),
            )
        )
    return pd.DataFrame(out, columns=_RESULT_SCHEMA.fieldNames(), dtype=object)


def derive_fiscal_columns(df: DataFrame, extra_key_cols: Sequence[str] = ()) -> DataFrame:
    """Add fiscal columns to consolidated facts and append derived Q4 rows.

    Input columns (one row per fact version, already filtered to consolidated rows):
        cik int, adsh string, tag string, taxonomy string, uom string, qtrs int,
        period_end date, value decimal(28,4), form string, filed date,
        fye_mmdd string      -- sub.fye of the SAME submission (rule 1)
        sub_fy int           -- sub.fy of that submission
        sub_period date      -- sub.period of that submission
    plus any pass-through columns, which must be preserved.

    Output: every input column, plus
        period_start date          -- approximate: period_end - qtrs quarters + 1 day; null for instants
        fiscal_year int            -- y_end(period_end, fye_mmdd) + label_offset(cik)   (rules 2-3)
        fiscal_period string       -- FY | Q1..Q4 | H1 | 9M | INSTANT                   (rule 4)
        calendar_frame string      -- CY2023 | CY2023Q3 | CY2023Q3I | null              (rule 6)
        dq_flags array<string>     -- empty array (not null) when there are no flags
        fact_key bigint            -- xxhash64(cik, tag, taxonomy, period_end, qtrs, uom, *extra_key_cols)
        fact_id bigint             -- xxhash64(fact_key, adsh)
        derived_from array<bigint> -- null, except on derived Q4 rows: [FY fact_id, 9M fact_id]

    Rule 5 appends one row per derivable Q4 (flow concepts only; never instants or per-share and
    ratio concepts): fiscal_period 'Q4', qtrs 1, period_end = the FY end, form '10-K', the FY
    row's adsh, value = FY - 9M, dq_flags containing 'q4_derived'. A derived row is its FY
    parent's row carried through whole (pass-through columns included) with `qtrs` and `value`
    replaced.

    `extra_key_cols` names extra columns that identify a series and enter fact_key and the Q4
    pairing (for example ['segments'] for facts_dimensional). The default is the consolidated
    case.

    Preconditions (not checked here): the natural key (cik, adsh, tag, taxonomy, uom, qtrs,
    period_end, *extra_key_cols) is unique, because rows are identified by `fact_id`; and `value`
    is non-null, because the reference asserts it when it derives a Q4. `normalize_facts` already
    guarantees both.
    """
    extra = list(extra_key_cols)

    def fact_key(qtrs: Any) -> Any:
        return F.xxhash64("cik", "tag", "taxonomy", "period_end", qtrs, "uom", *extra)

    keyed = df.withColumn("fact_key", fact_key(F.col("qtrs"))).withColumn(
        "fact_id", F.xxhash64("fact_key", "adsh")
    )
    series = F.to_json(F.struct(*extra)) if extra else F.lit("")
    derived = (
        keyed.select("fact_id", *_REF_COLUMNS, series.alias("_series"))
        .groupBy("cik")
        .applyInPandas(_derive_company, _RESULT_SCHEMA)
    )

    out_cols = [
        *df.columns,
        "period_start",
        "fiscal_year",
        "fiscal_period",
        "calendar_frame",
        "dq_flags",
        "fact_key",
        "fact_id",
        "derived_from",
    ]
    fiscal = ["period_start", "fiscal_year", "fiscal_period", "calendar_frame", "dq_flags"]

    originals = keyed.join(
        derived.where(~F.col("is_derived")).select("fact_id", *fiscal), "fact_id"
    ).withColumn("derived_from", F.lit(None).cast(ArrayType(LongType())))

    q4_rows = (
        derived.where(F.col("is_derived"))
        .select(
            F.col("fact_id").alias("_fy_fact_id"),
            F.col("nm_fact_id").alias("_nm_fact_id"),
            F.col("derived_value").alias("_q4_value"),
            *fiscal,
        )
        .join(keyed, F.col("_fy_fact_id") == keyed["fact_id"])
        .withColumn("derived_from", F.array("_fy_fact_id", "_nm_fact_id"))
        .withColumn("qtrs", F.lit(1).cast(df.schema["qtrs"].dataType))
        .withColumn("value", F.col("_q4_value"))
        .withColumn("fact_key", fact_key(F.col("qtrs")))
        .withColumn("fact_id", F.xxhash64("fact_key", "adsh"))
    )
    return originals.select(out_cols).unionByName(q4_rows.select(out_cols))
