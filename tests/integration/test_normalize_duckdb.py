"""Spark == DuckDB end to end (M01 section 3.7): the differential test of the two normalizers.

A small hand-built FSDS fixture (two quarters of gzip TSV, RFC-4180 quoting) is loaded into Spark's
raw tables with `fsds_to_iceberg.load_quarter` and normalized with `normalize_facts.run`; the SAME
directories are normalized by `pipelines.duckdb.normalize_facts_duckdb.run` into Parquet. Then:

* the two engines hold the same rows in `facts_history`, `facts` and `facts_dimensional`
  (every contract column except `fact_key` / `fact_id`, whose hash values differ between
  `xxhash64` and DuckDB's `hash`; `derived_from` is compared through the parents' natural keys,
  resolved through each engine's own `fact_id`);
* hand-derived expectations hold on EACH engine separately, so two engines that agree on a wrong
  answer still fail: exact row counts per table and per (company, tag), derived Q4 values, the
  restated FY, the same fact repeated with the same value, the dropped rows, the segments;
* the DuckDB outputs have exactly the contract's column names, order and types, are idempotent,
  and do not depend on the order of the quarters, threads or the memory limit.

Fixture companies (all values in USD unless stated; accession numbers are made up):

  Alpha (cik 1001, FYE 0930). 10-Q Q3 (2023q3, period 2023-06-30) and 10-K (2023q4, period
    2023-09-30). Revenues FY 1000 / 9M 700 -> derived Q4 300; a direct Q3 quarter (qtrs 1); EPS
    (USD/shares) FY and 9M -> never derived; instants; a custom tag; NetIncomeLoss under taxonomy
    years 2022 (90) and 2023 (100) in one filing -> one row (100); a dei instant; a footnote with a
    quote and a tab; two segment series (one whose name has a quote and a tab) with FY and 9M ->
    dimensional rows and their own Q4; a co-registrant row (not curated); a NULL value and an
    unparseable ddate (both dropped).
  Beta (cik 1002, FYE 0630). One 10-K present in BOTH quarters: 2023q3 (sub.fy 2023, Revenues 500)
    and 2023q4 (sub.fy 2024, Revenues 520). The latest quarter wins for the value AND for the
    sub row, so the fiscal year is 2024 (label offset +1).
  Gamma (cik 1003, FYE 0930). 10-Q 9M (2023q3), 10-K (2023q4) and a later 10-K/A (2023q4) that
    restates FY revenue 1100 -> 1080 and repeats NetIncomeLoss 50 unchanged.
  Delta (cik 1004). An S-1 with an empty fye: its facts are dropped.
  Plus a num row whose adsh has no sub row at all: dropped.
"""

from __future__ import annotations

import gzip
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pyspark.sql import SparkSession
from pyspark.sql.types import ArrayType, DataType, StructType

from pipelines.duckdb.normalize_facts_duckdb import run as duckdb_run
from pipelines.spark.curated_schemas import FACTS, FACTS_DIMENSIONAL, FACTS_HISTORY
from pipelines.spark.fsds_to_iceberg import load_quarter
from pipelines.spark.normalize_facts import run as spark_run
from pipelines.spark.raw_schemas import NUM_SOURCE, SOURCE_SCHEMAS, SUB_SOURCE

QUARTERS = ["2023q3", "2023q4"]
TABLES = ("facts_history", "facts", "facts_dimensional")
SCHEMAS: dict[str, StructType] = {
    "facts_history": FACTS_HISTORY,
    "facts": FACTS,
    "facts_dimensional": FACTS_DIMENSIONAL,
}
HASH_COLUMNS = ("fact_key", "fact_id")

# --- accessions ---------------------------------------------------------------------------
A_Q = "0000001001-23-000011"  # Alpha 10-Q, period 2023-06-30
A_K = "0000001001-23-000015"  # Alpha 10-K, period 2023-09-30
B_K = "0000001002-23-000007"  # Beta 10-K, period 2023-06-30 (present in both quarters)
C_Q = "0000001003-23-000021"  # Gamma 10-Q, period 2023-06-30
C_K = "0000001003-23-000030"  # Gamma 10-K, period 2023-09-30
C_KA = "0000001003-23-000035"  # Gamma 10-K/A, period 2023-09-30, filed later
D_S1 = "0000001004-23-000002"  # Delta S-1: no fye
ORPHAN = "0000009998-23-000001"  # a num row with no sub row

WIDGETS = "ProductOrServiceAxis=WidgetsMember"
QUOTED_SEG = 'LegalEntityAxis=Vacation ownership interest ("VOI")\tinventory'  # quote AND tab

# --- writing FSDS files -------------------------------------------------------------------


def field(value: Any) -> str:
    """One TSV field, RFC-4180 style: quote when it holds a tab, quote or newline; double quotes."""
    text = "" if value is None else str(value)
    if any(c in text for c in '\t"\r\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


def write_gz(path: Path, header: list[str], rows: list[dict[str, Any]]) -> None:
    lines = ["\t".join(header)] + ["\t".join(field(r.get(c)) for c in header) for r in rows]
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + "\n")


def sub(adsh: str, cik: int, name: str, fye: str, form: str, period: int, fy: int, fp: str,
        filed: int) -> dict[str, Any]:  # fmt: skip
    return {
        "adsh": adsh, "cik": cik, "name": name, "sic": 3571, "countryba": "US", "fye": fye,
        "form": form, "period": period, "fy": fy, "fp": fp, "filed": filed,
        "accepted": f"{str(filed)[:4]}-{str(filed)[4:6]}-{str(filed)[6:]} 16:30:00.0",
        "prevrpt": 0, "afs": "1-LAF", "wksi": 0, "detail": 1, "instance": f"{adsh}.xml",
    }  # fmt: skip


