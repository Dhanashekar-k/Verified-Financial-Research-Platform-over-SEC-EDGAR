"""Unit tests for `pipelines/duckdb/explain_differences.py`.

The real-data differential between Spark and DuckDB (M01-benchmark.json) found rows the two
engines disagree on. This module sorts every differing row into the cause it traces to, so the
disagreement is explained row by row and never waved through. The tests use tiny hand-built
Parquet tables.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import duckdb

from pipelines.common.fiscal_ref import FactInput
from pipelines.duckdb.explain_differences import (
    classify_differences,
    derived_signature,
    identity_collisions,
    label_offset_tie_ciks,
    reference_outputs_under_shuffles,
)

TABLES = ("facts_history", "facts", "facts_dimensional")
COLUMNS = (
    "fact_id BIGINT, fact_key BIGINT, cik INTEGER, tag VARCHAR, taxonomy VARCHAR, uom VARCHAR, "
    "qtrs INTEGER, period_end DATE, accession VARCHAR, fiscal_year INTEGER, "
    "fiscal_period VARCHAR, value DECIMAL(28,4), form VARCHAR, filed DATE, dq_flags VARCHAR[], "
    "derived_from BIGINT[], is_latest BOOLEAN, is_original BOOLEAN, n_versions INTEGER, "
    "is_restated BOOLEAN"
)


def counts(**kw: int) -> dict[str, int]:
    """The expected classification of one table: every cause listed, default 0."""
    causes = ("identity_collision", "identity_collision_knock_on", "label_offset_tie", "fy_tie",
              "fy_tie_knock_on", "unexplained")  # fmt: skip
    out = dict.fromkeys(causes, 0) | kw
    return {"total": sum(out.values()), **out}


def row(
    fact_id: int,
    *,
    qtrs: int = 4,
    period_end: str = "2023-09-30",
    accession: str = "A-23",
    fiscal_period: str = "FY",
    value: str = "10.0000",
    form: str = "10-K",
    filed: str = "2023-11-03",
    flags: tuple[str, ...] = (),
    parents: tuple[int, ...] | None = None,
    fiscal_year: int = 2023,
    n_versions: int = 1,
) -> tuple[Any, ...]:
    return (
        fact_id, fact_id, 5, "Revenues", "us-gaap", "USD", qtrs, period_end, accession,
        fiscal_year, fiscal_period, value, form, filed, list(flags),
        list(parents) if parents else None, True, True, n_versions, False,
    )  # fmt: skip


def write(directory: Path, tables: dict[str, list[tuple[Any, ...]]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for table in TABLES:
        con.execute(f"CREATE TABLE t ({COLUMNS})")
        for r in tables.get(table, []):
            con.execute(
                "INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, CAST(? AS DATE), ?, ?, ?, ?, ?, "
                "CAST(? AS DATE), ?, ?, ?, ?, ?, ?)",
                r,
            )
        con.execute(f"COPY t TO '{directory / (table + '.parquet')}' (FORMAT parquet)")
        con.execute("DROP TABLE t")
    return directory


DIRECT_Q3 = row(1, qtrs=1, period_end="2023-09-30", fiscal_period="Q3", value="12131.0000",
                form="10-Q", accession="TTM-10Q")  # fmt: skip
# a "Q4" derived from a trailing-twelve-month qtrs=4 value in the same 10-Q: same fact key and
# accession as the real quarter above, so the same fact_id would be given to both
DERIVED_Q4 = row(
    2, qtrs=1, period_end="2023-09-30", fiscal_period="Q4", value="12685.0000",
    form="10-Q", accession="TTM-10Q", flags=("q4_derived",), parents=(8, 9),
)  # fmt: skip


def test_identical_outputs_have_no_differences_to_explain(tmp_path: Path):
    tables = {"facts_history": [DIRECT_Q3, row(3)]}
    a, b = write(tmp_path / "a", tables), write(tmp_path / "b", tables)

    result = classify_differences(a, b)

    assert all(t == counts() for t in result.values())


def test_a_difference_on_a_key_that_two_history_rows_share_is_an_identity_collision(
    tmp_path: Path,
):
    a = write(tmp_path / "a", {"facts_history": [DIRECT_Q3, DERIVED_Q4]})
    changed = row(2, qtrs=1, fiscal_period="Q4", value="99.0000", form="10-Q",
                  accession="TTM-10Q", flags=("q4_derived",), parents=(8, 9))  # fmt: skip
    b = write(tmp_path / "b", {"facts_history": [DIRECT_Q3, changed]})

    result = classify_differences(a, b)["facts_history"]

    assert result == counts(identity_collision=2)


def test_a_difference_in_a_group_with_two_fy_rows_of_one_filing_is_a_fy_tie(tmp_path: Path):
    """A fiscal-year change: one 10-K carries FY rows for two period ends, same filing date."""
    fy_old = row(1, qtrs=4, period_end="2021-09-30", fiscal_year=2022)
    fy_new = row(2, qtrs=4, period_end="2022-06-30", fiscal_year=2022, value="16131.0000")
    nine_m = row(3, qtrs=3, period_end="2022-06-30", fiscal_period="9M", fiscal_year=2022,
                 value="15763.0000")  # fmt: skip
    q4_a = row(4, qtrs=1, period_end="2022-06-30", fiscal_period="Q4", fiscal_year=2022,
               value="368.0000", flags=("q4_derived",), parents=(2, 3))  # fmt: skip
    q4_b = row(5, qtrs=1, period_end="2021-09-30", fiscal_period="Q4", fiscal_year=2022,
               value="-14019.0000", flags=("q4_derived",), parents=(1, 3))  # fmt: skip
    base = [fy_old, fy_new, nine_m]
    a = write(tmp_path / "a", {"facts_history": [*base, q4_a]})
    b = write(tmp_path / "b", {"facts_history": [*base, q4_b]})

    result = classify_differences(a, b)["facts_history"]

    assert result == counts(fy_tie=2)


def test_a_difference_with_no_known_cause_is_reported_as_unexplained(tmp_path: Path):
    a = write(tmp_path / "a", {"facts_history": [row(1, value="10.0000")]})
    b = write(tmp_path / "b", {"facts_history": [row(1, value="10.0001")]})

    result = classify_differences(a, b)["facts_history"]

    assert result == counts(unexplained=2)


def test_identity_collisions_counts_fact_ids_shared_by_rows_and_derived_rows_by_form(
    tmp_path: Path,
):
    annual_q4 = row(7, qtrs=1, fiscal_period="Q4", value="3.0000", flags=("q4_derived",),
                    parents=(5, 6), accession="K-23")  # fmt: skip
    # DuckDB gives the two colliding rows the same fact_id; build the table that way
    collided = DERIVED_Q4[:0] + (1,) + DERIVED_Q4[1:]
    d = write(tmp_path / "d", {"facts_history": [DIRECT_Q3, collided, annual_q4]})

    result = identity_collisions(d)

    assert result["fact_ids_shared_by_several_rows"] == 1
    assert result["rows_with_a_shared_fact_id"] == 2
    assert result["derived_q4_rows"] == 2
    assert result["derived_q4_rows_by_form"] == {"10-K": 1, "10-Q": 1}


# --------------------------------------------------------------------------------------
# label-offset ties: a company with two offsets in equal numbers (rule 3's mode is a tie)
# --------------------------------------------------------------------------------------


def test_a_fiscal_year_that_differs_for_a_company_with_an_offset_tie_is_a_label_offset_tie(
    tmp_path: Path,
):
    """The reference picks a tied mode by input order, the SQL picks the smaller offset: every
    row of the company then has another fiscal_year on one side."""
    a = write(tmp_path / "a", {"facts_history": [row(1, fiscal_year=2024), row(2, qtrs=0)]})
    b = write(tmp_path / "b", {"facts_history": [row(1, fiscal_year=2023), row(2, qtrs=0)]})

    result = classify_differences(a, b, label_offset_tie_ciks={5})["facts_history"]

    assert result == counts(label_offset_tie=2)


def test_a_company_with_an_offset_tie_does_not_excuse_a_difference_in_another_column(
    tmp_path: Path,
):
    a = write(tmp_path / "a", {"facts_history": [row(1, value="10.0000")]})
    b = write(tmp_path / "b", {"facts_history": [row(1, value="10.0001")]})

    result = classify_differences(a, b, label_offset_tie_ciks={5})["facts_history"]

    assert result["unexplained"] == 2 and result["label_offset_tie"] == 0


def write_sub(
    directory: Path, quarter: str, rows: list[tuple[str, int, str, str, int, str]]
) -> None:
    """rows: (adsh, cik, form, fye, fy, period)."""
    folder = directory / quarter
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["adsh\tcik\tname\tsic\tfye\tform\tperiod\tfy\tfiled"]
    lines += [f"{a}\t{c}\tX\t1\t{fye}\t{form}\t{period}\t{fy}\t20230101"
              for a, c, form, fye, fy, period in rows]  # fmt: skip
    with gzip.open(folder / "sub.txt.gz", "wt") as fh:
        fh.write("\n".join(lines) + "\n")


def test_label_offset_tie_ciks_finds_companies_whose_10_k_offsets_tie(tmp_path: Path):
    write_sub(
        tmp_path,
        "2023q1",
        [
            # cik 1: offsets 0 and -1, one submission each: a tie
            ("A1", 1, "10-K", "1231", 2021, "20211231"),
            ("A2", 1, "10-K", "0331", 2022, "20230331"),
            # cik 2: offsets 0 and 0: not a tie
            ("B1", 2, "10-K", "1231", 2021, "20211231"),
            ("B2", 2, "10-K", "1231", 2022, "20221231"),
            # cik 3: would tie, but one of the two accessions has no facts: only one offset
            ("C1", 3, "10-K", "1231", 2021, "20211231"),
            ("C2", 3, "10-K", "0331", 2022, "20230331"),
            # cik 4: a 10-Q never counts
            ("D1", 4, "10-K", "1231", 2021, "20211231"),
            ("D2", 4, "10-Q", "0331", 2022, "20230331"),
        ],
    )
    accessions = {"A1", "A2", "B1", "B2", "C1", "D1", "D2"}  # C2 has no facts

    assert label_offset_tie_ciks(tmp_path, ["2023q1"], accessions) == {1}


def test_label_offset_tie_uses_the_latest_sub_row_of_an_accession_and_skips_blank_fye(
    tmp_path: Path,
):
    write_sub(tmp_path, "2022q4", [("A1", 1, "10-K", "1231", 2021, "20211231")])
    write_sub(
        tmp_path,
        "2023q1",
        [
            ("A1", 1, "10-K", "1231", 2021, "20211231"),  # same accession again: counts once
            ("A2", 1, "10-K", "0331", 2022, "20230331"),
            ("A3", 1, "10-K", "", 2022, "20230331"),  # blank fye: dropped like prepare_facts does
        ],
    )

    assert label_offset_tie_ciks(tmp_path, ["2022q4", "2023q1"], {"A1", "A2", "A3"}) == {1}


# --------------------------------------------------------------------------------------
# the classifier must not excuse a difference that is not the cause it names
# --------------------------------------------------------------------------------------


def test_a_difference_on_another_accession_of_a_colliding_fact_is_a_knock_on_not_a_collision(
    tmp_path: Path,
):
    other_a = row(3, qtrs=1, period_end="2023-09-30", fiscal_period="Q3", value="5.0000",
                  form="10-Q", accession="OTHER")  # fmt: skip
    other_b = row(3, qtrs=1, period_end="2023-09-30", fiscal_period="Q3", value="6.0000",
                  form="10-Q", accession="OTHER")  # fmt: skip
    a = write(tmp_path / "a", {"facts_history": [DIRECT_Q3, DERIVED_Q4, other_a]})
    b = write(tmp_path / "b", {"facts_history": [DIRECT_Q3, DERIVED_Q4, other_b]})

    result = classify_differences(a, b)["facts_history"]

    assert result["identity_collision"] == 0 and result["identity_collision_knock_on"] == 2
    assert result["unexplained"] == 0


def test_a_row_present_on_one_side_only_is_not_excused_by_a_label_offset_tie_unless_derived(
    tmp_path: Path,
):
    """A dropped or invented ordinary row at a tie company is a real difference."""
    a = write(tmp_path / "a", {"facts_history": [row(1), row(2, qtrs=0)]})
    b = write(tmp_path / "b", {"facts_history": [row(1)]})

    result = classify_differences(a, b, label_offset_tie_ciks={5})["facts_history"]

    assert result["label_offset_tie"] == 0 and result["unexplained"] == 1


def test_a_changed_value_inside_a_fy_tie_group_is_not_the_tie(tmp_path: Path):
    """The group holds a real tie, but the differing rows are a 9M value, not a derived Q4."""
    fy_old = row(1, qtrs=4, period_end="2021-09-30", fiscal_year=2022)
    fy_new = row(2, qtrs=4, period_end="2022-06-30", fiscal_year=2022, value="16131.0000")
    nine_a = row(3, qtrs=3, period_end="2022-06-30", fiscal_period="9M", fiscal_year=2022,
                 value="15763.0000")  # fmt: skip
    nine_b = row(3, qtrs=3, period_end="2022-06-30", fiscal_period="9M", fiscal_year=2022,
                 value="15764.0000")  # fmt: skip
    a = write(tmp_path / "a", {"facts_history": [fy_old, fy_new, nine_a]})
    b = write(tmp_path / "b", {"facts_history": [fy_old, fy_new, nine_b]})

    result = classify_differences(a, b)["facts_history"]

    assert result["fy_tie"] == 0 and result["unexplained"] == 2


def test_unexplained_rows_can_be_sampled_for_inspection(tmp_path: Path):
    a = write(tmp_path / "a", {"facts_history": [row(1, value="10.0000")]})
    b = write(tmp_path / "b", {"facts_history": [row(1, value="10.0001")]})
    samples: dict[str, list[dict[str, Any]]] = {}

    classify_differences(a, b, samples=samples, sample_size=5)

    rows = samples["facts_history"]
    assert {r["side"] for r in rows} == {"a", "b"}
    assert {str(r["value"]) for r in rows} == {"10.0000", "10.0001"}
    assert samples["facts"] == [] and samples["facts_dimensional"] == []


def knock_on_tables(tmp_path: Path, real_a: tuple[Any, ...], real_b: tuple[Any, ...]):
    """A fiscal-year tie (two FY rows of one filing) makes the engines derive different Q4 rows;
    engine b's derived row lands on the key of a real quarter row from another accession."""
    fy_old = row(1, qtrs=4, period_end="2021-09-30", fiscal_year=2022)
    fy_new = row(2, qtrs=4, period_end="2022-06-30", fiscal_year=2022, value="16131.0000")
    nine_m = row(3, qtrs=3, period_end="2022-06-30", fiscal_period="9M", fiscal_year=2022,
                 value="15763.0000")  # fmt: skip
    q4_a = row(4, qtrs=1, period_end="2022-06-30", fiscal_period="Q4", fiscal_year=2022,
               value="368.0000", flags=("q4_derived",), parents=(2, 3))  # fmt: skip
    q4_b = row(5, qtrs=1, period_end="2021-09-30", fiscal_period="Q4", fiscal_year=2022,
               value="-14019.0000", flags=("q4_derived",), parents=(1, 3))  # fmt: skip
    base = [fy_old, fy_new, nine_m]
    a = write(tmp_path / "a", {"facts_history": [*base, q4_a, real_a], "facts": [real_a]})
    b = write(tmp_path / "b", {"facts_history": [*base, q4_b, real_b], "facts": [real_b]})
    return a, b


