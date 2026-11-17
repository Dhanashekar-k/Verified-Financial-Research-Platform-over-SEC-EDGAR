"""Spark twin of `tests/unit/test_fiscal.py`: `derive_fiscal_columns` over DataFrames.

The same hand-derived scenarios (`tests/fiscal_cases.py`) are run through Spark and checked by the
same checker as the pure-Python reference. `derived_from` ids are resolved back to natural keys by
looking them up in the output's own `fact_id` column; no xxhash is recomputed in Python.

Needs a JVM (see tests/integration/conftest.py; the `spark` fixture skips without one).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.types import (
    DateType,
    DecimalType,
    IntegerType,
    StringType,
    StructField,
    StructType,
)

from pipelines.common.fiscal_ref import FactInput
from pipelines.spark.fiscal import derive_fiscal_columns
from tests.fiscal_cases import (
    FISCAL_YEAR_SCENARIOS,
    INPUT_FIELDS,
    K23,
    NO_DERIVE_SCENARIOS,
    OT_K,
    Q3_23,
    Q4_SCENARIOS,
    FiscalScenario,
    Sub,
    check_fiscal_scenario,
    check_no_derivation,
)

FACT_SCHEMA = StructType(
    [
        StructField("cik", IntegerType()),
        StructField("adsh", StringType()),
        StructField("tag", StringType()),
        StructField("taxonomy", StringType()),
        StructField("uom", StringType()),
        StructField("qtrs", IntegerType()),
        StructField("period_end", DateType()),
        StructField("value", DecimalType(28, 4)),
        StructField("form", StringType()),
        StructField("filed", DateType()),
        StructField("fye_mmdd", StringType()),
        StructField("sub_fy", IntegerType()),
        StructField("sub_period", DateType()),
    ]
)

EXPECTED_DTYPES = {
    "cik": "int",
    "adsh": "string",
    "tag": "string",
    "taxonomy": "string",
    "uom": "string",
    "qtrs": "int",
    "period_end": "date",
    "value": "decimal(28,4)",
    "form": "string",
    "filed": "date",
    "fye_mmdd": "string",
    "sub_fy": "int",
    "sub_period": "date",
    "period_start": "date",
    "fiscal_year": "int",
    "fiscal_period": "string",
    "calendar_frame": "string",
    "dq_flags": "array<string>",
    "fact_key": "bigint",
    "fact_id": "bigint",
    "derived_from": "array<bigint>",
}


def to_df(
    spark: SparkSession,
    facts: list[FactInput],
    extras: list[tuple[Any, ...]] | None = None,
    extra_fields: list[StructField] | None = None,
) -> DataFrame:
    """Build the input DataFrame with the explicit schema; `extras[i]` extends row i."""
    fields = list(FACT_SCHEMA.fields) + list(extra_fields or [])
    rows = []
    for i, f in enumerate(facts):
        base = tuple(getattr(f, k) for k in INPUT_FIELDS)
        rows.append(base + (extras[i] if extras else ()))
    return spark.createDataFrame(rows, StructType(fields))


def collect_rows(out: DataFrame) -> list[dict[str, Any]]:
    """Collect the output as row dicts; `derived_from` ids are resolved by joining on fact_id."""
    raw = [r.asDict() for r in out.collect()]
    by_id = {r["fact_id"]: r for r in raw}
    assert len(by_id) == len(raw), "fact_id must be unique across the output"
    rows = []
    for r in raw:
        assert r["dq_flags"] is not None, "dq_flags must be an empty array, never null"
        resolved = None
        if r["derived_from"] is not None:
            resolved = tuple(
                (by_id[i]["period_end"], by_id[i]["qtrs"], by_id[i]["adsh"])
                for i in r["derived_from"]
            )
        rows.append({**r, "dq_flags": frozenset(r["dq_flags"]), "derived_from": resolved})
    return rows


def run(
    spark: SparkSession, facts: list[FactInput], extra_key_cols: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    return collect_rows(derive_fiscal_columns(to_df(spark, facts), extra_key_cols))


@pytest.mark.parametrize("scenario", FISCAL_YEAR_SCENARIOS, ids=repr)
def test_spark_fiscal_year_period_and_frame(spark: SparkSession, scenario: FiscalScenario) -> None:
    """Rules 1-4 and 6 in Spark: fiscal_year (y_end + per-company label offset), fiscal_period,
    calendar_frame and dq_flags are as hand-derived; input rows pass through unchanged.
    """
    check_fiscal_scenario(scenario, run(spark, list(scenario.facts)))


@pytest.mark.parametrize("scenario", Q4_SCENARIOS, ids=repr)
def test_spark_derives_q4_as_fy_minus_9m(spark: SparkSession, scenario: FiscalScenario) -> None:
    """Rule 5 in Spark: one derived Q4 row per FY/9M pair (exact 91-day and FSDS-rounded 92-day
    pairings, two fiscal years, an offset -1 retailer); derived_from ids resolve to the parents'
    natural keys (FY first, then 9M); the parents survive unchanged.
    """
    check_fiscal_scenario(scenario, run(spark, list(scenario.facts)))


@pytest.mark.parametrize("scenario", NO_DERIVE_SCENARIOS, ids=repr)
def test_spark_does_not_derive_q4_when_it_would_be_wrong(
    spark: SparkSession, scenario: FiscalScenario
) -> None:
    """Rule 5 negatives in Spark: EPS and 'pure' uoms, instants, unpaired FY or 9M, mismatched
    uom/tag/taxonomy/cik, a 9M from another year, and a direct Q4 in the same accession.
    """
    rows = run(spark, list(scenario.facts))
    check_no_derivation(rows, len(scenario.facts))
    check_fiscal_scenario(scenario, rows)


def test_spark_output_schema_types(spark: SparkSession) -> None:
    """Docstring contract: input columns keep their types (value stays decimal(28,4), so the
    Iceberg write needs no cast) and the new columns have the documented types.
    """
    df = to_df(spark, [K23.fact("Revenues", date(2023, 9, 30), 4)])
    out = derive_fiscal_columns(df)
    assert dict(out.dtypes) == EXPECTED_DTYPES


def test_spark_dq_flags_is_an_empty_array_not_null(spark: SparkSession) -> None:
    """Docstring: dq_flags is an empty array (not null) when there are no flags, so
    `array_contains` and `size` work downstream without null handling.
    """
    out = derive_fiscal_columns(to_df(spark, [K23.fact("Revenues", date(2023, 9, 30), 4)]))
    (row,) = out.collect()
    assert row["dq_flags"] == []
    assert row["derived_from"] is None


def test_spark_preserves_pass_through_columns(spark: SparkSession) -> None:
    """Docstring: extra input columns are preserved on every input row."""
    facts = [
        K23.fact("Revenues", date(2023, 9, 30), 4, "5.0000"),
        Q3_23.fact("Revenues", date(2023, 7, 1), 3, "3.0000"),
    ]
    extras = [("2023q4", 11), ("2023q3", 22)]
    fields = [StructField("src_quarter", StringType()), StructField("batch", IntegerType())]
    out = derive_fiscal_columns(to_df(spark, facts, extras, fields))
    assert {"src_quarter", "batch"} <= set(out.columns)
    kept = {(r["adsh"], r["qtrs"]): (r["src_quarter"], r["batch"]) for r in out.collect()
            if r["derived_from"] is None}  # fmt: skip
    assert kept == {(K23.adsh, 4): ("2023q4", 11), (Q3_23.adsh, 3): ("2023q3", 22)}


def test_spark_fact_key_groups_versions_and_fact_id_is_unique(spark: SparkSession) -> None:
    """Contract section 2.2: fact_key hashes (cik, tag, taxonomy, period_end, qtrs, uom) and
    nothing else, so the same fact in two accessions shares a fact_key but not a fact_id, and
    changing any one of the six fields gives a different fact_key.
    """
    k22 = Sub(K23.cik, "0000320193-22-000108", "10-K", date(2022, 10, 28), "0930", 2022,
              date(2022, 9, 24))  # fmt: skip
    pe = date(2023, 9, 30)
    facts = [
        K23.fact("Revenues", pe, 4),  # 0 base
        k22.fact("Revenues", pe, 4),  # 1 same natural key, other accession
        K23.fact("OtherTag", pe, 4),  # 2 tag differs
        K23.fact("Revenues", pe, 4, taxonomy="custom"),  # 3 taxonomy differs
        K23.fact("Revenues", pe, 4, uom="EUR"),  # 4 uom differs
        K23.fact("Revenues", pe, 1),  # 5 qtrs differs
        K23.fact("Revenues", date(2023, 9, 29), 4),  # 6 period_end differs
        OT_K.fact("Revenues", pe, 4),  # 7 cik differs
    ]
    rows = run(spark, facts)
    by_input = {}
    for r in rows:
        by_input[(r["adsh"], r["cik"], r["tag"], r["taxonomy"], r["uom"], r["qtrs"],
                  r["period_end"])] = r  # fmt: skip

    def find(f: FactInput) -> dict[str, Any]:
        return by_input[(f.adsh, f.cik, f.tag, f.taxonomy, f.uom, f.qtrs, f.period_end)]

    keys = [find(f)["fact_key"] for f in facts]
    ids = [find(f)["fact_id"] for f in facts]
    assert keys[0] == keys[1]
    assert ids[0] != ids[1]
    assert len(set(keys)) == len(facts) - 1  # only 0 and 1 share a key
    assert len(set(ids)) == len(facts)


def test_spark_derived_q4_has_its_own_fact_key_and_id(spark: SparkSession) -> None:
    """Contract section 2.2: the derived Q4 (qtrs 1) has a fact_key different from the FY fact's
    (qtrs 4), and a fact_id of its own; derived_from points at the FY and 9M fact_ids.
    """
    out = derive_fiscal_columns(
        to_df(
            spark,
            [
                K23.fact("Revenues", date(2023, 9, 30), 4, "10.0000"),
                Q3_23.fact("Revenues", date(2023, 7, 1), 3, "7.0000"),
            ],
        )
    )
    raw = [r.asDict() for r in out.collect()]
    fy = next(r for r in raw if r["qtrs"] == 4)
    nine = next(r for r in raw if r["qtrs"] == 3)
    q4 = next(r for r in raw if r["derived_from"] is not None)
    assert q4["fact_key"] not in {fy["fact_key"], nine["fact_key"]}
    assert q4["fact_id"] not in {fy["fact_id"], nine["fact_id"]}
    assert q4["derived_from"] == [fy["fact_id"], nine["fact_id"]]
    assert q4["value"] == Decimal("3.0000")


@pytest.mark.parametrize(
    ("period_end", "qtrs", "expected"),
    [
        (date(2023, 9, 30), 4, date(2022, 10, 1)),  # Sep 30 minus 12 months = 2022-09-30, +1 day
        (date(2023, 6, 30), 1, date(2023, 4, 1)),  # Jun 30 minus 3 months = Mar 31, +1 day
        (date(2022, 12, 31), 2, date(2022, 7, 1)),  # Dec 31 minus 6 months = Jun 30, +1 day
        (date(2023, 7, 1), 3, date(2022, 10, 2)),  # Jul 1 minus 9 months = Oct 1, +1 day
        (date(2023, 9, 30), 0, None),  # instants have no start
    ],
)
def test_spark_period_start_is_period_end_minus_qtrs_quarters_plus_a_day(
    spark: SparkSession, period_end: date, qtrs: int, expected: date | None
) -> None:
    """Docstring: period_start ~ period_end - qtrs quarters + 1 day, null for instants."""
    (row,) = run(spark, [K23.fact("OneOffTag", period_end, qtrs)])
    assert row["period_start"] == expected


def _seg_df(spark: SparkSession, spec: list[tuple[Sub, int, date, str, str]]) -> DataFrame:
    """spec items: (sub, qtrs, period_end, value, segments) for tag 'Revenues'."""
    facts = [s.fact("Revenues", pe, q, v) for s, q, pe, v, _ in spec]
    return to_df(
        spark, facts, [(seg,) for *_, seg in spec], [StructField("segments", StringType())]
    )


def test_spark_extra_key_cols_keep_segment_series_apart_in_q4_pairing(
    spark: SparkSession,
) -> None:
    """With extra_key_cols=['segments'], FY and 9M pair only within the same segments value:
    A (100 - 60) and B (200 - 150) each derive their own Q4, and the derived rows carry their
    series' segments value.
    """
    fy, nm = date(2023, 9, 30), date(2023, 7, 1)
    df = _seg_df(
        spark,
        [
            (K23, 4, fy, "100.0000", "Seg=A"),
            (K23, 4, fy, "200.0000", "Seg=B"),
            (Q3_23, 3, nm, "60.0000", "Seg=A"),
            (Q3_23, 3, nm, "150.0000", "Seg=B"),
        ],
    )
    out = derive_fiscal_columns(df, ["segments"])
    derived = [r for r in out.collect() if r["derived_from"] is not None]
    assert {r["segments"]: r["value"] for r in derived} == {
        "Seg=A": Decimal("40.0000"),
        "Seg=B": Decimal("50.0000"),
    }
    assert len(out.collect()) == 6


def test_spark_extra_key_cols_block_pairing_across_different_segments(
    spark: SparkSession,
) -> None:
    """With extra_key_cols=['segments'], an FY of Seg=A and a 9M of Seg=B are different series:
    nothing is derived.
    """
    df = _seg_df(
        spark,
        [
            (K23, 4, date(2023, 9, 30), "100.0000", "Seg=A"),
            (Q3_23, 3, date(2023, 7, 1), "60.0000", "Seg=B"),
        ],
    )
    out = derive_fiscal_columns(df, ["segments"])
    assert out.count() == 2
    assert all(r["derived_from"] is None for r in out.collect())


def test_spark_extra_key_cols_enter_fact_key(spark: SparkSession) -> None:
    """With extra_key_cols=['segments'], two rows identical except for `segments` get different
    fact_keys (they are different series), while equal segments give equal fact_keys.
    """
    pe = date(2023, 9, 30)
    df = _seg_df(
        spark,
        [
            (K23, 4, pe, "1.0000", "Seg=A"),
            (K23, 4, pe, "2.0000", "Seg=B"),
            (Q3_23, 4, pe, "3.0000", "Seg=A"),  # same series as row 0, other accession
        ],
    )
    out = derive_fiscal_columns(df, ["segments"])
    keys = {(r["adsh"], r["segments"]): r["fact_key"] for r in out.collect()}
    assert keys[(K23.adsh, "Seg=A")] != keys[(K23.adsh, "Seg=B")]
    assert keys[(K23.adsh, "Seg=A")] == keys[(Q3_23.adsh, "Seg=A")]
