"""ADR-022 rules against the Spark twin: determinism, Q4 eligibility and fact_id uniqueness.

Spark twin of `tests/unit/test_fiscal_duckdb_rules.py`, over the same hand-derived scenarios S1 to
S5 and the same seeded `tie_heavy` data (`tests/tie_cases.py`), compared with the oracle
`tests.proposed_rules.reference_with_proposed_rules`. Rows are given in several orders and the
DataFrame is repartitioned for some of them, so a tie broken by scan position or by partition
placement changes the answer. `derived_from` ids are resolved back to natural keys through the
output's own `fact_id`; no xxhash is recomputed in Python.

Needs a JVM (tests/integration/conftest.py): the `spark` fixture fails without one, never skips.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from pyspark.sql import SparkSession
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
    INPUT_FIELDS,
    FiscalScenario,
    check_fiscal_scenario,
    check_no_derivation,
)
from tests.proposed_rules import reference_with_proposed_rules
from tests.tie_cases import (
    ALL_SCENARIOS,
    REV,
    S1_INELIGIBLE,
    S1_MIXED,
    S2,
    S3_SMALLEST_IS_ZERO,
    S4,
    S5,
    S5_END,
    S5_K,
    compare_to_reference,
    id_signature,
    nine_month_accession_variants,
    normalise_output,
    reorder,
    tie_heavy,
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

# (input order, number of partitions the DataFrame is repartitioned into; 1 = not repartitioned)
LAYOUTS = [("given", 1), ("reversed", 1), ("shuffle_1", 4)]
LAYOUT_IDS = [f"{order}-p{n}" for order, n in LAYOUTS]
ID_ORDER_SCENARIOS = [S1_MIXED, S2, S3_SMALLEST_IS_ZERO, S4, S5]


def derive_raw(
    spark: SparkSession, facts: Sequence[FactInput], partitions: int = 1
) -> list[dict[str, Any]]:
    """Run the Spark twin over `facts` (in the order given, then repartitioned) and collect."""
    rows = [tuple(getattr(f, k) for k in INPUT_FIELDS) for f in facts]
    df = spark.createDataFrame(rows, FACT_SCHEMA)
    if partitions > 1:
        df = df.repartition(partitions)
    return [r.asDict() for r in derive_fiscal_columns(df).collect()]


def run(
    spark: SparkSession,
    facts: Sequence[FactInput],
    partitions: int = 1,
    full_keys: bool = False,
) -> list[dict[str, Any]]:
    return normalise_output(derive_raw(spark, facts, partitions), full_keys=full_keys)


@pytest.mark.parametrize(("order", "partitions"), LAYOUTS, ids=LAYOUT_IDS)
@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=repr)
def test_spark_scenarios_give_the_hand_derived_answer_in_every_input_order(
    spark: SparkSession, scenario: FiscalScenario, order: str, partitions: int
) -> None:
    """S1 (eligibility), S2 (fiscal-year change), S3 (label-offset tie), S4 (9M tie across period
    ends) and S5 (identity): the twin's rows and derived Q4s equal the hand-derived expectations
    whatever order the rows came in and however the DataFrame is partitioned.
    """
    check_fiscal_scenario(scenario, run(spark, reorder(scenario.facts, order), partitions))


@pytest.mark.parametrize(("order", "partitions"), LAYOUTS, ids=LAYOUT_IDS)
@pytest.mark.parametrize("scenario", S1_INELIGIBLE, ids=repr)
def test_spark_fy_row_from_a_non_annual_form_is_kept_but_never_a_parent(
    spark: SparkSession, scenario: FiscalScenario, order: str, partitions: int
) -> None:
    """Rule 2: an FY row from a 10-Q, 10-Q/A, S-1 or 10-KT/A filing stays in the output as an
    ordinary row (no derived row, no q4_derived flag anywhere), even with a 9M row to pair with.
    """
    rows = run(spark, reorder(scenario.facts, order), partitions)
    check_no_derivation(rows, len(scenario.facts))


@pytest.mark.parametrize(("order", "partitions"), LAYOUTS, ids=LAYOUT_IDS)
def test_spark_ten_q_fy_row_does_not_add_a_second_q4_or_change_the_first(
    spark: SparkSession, order: str, partitions: int
) -> None:
    """Rule 2, mixed: with a 10-Q FY row (999) filed after the 10-K FY row (100) and a 9M (60),
    there is exactly one derived Q4 (40), stamped with the 10-K's accession; nothing is stamped
    with the 10-Q's accession and no value is 999 - 60 = 939.
    """
    rows = run(spark, reorder(S1_MIXED.facts, order), partitions)
    derived = [r for r in rows if r["derived_from"] is not None]
    assert [(r["adsh"], str(r["value"])) for r in derived] == [
        (S1_MIXED.expect[0].sub.adsh, "40.0000")
    ]


@pytest.mark.parametrize("scenario", ID_ORDER_SCENARIOS, ids=repr)
def test_spark_fact_key_and_fact_id_do_not_depend_on_input_row_order(
    spark: SparkSession, scenario: FiscalScenario
) -> None:
    """Rule 1: the output, hash columns included, is one set of (row, fact_key, fact_id) whatever
    the order of the input rows and the partitioning: a derived Q4's id comes from its parents'
    natural keys, never from a row position, so a rerun is idempotent.
    """
    sigs = [id_signature(derive_raw(spark, reorder(scenario.facts, o), n)) for o, n in LAYOUTS]
    assert all(s == sigs[0] for s in sigs), "fact_key / fact_id changed with the input order"


def test_spark_derived_q4_fact_id_differs_from_a_real_row_with_the_same_key(
    spark: SparkSession,
) -> None:
    """S5: a real qtrs-1 row and the derived Q4 share (cik, tag, taxonomy, period_end, qtrs, uom)
    and the 10-K's accession. They have the same fact_key (the premise: fact_key hashes only the
    six natural fields) but must have different fact_ids, and every fact_id in the output is
    unique, or a downstream MERGE on fact_id would overwrite one with the other.
    """
    raw = derive_raw(spark, S5.facts)
    same = [
        r
        for r in raw
        if (r["adsh"], r["tag"], r["qtrs"], r["period_end"]) == (S5_K.adsh, REV, 1, S5_END)
    ]
    assert len(same) == 2, "expected the real Q1 row and the derived Q4 row"
    assert {r["derived_from"] is None for r in same} == {True, False}
    assert same[0]["fact_key"] == same[1]["fact_key"], "premise: same fact_key"
    assert same[0]["fact_id"] != same[1]["fact_id"], "derived and real rows share a fact_id"
    assert len({r["fact_id"] for r in raw}) == len(raw)


def test_spark_derived_q4_fact_id_incorporates_the_nine_month_accession(
    spark: SparkSession,
) -> None:
    """Two inputs that differ only in the accession of the 9M parent give a derived Q4 with the
    same fact_key and accession (the 10-K's) but a different fact_id: the id is
    xxhash64(fact_key, accession, nine_month_accession), so a Q4 derived from a different 9M
    version is a different fact.
    """
    facts_a, facts_b = nine_month_accession_variants()
    (qa,) = [r for r in derive_raw(spark, facts_a) if r["derived_from"] is not None]
    (qb,) = [r for r in derive_raw(spark, facts_b) if r["derived_from"] is not None]
    assert (qa["adsh"], qa["fact_key"]) == (qb["adsh"], qb["fact_key"])
    assert qa["fact_id"] != qb["fact_id"]


@pytest.mark.parametrize(
    ("order", "partitions"), [("given", 1), ("shuffle_2", 4)], ids=["given-p1", "shuffle_2-p4"]
)
@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_spark_agrees_with_the_proposed_rules_reference_on_tie_heavy_data(
    spark: SparkSession, seed: int, order: str, partitions: int
) -> None:
    """Over roughly 700 generated rows per seed (ties on (filed, accession) and on value, 2-3
    period-end spellings, FY rows in 10-Qs, 9M rows in 10-Ks, label-offset ties, NULLs): the
    twin has exactly the oracle's rows, and for each row the same period_start, fiscal_year,
    fiscal_period, calendar_frame, dq_flags, value and derived_from parents (resolved to full
    natural keys). fact_id is unique, and the result does not depend on row order or partitions.
    """
    facts = tie_heavy(seed)
    rows = run(spark, reorder(facts, order), partitions, full_keys=True)
    problems = compare_to_reference(rows, reference_with_proposed_rules(facts))
    assert not problems, f"{len(problems)}+ mismatches, first ones:\n" + "\n".join(problems)