def real_q1(**kw: Any) -> tuple[Any, ...]:
    return row(6, qtrs=1, period_end="2021-09-30", fiscal_period="Q1", fiscal_year=2022,
               value="9.0000", form="10-Q", accession="Q-22", **kw)  # fmt: skip


def test_a_real_row_whose_version_flags_differ_at_a_tie_dependent_derived_fact_is_a_knock_on(
    tmp_path: Path,
):
    """Same row on both sides except its version count: the derived row that appeared at its fact
    in one engine changed how many versions the fact has."""
    a, b = knock_on_tables(tmp_path, real_q1(n_versions=1), real_q1(n_versions=2))

    result = classify_differences(a, b)["facts"]

    assert result == counts(fy_tie_knock_on=2)


def test_a_changed_value_at_such_a_fact_is_not_a_knock_on(tmp_path: Path):
    """The version flags may differ because of the tie; the value may not."""
    changed = row(6, qtrs=1, period_end="2021-09-30", fiscal_period="Q1", fiscal_year=2022,
                  value="9999.0000", form="10-Q", accession="Q-22", n_versions=2)  # fmt: skip
    a, b = knock_on_tables(tmp_path, real_q1(n_versions=1), changed)

    result = classify_differences(a, b)["facts"]

    assert result == counts(unexplained=2)


