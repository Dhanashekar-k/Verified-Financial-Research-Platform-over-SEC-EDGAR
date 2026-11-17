"""ADR-022 rules against the DuckDB twin: determinism, Q4 eligibility and fact_id uniqueness.

The hand-derived scenarios S1 to S5 and the seeded `tie_heavy` generator live in
`tests/tie_cases.py`; the oracle for the random data is
`tests.proposed_rules.reference_with_proposed_rules`. Every scenario is run with the rows inserted
in five orders (as given, reversed, three seeded shuffles) and must give the same, hand-derived
answer in all of them. `derived_from` ids are resolved back to natural keys through the output's
own `fact_id`; hash VALUES are never compared with any other engine.

No JVM and no network: an in-memory DuckDB connection per run. The first group of tests checks
the test data itself (the generator and the oracle) and passes today; the rest fail until the
twin implements the rules.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import duckdb
import pytest

from pipelines.common.fiscal_ref import FactInput
from pipelines.duckdb.normalize_facts_duckdb import derive_fiscal
from tests.fiscal_cases import (
    INPUT_FIELDS,
    FiscalScenario,
    check_fiscal_scenario,
    check_no_derivation,
)
from tests.proposed_rules import reference_with_proposed_rules
from tests.tie_cases import (
    ALL_SCENARIOS,
    FLOW_TAGS,
    ORDERS,
    REV,
    S1_INELIGIBLE,
    S1_MIXED,
    S5,
    S5_END,
    S5_K,
    compare_to_reference,
    id_signature,
    nine_month_accession_variants,
    normalise_output,
    offsets_tie,
    reorder,
    signature,
    tie_heavy,
)

FACT_COLUMNS = [
    ("cik", "INTEGER"),
    ("adsh", "VARCHAR"),
    ("tag", "VARCHAR"),
    ("taxonomy", "VARCHAR"),
    ("uom", "VARCHAR"),
    ("qtrs", "INTEGER"),
    ("period_end", "DATE"),
    ("value", "DECIMAL(28,4)"),
    ("form", "VARCHAR"),
    ("filed", "DATE"),
    ("fye_mmdd", "VARCHAR"),
    ("sub_fy", "INTEGER"),
    ("sub_period", "DATE"),
]
SEEDS = range(1, 9)


def derive_raw(facts: Sequence[FactInput]) -> list[dict[str, Any]]:
    """Run the DuckDB twin over `facts` (inserted in the order given) in a fresh connection."""
    con = duckdb.connect(":memory:")
    try:
        ddl = ", ".join(f"{c} {t}" for c, t in FACT_COLUMNS)
        con.execute(f"CREATE TABLE src ({ddl})")
        marks = ", ".join("?" for _ in FACT_COLUMNS)
        rows = [tuple(getattr(f, k) for k in INPUT_FIELDS) for f in facts]
        con.executemany(f"INSERT INTO src VALUES ({marks})", rows)
        rel = derive_fiscal(con, "src")
        names = list(rel.columns)
        return [dict(zip(names, r, strict=True)) for r in rel.fetchall()]
    finally:
        con.close()


def run(facts: Sequence[FactInput], full_keys: bool = False) -> list[dict[str, Any]]:
    return normalise_output(derive_raw(facts), full_keys=full_keys)


# --------------------------------------------------------------------------------------
# The test data itself (these pass today)
# --------------------------------------------------------------------------------------


def test_tie_heavy_is_deterministic_and_seeds_differ() -> None:
    """The generator is a pure function of its seed (so a red run is reproducible), different
    seeds give different data, and every natural key is unique (the generator asserts it)."""
    assert tie_heavy(1) == tie_heavy(1)
    assert tie_heavy(1) != tie_heavy(2)
    assert len(tie_heavy(3, n_companies=5)) < len(tie_heavy(3, n_companies=12))


@pytest.mark.parametrize("seed", SEEDS)
def test_tie_heavy_contains_the_ties_and_ineligible_rows_the_rules_are_about(seed: int) -> None:
    """Each seed has FY and 9M rows tied on (accession) with equal and with different values,
    FY rows from non-annual forms, 10-K/10-KT submissions whose label offsets tie, NULL values,
    all four annual and quarterly form families, and enough derivable Q4s. (That the reference
    gives another answer on reversed input is checked in test_fiscal_reference_proposed.py.)
    """
    facts = tie_heavy(seed)
    by_series: dict[tuple[Any, ...], list[FactInput]] = {}
    for f in facts:
        if f.qtrs in (3, 4):
            by_series.setdefault((f.cik, f.adsh, f.tag, f.qtrs), []).append(f)
    tied = [rows for rows in by_series.values() if len(rows) >= 2]
    assert any(len({r.value for r in rows}) == 1 for rows in tied), "no value tie"
    assert any(len({r.value for r in rows}) > 1 for rows in tied), "no tie across values"
    assert any(f.qtrs == 4 and f.form not in ("10-K", "10-KT", "10-K/A") for f in facts)
    assert any(f.qtrs == 3 and f.form in ("10-K", "10-KT") for f in facts)
    assert {"10-K", "10-K/A", "10-KT", "10-Q", "10-Q/A", "S-1"} <= {f.form for f in facts}
    assert any(f.value is None for f in facts)
    assert any(f.uom == "USD/shares" for f in facts) and any(f.qtrs == 0 for f in facts)
    assert offsets_tie(facts), "no company has two label offsets in equal numbers"
    assert len({f.filed for f in facts}) < len({f.adsh for f in facts}), "filing dates do not tie"
    expected = reference_with_proposed_rules(facts)
    assert sum(1 for ff in expected if ff.derived_from is not None) >= 10
    assert set(FLOW_TAGS) == {ff.fact.tag for ff in expected if ff.derived_from is not None}


@pytest.mark.parametrize("seed", SEEDS)
def test_oracle_is_independent_of_input_order(seed: int) -> None:
    """`reference_with_proposed_rules` gives one answer for every input order (otherwise the
    twins could not be held to it): the oracle itself is sound on the generated data."""
    facts = tie_heavy(seed)
    want = signature(reference_with_proposed_rules(facts))
    for order in ORDERS:
        assert signature(reference_with_proposed_rules(reorder(facts, order))) == want, order


# --------------------------------------------------------------------------------------
# S1 to S5 against the twin
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=repr)
def test_duckdb_scenarios_give_the_hand_derived_answer_in_every_input_order(
    scenario: FiscalScenario, order: str
) -> None:
    """S1 (eligibility), S2 (fiscal-year change), S3 (label-offset tie), S4 (9M tie across period
    ends) and S5 (identity): the twin's rows and derived Q4s equal the hand-derived expectations
    whatever order the rows were inserted in. A twin that breaks a tie by scan position, or
    derives from a 10-Q's trailing-twelve-month FY row, gets at least one order wrong.
    """
    check_fiscal_scenario(scenario, run(reorder(scenario.facts, order)))


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("scenario", S1_INELIGIBLE, ids=repr)
def test_duckdb_fy_row_from_a_non_annual_form_is_kept_but_never_a_parent(
    scenario: FiscalScenario, order: str
) -> None:
    """Rule 2: an FY row from a 10-Q, 10-Q/A, S-1 or 10-KT/A filing stays in the output as an
    ordinary row (no derived row, no q4_derived flag anywhere), even with a 9M row to pair with.
    """
    rows = run(reorder(scenario.facts, order))
    check_no_derivation(rows, len(scenario.facts))


@pytest.mark.parametrize("order", ORDERS)
def test_duckdb_ten_q_fy_row_does_not_add_a_second_q4_or_change_the_first(order: str) -> None:
    """Rule 2, mixed: with a 10-Q FY row (999) filed after the 10-K FY row (100) and a 9M (60),
    there is exactly one derived Q4 (40), stamped with the 10-K's accession; nothing is stamped
    with the 10-Q's accession and no value is 999 - 60 = 939.
    """
    rows = run(reorder(S1_MIXED.facts, order))
    derived = [r for r in rows if r["derived_from"] is not None]
    assert [(r["adsh"], str(r["value"])) for r in derived] == [
        (S1_MIXED.expect[0].sub.adsh, "40.0000")
    ]


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=repr)
def test_duckdb_fact_key_and_fact_id_do_not_depend_on_input_row_order(
    scenario: FiscalScenario,
) -> None:
    """Rule 1: the output, hash columns included, is one set of (row, fact_key, fact_id) whatever
    the order of the input rows. A derived Q4's id comes from its parents' natural keys, never
    from a row position, so a rerun with a different scan order is idempotent.
    """
    sigs = [id_signature(derive_raw(reorder(scenario.facts, o))) for o in ORDERS]
    assert all(s == sigs[0] for s in sigs), "fact_key / fact_id changed with the input order"


# --------------------------------------------------------------------------------------
# Rule 3: fact_id identity
# --------------------------------------------------------------------------------------


def test_duckdb_derived_q4_fact_id_differs_from_a_real_row_with_the_same_key() -> None:
    """S5: a real qtrs-1 row and the derived Q4 share (cik, tag, taxonomy, period_end, qtrs, uom)
    and the 10-K's accession. They have the same fact_key (the premise: fact_key hashes only the
    six natural fields) but must have different fact_ids, and every fact_id in the output is
    unique, or a downstream MERGE on fact_id would overwrite one with the other.
    """
    raw = derive_raw(S5.facts)
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


def test_duckdb_derived_q4_fact_id_incorporates_the_nine_month_accession() -> None:
    """Two inputs that differ only in the accession of the 9M parent give a derived Q4 with the
    same fact_key and accession (the 10-K's) but a different fact_id: the id is
    hash(fact_key, accession, nine_month_accession), so a Q4 derived from a different 9M version
    is a different fact.
    """
    facts_a, facts_b = nine_month_accession_variants()
    (qa,) = [r for r in derive_raw(facts_a) if r["derived_from"] is not None]
    (qb,) = [r for r in derive_raw(facts_b) if r["derived_from"] is not None]
    assert (qa["adsh"], qa["fact_key"]) == (qb["adsh"], qb["fact_key"])
    assert qa["fact_id"] != qb["fact_id"]


# --------------------------------------------------------------------------------------
# Seeded tie-heavy data against the oracle
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("order", ["given", "reversed", "shuffle_1"])
@pytest.mark.parametrize("seed", SEEDS)
def test_duckdb_agrees_with_the_proposed_rules_reference_on_tie_heavy_data(
    seed: int, order: str
) -> None:
    """Over roughly 700 generated rows per seed (ties on (filed, accession) and on value, 2-3
    period-end spellings, FY rows in 10-Qs, 9M rows in 10-Ks, label-offset ties, NULLs): the
    twin has exactly the oracle's rows, and for each row the same period_start, fiscal_year,
    fiscal_period, calendar_frame, dq_flags, value and derived_from parents (resolved to full
    natural keys). fact_id is unique, and the result does not depend on the row order.
    """
    facts = tie_heavy(seed)
    rows = run(reorder(facts, order), full_keys=True)
    problems = compare_to_reference(rows, reference_with_proposed_rules(facts))
    assert not problems, f"{len(problems)}+ mismatches, first ones:\n" + "\n".join(problems)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_duckdb_tie_heavy_output_is_identical_across_input_orders(seed: int) -> None:
    """Rule 1 directly: the same facts in five orders give one set of rows (hash columns
    included). A twin may be wrong against the oracle yet consistent, or the other way round;
    this pins the consistency on its own.
    """
    facts = tie_heavy(seed)
    sigs = [id_signature(derive_raw(reorder(facts, o))) for o in ORDERS]
    assert all(s == sigs[0] for s in sigs)


def test_random_shuffles_are_really_different_orders() -> None:
    """The orders in `ORDERS` are not all the same permutation (otherwise the order tests
    would be vacuous): on 50 facts, given, reversed and each shuffle are pairwise different."""
    facts = tie_heavy(1)[:50]
    permutations = {tuple(map(id, reorder(facts, o))) for o in ORDERS}
    assert len(permutations) == len(ORDERS)