def num(adsh: str, tag: str, ddate: int, qtrs: int, value: str | None, *, uom: str = "USD",
        version: str = "us-gaap/2023", segments: str | None = None, coreg: str | None = None,
        footnote: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "adsh": adsh, "tag": tag, "version": version, "ddate": ddate, "qtrs": qtrs, "uom": uom,
        "segments": segments, "coreg": coreg, "value": value, "footnote": footnote,
    }  # fmt: skip


def write_quarter(
    root: Path, quarter: str, subs: list[dict[str, Any]], nums: list[dict[str, Any]]
) -> None:
    qdir = root / quarter
    qdir.mkdir(parents=True, exist_ok=True)
    write_gz(qdir / "sub.txt.gz", SUB_SOURCE.fieldNames(), subs)
    write_gz(qdir / "num.txt.gz", NUM_SOURCE.fieldNames(), nums)
    for name in ("tag", "pre"):  # not used by the normalizers; Spark's loader wants all four
        write_gz(qdir / f"{name}.txt.gz", SOURCE_SCHEMAS[name].fieldNames(), [])


def build_fixture(root: Path) -> Path:
    alpha = 'Alpha "Quoted"\tCorp.'  # a quoted name with a tab and quotes (not curated)
    q3_subs = [
        sub(A_Q, 1001, alpha, "0930", "10-Q", 20230630, 2023, "Q3", 20230804),
        sub(B_K, 1002, "Beta Inc.", "0630", "10-K", 20230630, 2023, "FY", 20230810),
        sub(C_Q, 1003, "Gamma Co.", "0930", "10-Q", 20230630, 2023, "Q3", 20230803),
        sub(D_S1, 1004, "Delta Ltd.", "", "S-1", 20230630, 2023, "FY", 20230901),
    ]
    q3_nums = [
        # Alpha 10-Q
        num(A_Q, "Revenues", 20230630, 3, "700.0000"),
        num(A_Q, "Revenues", 20230630, 1, "250.0000"),
        num(A_Q, "EarningsPerShareDiluted", 20230630, 3, "3.5000", uom="USD/shares"),
        num(A_Q, "Assets", 20230630, 0, "4800.0000"),
        num(A_Q, "Revenues", 20230630, 3, "400.0000", segments=WIDGETS),
        num(A_Q, "Revenues", 20230630, 3, "60.0000", segments=QUOTED_SEG),
        # Beta 10-K as first loaded (2023q3); the 2023q4 copy replaces Revenues
        num(B_K, "Revenues", 20230630, 4, "500.0000"),
        num(B_K, "NetIncomeLoss", 20230630, 4, "60.0000"),
        num(B_K, "Assets", 20230630, 0, "900.0000"),
        # Gamma 10-Q: 9M
        num(C_Q, "Revenues", 20230630, 3, "800.0000"),
        num(C_Q, "OperatingIncomeLoss", 20230630, 3, "80.0000"),
        # Delta S-1 (no fye) and an orphan: both dropped
        num(D_S1, "Revenues", 20230630, 4, "9999.0000"),
        num(ORPHAN, "Revenues", 20230630, 4, "8888.0000"),
    ]
    q4_subs = [
        sub(A_K, 1001, alpha, "0930", "10-K", 20230930, 2023, "FY", 20231103),
        sub(B_K, 1002, "Beta Inc.", "0630", "10-K", 20230630, 2024, "FY", 20230810),
        sub(C_K, 1003, "Gamma Co.", "0930", "10-K", 20230930, 2023, "FY", 20231102),
        sub(C_KA, 1003, "Gamma Co.", "0930", "10-K/A", 20230930, 2023, "FY", 20231212),
    ]
    q4_nums = [
        # Alpha 10-K
        num(
            A_K,
            "Revenues",
            20230930,
            4,
            "1000.0000",
            footnote='Includes "special" items\tand a tab',
        ),
        num(A_K, "EarningsPerShareDiluted", 20230930, 4, "5.0000", uom="USD/shares"),
        num(A_K, "Assets", 20230930, 0, "5200.0000"),
        num(A_K, "CustomMetric", 20230930, 4, "7.0000", version=A_K),
        num(A_K, "NetIncomeLoss", 20230930, 4, "90.0000", version="us-gaap/2022"),
        num(A_K, "NetIncomeLoss", 20230930, 4, "100.0000", version="us-gaap/2023"),
        num(
            A_K,
            "EntityCommonStockSharesOutstanding",
            20231015,
            0,
            "15000000.0000",
            uom="shares",
            version="dei/2023",
        ),
        num(A_K, "Revenues", 20230930, 4, "600.0000", segments=WIDGETS),
        num(A_K, "Revenues", 20230930, 4, "90.0000", segments=QUOTED_SEG),
        num(A_K, "Revenues", 20230930, 4, "1.0000", coreg="0000001099"),
        num(A_K, "NullValueTag", 20230930, 4, None),
        num(A_K, "BadDateTag", 20231399, 4, "5.0000"),
        # Beta: the same filing again in the later quarter, with a changed Revenues
        num(B_K, "Revenues", 20230630, 4, "520.0000"),
        # Gamma 10-K and 10-K/A (restated revenue; net income repeated unchanged)
        num(C_K, "Revenues", 20230930, 4, "1100.0000"),
        num(C_K, "NetIncomeLoss", 20230930, 4, "50.0000"),
        num(C_K, "OperatingIncomeLoss", 20230930, 4, "120.0000"),
        num(C_KA, "Revenues", 20230930, 4, "1080.0000"),
        num(C_KA, "NetIncomeLoss", 20230930, 4, "50.0000"),
    ]
    write_quarter(root, "2023q3", q3_subs, q3_nums)
    write_quarter(root, "2023q4", q4_subs, q4_nums)
    return root