def test_a_difference_at_a_fact_with_no_derived_row_is_never_a_fy_tie_knock_on(tmp_path: Path):
    fy_old = row(1, qtrs=4, period_end="2021-09-30", fiscal_year=2022)
    fy_new = row(2, qtrs=4, period_end="2022-06-30", fiscal_year=2022, value="16131.0000")
    a = write(tmp_path / "a", {"facts_history": [fy_old, fy_new, real_q1()], "facts": [real_q1()]})
    b = write(
        tmp_path / "b",
        {
            "facts_history": [fy_old, fy_new, real_q1(n_versions=2)],
            "facts": [real_q1(n_versions=2)],
        },
    )

    result = classify_differences(a, b)["facts"]

    assert result == counts(unexplained=2)


def test_a_tie_company_row_whose_value_also_differs_is_not_a_label_offset_tie(tmp_path: Path):
    a = write(tmp_path / "a", {"facts_history": [row(1, fiscal_year=2024, value="10.0000")]})
    b = write(tmp_path / "b", {"facts_history": [row(1, fiscal_year=2023, value="10.0001")]})

    result = classify_differences(a, b, label_offset_tie_ciks={5})["facts_history"]

    assert result["label_offset_tie"] == 0 and result["unexplained"] == 2


def test_a_derived_row_on_one_side_only_is_not_excused_by_a_label_offset_tie(tmp_path: Path):
    derived = row(2, qtrs=1, fiscal_period="Q4", flags=("q4_derived",), parents=(1, 3))
    a = write(tmp_path / "a", {"facts_history": [row(1), derived]})
    b = write(tmp_path / "b", {"facts_history": [row(1)]})

    result = classify_differences(a, b, label_offset_tie_ciks={5})["facts_history"]

    assert result["label_offset_tie"] == 0 and result["unexplained"] == 1


# --------------------------------------------------------------------------------------
# does the reference's own output depend on the order of its input?
# --------------------------------------------------------------------------------------


def fact(
    qtrs: int, period_end: str, value: str, adsh: str, filed: str, sub_period: str
) -> FactInput:
    from datetime import date as d
    from decimal import Decimal

    return FactInput(
        cik=5, adsh=adsh, tag="Revenues", taxonomy="us-gaap", uom="USD", qtrs=qtrs,
        period_end=d.fromisoformat(period_end), value=Decimal(value), form="10-K",
        filed=d.fromisoformat(filed), fye_mmdd="1231", sub_fy=2022,
        sub_period=d.fromisoformat(sub_period),
    )  # fmt: skip


def is_revenues(f: FactInput) -> bool:
    return f.tag == "Revenues"


def test_two_fy_rows_of_one_filing_make_the_reference_output_depend_on_input_order():
    """Two FY rows, one accession, one filing date, different period ends, neither native: the
    reference keeps whichever comes first, so the derived Q4 is 100 - 50 or 200 - 50."""
    inputs = [
        fact(4, "2022-06-30", "100.0000", "K-22", "2023-03-01", "2023-03-31"),
        fact(4, "2022-12-31", "200.0000", "K-22", "2023-03-01", "2023-03-31"),
        fact(3, "2022-09-30", "50.0000", "Q-22", "2022-11-09", "2022-09-30"),
    ]

    outputs = reference_outputs_under_shuffles(inputs, is_revenues, n=40, seed=1)

    values = {sig[0][2] for sig in outputs}
    assert len(outputs) == 2 and values == {"50.0000", "150.0000"}