def build_minimal_fixture(root: Path) -> Path:
    """One company, one 10-K, one consolidated fact: no segments and no derived Q4 at all."""
    write_quarter(
        root,
        "2023q4",
        [sub(A_K, 1001, "Alpha", "0930", "10-K", 20230930, 2023, "FY", 20231103)],
        [num(A_K, "Revenues", 20230930, 4, "1000.0000")],
    )
    return root


# --- running the two engines --------------------------------------------------------------

Row = dict[str, Any]


@dataclass
class EngineResult:
    name: str
    counts: dict[str, int]
    tables: dict[str, list[Row]]


def read_parquet(path: Path) -> tuple[list[tuple[str, str]], list[Row]]:
    """(column name, DuckDB type) pairs in file order, and the rows as dicts."""
    con = duckdb.connect(":memory:")
    try:
        described = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
        cur = con.execute("SELECT * FROM read_parquet(?)", [str(path)])
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
    finally:
        con.close()
    return [(d[0], d[1]) for d in described], rows


def duckdb_result(src: Path, out_dir: Path, **kwargs: Any) -> EngineResult:
    counts = duckdb_run(src, QUARTERS, out_dir, **kwargs)
    tables = {t: read_parquet(out_dir / f"{t}.parquet")[1] for t in TABLES}
    return EngineResult("duckdb", counts, tables)


@pytest.fixture(scope="module")
def fsds_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_fixture(tmp_path_factory.mktemp("fsds"))


@pytest.fixture(scope="module")
def spark_result(spark: SparkSession, fsds_dir: Path):  # type: ignore[no-untyped-def]
    """Load the fixture into Spark raw tables and run the Spark normalizer, once per module.

    Uses its own throwaway namespaces (module-scoped, like the data) instead of the
    function-scoped `lake_namespace` fixture, so the Spark job runs once, not once per test.
    """
    base = "lake.t_norm_duckdb"
    raw_ns, cur_ns = f"{base}_raw", f"{base}_cur"

    def drop() -> None:
        for ns in (raw_ns, cur_ns):
            if spark.catalog.databaseExists(ns):
                for r in spark.sql(f"SHOW TABLES IN {ns}").collect():
                    spark.sql(f"DROP TABLE IF EXISTS {ns}.{r.tableName} PURGE")
                spark.sql(f"DROP NAMESPACE IF EXISTS {ns}")

    drop()
    try:
        for q in QUARTERS:
            load_quarter(spark, fsds_dir, q, raw_ns)
        counts = spark_run(spark, raw_ns, cur_ns)
        tables = {t: [r.asDict() for r in spark.table(f"{cur_ns}.{t}").collect()] for t in TABLES}
        yield EngineResult("spark", counts, tables)
    finally:
        drop()


@pytest.fixture(scope="module")
def duck_result(fsds_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> EngineResult:
    return duckdb_result(fsds_dir, tmp_path_factory.mktemp("duck_out"))


@pytest.fixture(params=["spark", "duckdb"])
def engine(request: pytest.FixtureRequest) -> EngineResult:
    """Each hand-derived expectation is checked against each engine on its own."""
    return request.getfixturevalue("spark_result" if request.param == "spark" else "duck_result")


# --- comparing rows -----------------------------------------------------------------------


def natural_key(r: Row, with_segments: bool) -> tuple[Any, ...]:
    key = (r["cik"], r["tag"], r["taxonomy"], r["uom"], r["qtrs"], r["period_end"], r["accession"])
    return (*key, r["segments"]) if with_segments else key


def canonical(result: EngineResult) -> dict[str, Counter[tuple[Any, ...]]]:
    """Per table, a multiset of rows without fact_key/fact_id, with `derived_from` replaced by the
    parents' natural keys, resolved through the engine's own facts_history. (facts_dimensional
    has no derived_from column in the contract; its derived Q4s are compared by value and flags.)
    """
    history_ids = {r["fact_id"]: natural_key(r, False) for r in result.tables["facts_history"]}
    out: dict[str, Counter[tuple[Any, ...]]] = {}
    for table, rows in result.tables.items():
        by_id = history_ids
        counter: Counter[tuple[Any, ...]] = Counter()
        for r in rows:
            parents = None
            if r.get("derived_from") is not None:  # facts_dimensional has no derived_from column
                missing = [i for i in r["derived_from"] if i not in by_id]
                assert not missing, f"{result.name}.{table}: derived_from ids {missing} not found"
                parents = tuple(by_id[i] for i in r["derived_from"])
            kept = {k: v for k, v in r.items() if k not in HASH_COLUMNS}
            if "derived_from" in r:
                kept["derived_from"] = parents
            kept["dq_flags"] = None if r["dq_flags"] is None else tuple(sorted(r["dq_flags"]))
            counter[tuple(sorted(kept.items(), key=lambda kv: kv[0]))] += 1
        out[table] = counter
    return out


def pick(result: EngineResult, table: str, **where: Any) -> list[Row]:
    return [r for r in result.tables[table] if all(r[k] == v for k, v in where.items())]


def one(result: EngineResult, table: str, **where: Any) -> Row:
    rows = pick(result, table, **where)
    assert len(rows) == 1, f"{result.name}.{table} {where}: expected one row, got {len(rows)}"
    return rows[0]


def parents_of(result: EngineResult, row: Row) -> list[Row]:
    """The history rows a derived Q4 points at, in derived_from order."""
    by_id = {r["fact_id"]: r for r in result.tables["facts_history"]}
    return [by_id[i] for i in row["derived_from"]]


# --- the fixture itself -------------------------------------------------------------------


def test_fixture_files_are_what_the_tests_below_assume(fsds_dir: Path) -> None:
    """Guard: both quarters have all four gzip files, the quoted segment really is written as an
    RFC-4180 quoted field with doubled quotes and an embedded tab, and Beta's accession is in the
    sub and num files of both quarters (so the 'latest quarter wins' cases are not vacuous).
    """
    for q in QUARTERS:
        for name in ("sub", "num", "tag", "pre"):
            assert (fsds_dir / q / f"{name}.txt.gz").is_file()
    with gzip.open(fsds_dir / "2023q4" / "num.txt.gz", "rt", encoding="utf-8", newline="") as fh:
        text = fh.read()
    assert '"LegalEntityAxis=Vacation ownership interest (""VOI"")\tinventory"' in text
    for q in QUARTERS:
        with gzip.open(fsds_dir / q / "sub.txt.gz", "rt", encoding="utf-8") as fh:
            assert B_K in fh.read()
        with gzip.open(fsds_dir / q / "num.txt.gz", "rt", encoding="utf-8") as fh:
            assert B_K in fh.read()


# --- the two engines agree ----------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_spark_and_duckdb_hold_the_same_rows(
    spark_result: EngineResult, duck_result: EngineResult, table: str
) -> None:
    """The differential test of M01 section 3.7: for each curated table the two engines hold
    the same multiset of rows over every contract column except the hash columns, with
    derived_from compared through the parents' natural keys (cik, tag, taxonomy, uom, qtrs,
    period_end, accession[, segments]). Reports a few rows from each side that differ.
    """
    spark_rows = canonical(spark_result)[table]
    duck_rows = canonical(duck_result)[table]
    only_spark = list((spark_rows - duck_rows).elements())
    only_duck = list((duck_rows - spark_rows).elements())
    assert not only_spark and not only_duck, (
        f"{table}: {len(only_spark)} rows only in Spark, {len(only_duck)} only in DuckDB.\n"
        f"Spark only (first 3): {only_spark[:3]}\nDuckDB only (first 3): {only_duck[:3]}"
    )


def test_returned_counts_equal_the_spark_counts(
    spark_result: EngineResult, duck_result: EngineResult
) -> None:
    """`run` returns the rows written per table, and they are the Spark job's counts."""
    assert duck_result.counts == spark_result.counts


# --- hand-derived expectations, per engine -------------------------------------------------


def test_row_counts_per_table_are_the_hand_derived_ones(engine: EngineResult) -> None:
    """Counts worked out from the fixture, not read from either engine:
    facts_history = Alpha 11 (10-Q: 4 consolidated facts; 10-K: Revenues, EPS, Assets, custom,
    NetIncomeLoss once, dei = 6; plus one derived Q4) + Beta 3 + Gamma 10 (Revenues 9M, 2 FY, 2
    derived Q4; NetIncomeLoss 2; OperatingIncomeLoss 9M, FY, Q4) = 24.
    facts = 11 + 3 + 9 (Gamma NetIncomeLoss repeated with the same value is ONE row; the restated
    FY and the two derived Q4s are two rows each) = 23.
    facts_dimensional = two series x (FY, 9M, derived Q4) = 6.
    """
    assert engine.counts == {"facts_history": 24, "facts": 23, "facts_dimensional": 6}
    assert {t: len(rows) for t, rows in engine.tables.items()} == engine.counts


def test_rows_per_company_and_tag_are_the_hand_derived_ones(engine: EngineResult) -> None:
    """Per (cik, tag) row counts, in history and in facts. Alpha Revenues: FY, 9M, a direct Q3
    quarter, and one derived Q4. EPS never derives (2 rows); Assets has two instants; the 2022 and
    2023 NetIncomeLoss collapse to one. Gamma Revenues in history: 9M, FY x 2, Q4 x 2; in facts the
    same 5 (the restated FY and the two Q4 tiers keep both rows).
    """
    history = Counter((r["cik"], r["tag"]) for r in engine.tables["facts_history"])
    assert history == {
        (1001, "Revenues"): 4,
        (1001, "EarningsPerShareDiluted"): 2,
        (1001, "Assets"): 2,
        (1001, "CustomMetric"): 1,
        (1001, "NetIncomeLoss"): 1,
        (1001, "EntityCommonStockSharesOutstanding"): 1,
        (1002, "Revenues"): 1,
        (1002, "NetIncomeLoss"): 1,
        (1002, "Assets"): 1,
        (1003, "Revenues"): 5,
        (1003, "NetIncomeLoss"): 2,
        (1003, "OperatingIncomeLoss"): 3,
    }
    facts = Counter((r["cik"], r["tag"]) for r in engine.tables["facts"])
    assert facts == {**history, (1003, "NetIncomeLoss"): 1}