def test_without_a_tie_the_reference_output_is_the_same_whatever_the_order():
    inputs = [
        fact(4, "2022-06-30", "100.0000", "K-22", "2023-03-01", "2023-03-31"),
        fact(4, "2022-12-31", "200.0000", "K-23", "2023-04-01", "2023-03-31"),  # a later filing
        fact(3, "2022-09-30", "50.0000", "Q-22", "2022-11-09", "2022-09-30"),
    ]

    outputs = reference_outputs_under_shuffles(inputs, is_revenues, n=40, seed=1)

    assert len(outputs) == 1


def test_derived_signature_lists_the_derived_rows_of_one_series(tmp_path: Path):
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (cik INTEGER, tag VARCHAR, segments VARCHAR, period_end DATE, "
        "accession VARCHAR, value DECIMAL(28,4), dq_flags VARCHAR[])"
    )
    con.execute(
        "INSERT INTO t VALUES (5, 'Revenues', 'S=A;', DATE '2022-12-31', 'K-22', 150, "
        "['q4_derived'])"
    )
    con.execute("INSERT INTO t VALUES (5, 'Revenues', 'S=A;', DATE '2022-12-31', 'K-22', 200, [])")
    con.execute(
        "INSERT INTO t VALUES (5, 'Revenues', 'S=B;', DATE '2022-12-31', 'K-22', 7, ['q4_derived'])"
    )
    (tmp_path / "d").mkdir()
    con.execute(f"COPY t TO '{tmp_path / 'd' / 'facts_dimensional.parquet'}' (FORMAT parquet)")

    assert derived_signature(tmp_path / "d", 5, "Revenues", "S=A;") == (
        ("2022-12-31", "K-22", "150.0000"),
    )