def test_rows_that_must_not_be_curated_are_absent(engine: EngineResult) -> None:
    """Dropped: the NULL value, the unparseable ddate (20231399), the S-1 without fye, the num row
    without a sub row, and the co-registrant row (value 1.0000, which would otherwise be a second
    consolidated Alpha FY revenue). The co-registrant is in no table, not even dimensional.
    """
    for table, rows in engine.tables.items():
        assert not [r for r in rows if r["tag"] in ("NullValueTag", "BadDateTag")], table
        assert not [r for r in rows if r["cik"] == 1004], table
        assert not [r for r in rows if r["value"] in (Decimal("9999"), Decimal("8888"))], table
        assert not [r for r in rows if r["value"] == Decimal("1") and r["cik"] == 1001], table
    fy = one(engine, "facts", cik=1001, tag="Revenues", qtrs=4)
    assert fy["value"] == Decimal("1000.0000")


def test_alpha_derived_q4_is_fy_minus_9m_with_the_fy_filings_provenance(
    engine: EngineResult,
) -> None:
    """Rule 5: Alpha Revenues Q4 = 1000 (10-K FY) - 700 (10-Q 9M) = 300, qtrs 1, period_end = the
    FY end, fiscal 2023 / Q4, form, accession and filed of the 10-K, flagged q4_derived, with
    derived_from = [FY fact, 9M fact] resolving to the 10-K FY row and the 10-Q 9M row.
    """
    q4 = one(engine, "facts", cik=1001, tag="Revenues", qtrs=1, fiscal_period="Q4")
    assert q4["value"] == Decimal("300.0000")
    assert (q4["period_end"], q4["period_start"]) == (date(2023, 9, 30), date(2023, 7, 1))
    assert (q4["fiscal_year"], q4["accession"], q4["form"]) == (2023, A_K, "10-K")
    assert q4["filed"] == date(2023, 11, 3)
    assert "q4_derived" in q4["dq_flags"]
    assert (q4["is_latest"], q4["is_original"], q4["n_versions"], q4["is_restated"]) == (
        True,
        True,
        1,
        False,
    )
    fy, nine = parents_of(engine, q4)
    assert (fy["qtrs"], fy["accession"], fy["value"], fy["period_end"]) == (
        4,
        A_K,
        Decimal("1000.0000"),
        date(2023, 9, 30),
    )
    assert (nine["qtrs"], nine["accession"], nine["value"], nine["period_end"]) == (
        3,
        A_Q,
        Decimal("700.0000"),
        date(2023, 6, 30),
    )


def test_alpha_nothing_else_is_derived(engine: EngineResult) -> None:
    """Per-share facts (EPS FY and 9M), instants and Alpha's unpaired NetIncomeLoss never get a
    Q4; the only derived rows in the consolidated tables are the one Alpha Revenues Q4, Gamma's
    two Revenues Q4 and Gamma's one OperatingIncomeLoss Q4.
    """
    derived = [r for r in engine.tables["facts_history"] if r["derived_from"] is not None]
    assert Counter((r["cik"], r["tag"]) for r in derived) == {
        (1001, "Revenues"): 1,
        (1003, "Revenues"): 2,
        (1003, "OperatingIncomeLoss"): 1,
    }
    assert all("q4_derived" in r["dq_flags"] for r in derived)
    assert not [r for r in engine.tables["facts_history"] if "q4_derived" in r["dq_flags"]
                and r["derived_from"] is None]  # fmt: skip


def test_taxonomy_custom_flag_and_year_collapse(engine: EngineResult) -> None:
    """taxonomy is the year-stripped family, 'custom' when version == adsh, and a tag under two
    taxonomy years in one filing is ONE row carrying the latest year's value (100, not 90).
    """
    custom = one(engine, "facts", cik=1001, tag="CustomMetric")
    assert (custom["taxonomy"], custom["is_custom"]) == ("custom", True)
    nil = one(engine, "facts", cik=1001, tag="NetIncomeLoss")
    assert (nil["taxonomy"], nil["is_custom"], nil["value"]) == ("us-gaap", False, Decimal(100))
    dei = one(engine, "facts", cik=1001, tag="EntityCommonStockSharesOutstanding")
    assert (dei["taxonomy"], dei["uom"], dei["qtrs"]) == ("dei", "shares", 0)
    assert dei["period_start"] is None
    assert {r["taxonomy"] for r in engine.tables["facts"]} == {"us-gaap", "custom", "dei"}


def test_alpha_instants_and_fiscal_labels(engine: EngineResult) -> None:
    """Instants have no period_start, qtrs 0, and are labelled by their place in the fiscal year:
    the 2023-09-30 balance sheet is FY 2023, the 2023-06-30 one is Q3 2023. The FY revenue starts
    2022-10-01 (period_end minus four quarters plus a day).
    """
    year_end = one(engine, "facts", cik=1001, tag="Assets", period_end=date(2023, 9, 30))
    mid = one(engine, "facts", cik=1001, tag="Assets", period_end=date(2023, 6, 30))
    assert (year_end["qtrs"], year_end["period_start"], year_end["fiscal_period"]) == (
        0,
        None,
        "FY",
    )
    assert (year_end["fiscal_year"], mid["fiscal_period"], mid["fiscal_year"]) == (2023, "Q3", 2023)
    fy = one(engine, "facts", cik=1001, tag="Revenues", qtrs=4)
    assert (fy["period_start"], fy["fiscal_period"], fy["fiscal_year"]) == (
        date(2022, 10, 1),
        "FY",
        2023,
    )


def test_beta_the_latest_quarter_wins_for_the_value_and_for_the_sub_row(
    engine: EngineResult,
) -> None:
    """Beta's 10-K is in both quarters. The later quarter's num row (520, not 500) and its sub
    row (sub.fy 2024, not 2023) are used: one Revenues row, and its fiscal year is the June 2023
    year end (2023) plus label offset 2024 - 2023 = +1, i.e. 2024. All three Beta rows agree.
    """
    revenues = [r for r in engine.tables["facts_history"] if r["cik"] == 1002]
    assert len(revenues) == 3
    assert one(engine, "facts", cik=1002, tag="Revenues")["value"] == Decimal("520.0000")
    assert {r["fiscal_year"] for r in revenues} == {2024}
    assert one(engine, "facts", cik=1002, tag="NetIncomeLoss")["value"] == Decimal("60.0000")
    assets = one(engine, "facts", cik=1002, tag="Assets")
    assert (assets["fiscal_period"], assets["period_end"]) == ("FY", date(2023, 6, 30))


def test_gamma_restated_fy_keeps_both_versions_and_both_q4_tiers(engine: EngineResult) -> None:
    """Gamma restates FY revenue 1100 -> 1080 in a later 10-K/A. facts has the original (10-K) and
    the latest (10-K/A) as two rows with n_versions 2 and is_restated; each tier has its own
    derived Q4 (1100 - 800 = 300 from the 10-K, 1080 - 800 = 280 from the 10-K/A), carrying the
    accession and form of its FY parent, and both pointing at the same 9M.
    """
    fy_rows = pick(engine, "facts", cik=1003, tag="Revenues", qtrs=4)
    by_acc = {r["accession"]: r for r in fy_rows}
    assert set(by_acc) == {C_K, C_KA}
    assert (by_acc[C_K]["value"], by_acc[C_K]["is_original"], by_acc[C_K]["is_latest"]) == (
        Decimal("1100.0000"),
        True,
        False,
    )
    assert (by_acc[C_KA]["value"], by_acc[C_KA]["is_original"], by_acc[C_KA]["is_latest"]) == (
        Decimal("1080.0000"),
        False,
        True,
    )
    assert {(r["n_versions"], r["is_restated"]) for r in fy_rows} == {(2, True)}
    assert by_acc[C_KA]["form"] == "10-K/A"

    q4 = {r["accession"]: r for r in pick(engine, "facts", cik=1003, tag="Revenues", qtrs=1)}
    assert set(q4) == {C_K, C_KA}
    assert (q4[C_K]["value"], q4[C_K]["is_original"], q4[C_K]["is_latest"]) == (
        Decimal("300.0000"),
        True,
        False,
    )
    assert (q4[C_KA]["value"], q4[C_KA]["is_original"], q4[C_KA]["is_latest"]) == (
        Decimal("280.0000"),
        False,
        True,
    )
    assert q4[C_KA]["form"] == "10-K/A"
    for acc, row in q4.items():
        assert {(r["n_versions"], r["is_restated"]) for r in q4.values()} == {(2, True)}
        fy, nine = parents_of(engine, row)
        assert (fy["accession"], fy["qtrs"], nine["accession"], nine["qtrs"]) == (acc, 4, C_Q, 3)
    # both derived rows share one fact_key (they are two versions of one fact)
    assert len({r["fact_key"] for r in q4.values()}) == 1
    assert len({r["fact_id"] for r in q4.values()}) == 2


def test_gamma_same_value_repeated_in_a_later_filing_is_one_row_citing_the_original(
    engine: EngineResult,
) -> None:
    """NetIncomeLoss 50 appears in the 10-K and again, unchanged, in the 10-K/A: ONE facts row,
    carrying the original (10-K) accession, form and filed date, with both flags set, two
    versions, not restated. facts_history keeps both versions.
    """
    row = one(engine, "facts", cik=1003, tag="NetIncomeLoss")
    assert row["value"] == Decimal("50.0000")
    assert (row["accession"], row["form"], row["filed"]) == (C_K, "10-K", date(2023, 11, 2))
    assert (row["is_latest"], row["is_original"], row["n_versions"], row["is_restated"]) == (
        True,
        True,
        2,
        False,
    )
    history = pick(engine, "facts_history", cik=1003, tag="NetIncomeLoss")
    assert {r["accession"] for r in history} == {C_K, C_KA}
    assert {r["is_amendment"] for r in history} == {True, False}


def test_gamma_operating_income_q4_with_a_single_fy_version_is_one_row(
    engine: EngineResult,
) -> None:
    """OperatingIncomeLoss has one FY (120) and one 9M (80): both provenance tiers derive the same
    row, so there is exactly one Q4, 40, from the 10-K."""
    q4 = one(engine, "facts", cik=1003, tag="OperatingIncomeLoss", qtrs=1)
    assert q4["value"] == Decimal("40.0000")
    assert (q4["accession"], q4["n_versions"], q4["is_restated"]) == (C_K, 1, False)


def test_dimensional_rows_segments_and_their_own_q4(engine: EngineResult) -> None:
    """facts_dimensional holds only the segmented rows: two series (one whose name contains a quote
    and a tab, which must survive the TSV parsing byte for byte), each with FY, 9M and a derived Q4
    (600 - 400 = 200 and 90 - 60 = 30). The co-registrant and consolidated rows are not in it.
    """
    rows = engine.tables["facts_dimensional"]
    assert {r["segments"] for r in rows} == {WIDGETS, QUOTED_SEG}
    values = {(r["segments"], r["qtrs"]): r["value"] for r in rows}
    assert values == {
        (WIDGETS, 4): Decimal("600.0000"),
        (WIDGETS, 3): Decimal("400.0000"),
        (WIDGETS, 1): Decimal("200.0000"),
        (QUOTED_SEG, 4): Decimal("90.0000"),
        (QUOTED_SEG, 3): Decimal("60.0000"),
        (QUOTED_SEG, 1): Decimal("30.0000"),
    }
    for r in rows:
        assert (r["cik"], r["tag"], r["taxonomy"], r["uom"]) == (1001, "Revenues", "us-gaap", "USD")
        assert (r["is_latest"], r["is_original"], r["n_versions"]) == (True, True, 1)
        assert (r["is_restated"], r["is_custom"]) == (False, False)
        assert r["dq_flags"] == (["q4_derived"] if r["qtrs"] == 1 else [])
    q4 = one(engine, "facts_dimensional", segments=QUOTED_SEG, qtrs=1)
    assert (q4["accession"], q4["fiscal_period"], q4["fiscal_year"]) == (A_K, "Q4", 2023)
    assert "derived_from" not in q4  # the contract's facts_dimensional has no such column


def test_the_footnote_with_a_quote_and_tab_does_not_shift_the_row(engine: EngineResult) -> None:
    """The Alpha FY revenue row carries a footnote with a quote and a tab. A parser that split on
    the embedded tab would shift the columns and corrupt `value` (or drop the row); it is 1000.
    """
    assert one(engine, "facts_history", cik=1001, tag="Revenues", qtrs=4)["value"] == Decimal(1000)


def test_dq_flags_is_a_list_never_null_and_anomaly_score_is_null(engine: EngineResult) -> None:
    """Contract: dq_flags is an empty list, not NULL, when there are no flags; facts.anomaly_score
    is NULL for every row (nothing computes it in M1). Non-derived rows have derived_from NULL.
    """
    for table, rows in engine.tables.items():
        assert all(isinstance(r["dq_flags"], list) for r in rows), table
        for r in rows:
            if "derived_from" in r:  # facts_dimensional has no derived_from column
                assert (r["derived_from"] is not None) == ("q4_derived" in r["dq_flags"]), (
                    table,
                    r,
                )
    assert all(r["anomaly_score"] is None for r in engine.tables["facts"])
    plain = one(engine, "facts", cik=1001, tag="CustomMetric")
    assert plain["dq_flags"] == [] and plain["derived_from"] is None


def test_every_facts_row_is_a_history_row_and_ids_are_unique(engine: EngineResult) -> None:
    """Integrity of the hash columns inside one engine: fact_id is unique in facts_history, every
    facts row is the history row with the same fact_id (same accession, value, filed and form),
    and the fact_key groups exactly the versions of one natural key.
    """
    history = engine.tables["facts_history"]
    by_id = {r["fact_id"]: r for r in history}
    assert len(by_id) == len(history)
    for r in engine.tables["facts"]:
        h = by_id[r["fact_id"]]
        assert (r["accession"], r["value"], r["filed"], r["form"], r["fact_key"]) == (
            h["accession"],
            h["value"],
            h["filed"],
            h["form"],
            h["fact_key"],
        )
    keys: dict[int, set[tuple[Any, ...]]] = {}
    for h in history:
        keys.setdefault(h["fact_key"], set()).add(natural_key(h, False)[:-1])
    assert all(len(nk) == 1 for nk in keys.values())
    assert len({h["fact_key"] for h in history}) == len(keys)
    assert len(keys) == len({natural_key(h, False)[:-1] for h in history})


# --- DuckDB output contract ---------------------------------------------------------------


def duck_type(dtype: DataType) -> str:
    """The DuckDB type name of a contract (Spark) column type."""
    if isinstance(dtype, ArrayType):
        return f"{duck_type(dtype.elementType)}[]"
    simple = dtype.simpleString()
    return {
        "bigint": "BIGINT",
        "int": "INTEGER",
        "string": "VARCHAR",
        "date": "DATE",
        "boolean": "BOOLEAN",
        "float": "FLOAT",
        "decimal(28,4)": "DECIMAL(28,4)",
    }[simple]


@pytest.mark.parametrize("table", TABLES)
def test_duckdb_parquet_columns_and_types_equal_the_contract(
    fsds_dir: Path, tmp_path: Path, table: str
) -> None:
    """The Parquet files have exactly the columns, in the order and with the types of
    FACTS_HISTORY / FACTS / FACTS_DIMENSIONAL (pipelines/spark/curated_schemas.py), so a reader
    needs no cast: value DECIMAL(28,4), dates DATE, dq_flags VARCHAR[], derived_from BIGINT[],
    anomaly_score FLOAT.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    duckdb_run(fsds_dir, QUARTERS, out_dir)
    described, _ = read_parquet(out_dir / f"{table}.parquet")
    expected = [(f.name, duck_type(f.dataType)) for f in SCHEMAS[table].fields]
    assert described == expected


def test_run_writes_exactly_the_three_parquet_files(fsds_dir: Path, tmp_path: Path) -> None:
    """Outputs are {out_dir}/facts_history.parquet, facts.parquet and facts_dimensional.parquet.
    No other regular file is left behind (for example the temp file of an atomic write); a
    directory such as DuckDB's spill directory is not checked.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    counts = duckdb_run(fsds_dir, QUARTERS, out_dir)
    assert sorted(p.name for p in out_dir.iterdir() if p.is_file()) == [
        "facts.parquet",
        "facts_dimensional.parquet",
        "facts_history.parquet",
    ]
    assert counts == {"facts_history": 24, "facts": 23, "facts_dimensional": 6}


# --- idempotence and independence ---------------------------------------------------------


def freeze(row: Row) -> Row:
    """Lists to tuples so rows are hashable."""
    return {k: tuple(v) if isinstance(v, list) else v for k, v in row.items()}


def parquet_content(out_dir: Path) -> dict[str, tuple[list[tuple[str, str]], Counter[Any]]]:
    """Schema and the multiset of FULL rows (hash columns included: DuckDB's hash is stable)."""
    content = {}
    for t in TABLES:
        schema, rows = read_parquet(out_dir / f"{t}.parquet")
        frozen = (tuple(sorted(freeze(r).items(), key=str)) for r in rows)
        content[t] = (schema, Counter(frozen))
    return content  # type: ignore[return-value]


def test_a_second_run_into_the_same_out_dir_gives_identical_content(
    fsds_dir: Path, tmp_path: Path
) -> None:
    """Idempotent: rerunning replaces the files rather than appending, so counts and the full
    content (every row, hash columns included) are unchanged."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    first_counts = duckdb_run(fsds_dir, QUARTERS, out_dir)
    first = parquet_content(out_dir)
    second_counts = duckdb_run(fsds_dir, QUARTERS, out_dir)
    assert second_counts == first_counts
    assert parquet_content(out_dir) == first


def test_a_rerun_does_not_depend_on_what_an_earlier_run_left_in_out_dir(
    fsds_dir: Path, tmp_path: Path
) -> None:
    """A full rebuild replaces the whole output: running a smaller quarter set into a directory
    that holds the output of a bigger one gives exactly what a fresh directory gives (no stale
    rows from the earlier run)."""
    reused, fresh = tmp_path / "reused", tmp_path / "fresh"
    reused.mkdir()
    fresh.mkdir()
    duckdb_run(fsds_dir, QUARTERS, reused)
    reused_counts = duckdb_run(fsds_dir, ["2023q4"], reused)
    fresh_counts = duckdb_run(fsds_dir, ["2023q4"], fresh)
    assert reused_counts == fresh_counts
    assert reused_counts["facts_history"] < 24  # the 10-Q rows are really gone
    assert parquet_content(reused) == parquet_content(fresh)


def test_the_order_of_the_quarters_argument_does_not_matter(
    fsds_dir: Path, tmp_path: Path, duck_result: EngineResult
) -> None:
    """'Latest src_quarter wins' is decided by the quarter label, not by the position in the list:
    passing ['2023q4', '2023q3'] gives the same rows as ['2023q3', '2023q4']."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    duckdb_run(fsds_dir, list(reversed(QUARTERS)), out_dir)
    rows = {t: read_parquet(out_dir / f"{t}.parquet")[1] for t in TABLES}
    reversed_result = EngineResult("duckdb", {t: len(r) for t, r in rows.items()}, rows)
    assert canonical(reversed_result) == canonical(duck_result)
    assert one(reversed_result, "facts", cik=1002, tag="Revenues")["value"] == Decimal("520.0000")


def test_threads_and_memory_limit_do_not_change_the_output(
    fsds_dir: Path, tmp_path: Path, duck_result: EngineResult
) -> None:
    """The resource settings are performance knobs only: one thread and a small memory limit give
    the same rows as the defaults."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    limited = duckdb_result(fsds_dir, out_dir, threads=1, memory_limit="1GB")
    assert limited.counts == duck_result.counts
    assert canonical(limited) == canonical(duck_result)


def test_a_missing_quarter_fails_instead_of_producing_a_partial_output(
    fsds_dir: Path, tmp_path: Path
) -> None:
    """A quarter directory (or file) that does not exist is an error, as in the Spark loader
    (FileNotFoundError there); silently normalizing the other quarters would drop facts without a
    trace. Any OS or DuckDB error is accepted."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with pytest.raises((OSError, duckdb.Error)):
        duckdb_run(fsds_dir, ["2023q4", "2099q1"], out_dir)


def test_no_segments_and_no_derived_q4_still_writes_three_typed_files(tmp_path: Path) -> None:
    """A run whose dimensional branch is empty and with no derived row at all writes all three
    files with the contract types: an empty facts_dimensional (0 rows, segments VARCHAR, ...) and
    an all-NULL derived_from that is still BIGINT[] (an untyped NULL column would be INTEGER or
    NULL typed in Parquet and break readers that expect the contract).
    """
    src = build_minimal_fixture(tmp_path / "fsds")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    counts = duckdb_run(src, ["2023q4"], out_dir)
    assert counts == {"facts_history": 1, "facts": 1, "facts_dimensional": 0}
    for table in TABLES:
        described, rows = read_parquet(out_dir / f"{table}.parquet")
        assert described == [(f.name, duck_type(f.dataType)) for f in SCHEMAS[table].fields]
        assert len(rows) == counts[table]
    (row,) = read_parquet(out_dir / "facts.parquet")[1]
    assert (row["value"], row["fiscal_year"], row["fiscal_period"]) == (
        Decimal("1000.0000"),
        2023,
        "FY",
    )
    assert row["dq_flags"] == [] and row["derived_from"] is None and row["anomaly_score"] is None


def test_quarter_names_are_case_insensitive_like_the_spark_loader(
    fsds_dir: Path, duck_result: EngineResult, tmp_path: Path
) -> None:
    """`2023Q3` must behave as `2023q3`: otherwise the quarter of a filing present in two
    quarters is not recognised and the later quarter no longer wins."""
    out = tmp_path / "upper"
    out.mkdir()

    counts = duckdb_run(fsds_dir, [q.upper() for q in QUARTERS], out)

    assert counts == duck_result.counts
    got = sorted(read_parquet(out / "facts.parquet")[1], key=lambda r: r["fact_id"])
    want = sorted(duck_result.tables["facts"], key=lambda r: r["fact_id"])
    assert got == want
