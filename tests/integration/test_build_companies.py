"""Integration tests for ``pipelines/spark/build_companies.py`` (contract section 2.2 companies).

``lake.curated.companies`` has one row per CIK: the universe is every CIK in the raw FSDS ``sub``
table plus every CIK in the SEC ticker map. Ticker, name, SIC, fiscal-year-end and S&P 500
membership are derived as the module's docstring in the runbook describes; ``in_financebench`` is
always NULL until FinanceBench is loaded (never guessed). Runs on a local Spark session with an
Iceberg hadoop catalog (see ``tests/integration/conftest.py``); inputs are tiny hand-built frames.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pyspark.sql import DataFrame, Row, SparkSession
from pyspark.sql.types import IntegerType, StringType, StructField, StructType

from pipelines.spark.build_companies import (
    build_companies,
    normalize_ticker,
    run,
    unmatched_sp500,
    warn_if_snapshot_dates_differ,
)
from pipelines.spark.curated_schemas import COMPANIES
from tests.raw_builders import sub_df

# The contract's column order and types (05_data_contracts.md section 2.2), spelled out here
# independently of COMPANIES so a wrong COMPANIES cannot make a wrong table look right.
CONTRACT_DTYPES = [
    ("cik", "int"),
    ("name", "string"),
    ("ticker", "string"),
    ("sic", "int"),
    ("fye_mmdd", "string"),
    ("in_sp500", "boolean"),
    ("in_financebench", "boolean"),
    ("aliases", "array<string>"),
]

TICKERS_SCHEMA = StructType(
    [
        StructField("cik", IntegerType()),
        StructField("ticker", StringType()),
        StructField("name", StringType()),
        StructField("rank", IntegerType()),
    ]
)
SP500_SCHEMA = StructType([StructField("ticker", StringType()), StructField("name", StringType())])

AAPL, GOOGL_CIK, BRK, SUB_ONLY, TICKER_ONLY = 320193, 1652044, 1067983, 888002, 999001


# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------


def S(cik: int, n: int, filed: int, **kw: Any) -> dict[str, Any]:
    """A raw sub row for ``cik``. ``n`` makes the accession unique and orders ties (adsh sorts
    by ``n`` within one cik)."""
    return {"adsh": f"{cik:010d}-23-{n:06d}", "cik": cik, "filed": filed} | kw


def tickers_df(spark: SparkSession, rows: list[tuple[int, str, str, int]]) -> DataFrame:
    """(cik, ticker, name, rank) rows."""
    return spark.createDataFrame(rows, TICKERS_SCHEMA)


def sp500_df(spark: SparkSession, rows: list[tuple[str, str]]) -> DataFrame:
    return spark.createDataFrame(rows, SP500_SCHEMA)


def companies(
    spark: SparkSession,
    sub_rows: list[dict[str, Any]],
    ticker_rows: list[tuple[int, str, str, int]],
    sp500_rows: list[tuple[str, str]] | None = None,
) -> dict[int, Row]:
    """build_companies on tiny inputs, collected by cik (asserting one row per cik)."""
    out = build_companies(
        sub_df(spark, sub_rows), tickers_df(spark, ticker_rows), sp500_df(spark, sp500_rows or [])
    ).collect()
    by_cik = {r.cik: r for r in out}
    assert len(by_cik) == len(out), "more than one row for some cik"
    return by_cik


# --------------------------------------------------------------------------------------
# normalize_ticker
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BRK.B", "BRK-B"),
        ("brk.b", "BRK-B"),
        ("BF.B", "BF-B"),
        ("  aapl  ", "AAPL"),
        ("AAPL", "AAPL"),
        ("BRK-B", "BRK-B"),
        ("A.B.C", "A-B-C"),
        ("\tmsft\n", "MSFT"),
        ("", ""),
    ],
)
def test_normalize_ticker_examples(raw: str, expected: str):
    """Upper-case, strip, every '.' becomes '-' (S&P says BRK.B, the SEC says BRK-B)."""
    assert normalize_ticker(raw) == expected


# --------------------------------------------------------------------------------------
# build_companies: schema and universe
# --------------------------------------------------------------------------------------


def test_result_schema_is_exactly_the_contract(spark: SparkSession):
    """Names, order and types equal contract 2.2 and the COMPANIES StructType."""
    df = build_companies(
        sub_df(spark, [S(AAPL, 1, 20231103)]),
        tickers_df(spark, [(AAPL, "AAPL", "Apple Inc.", 0)]),
        sp500_df(spark, []),
    )

    assert df.dtypes == CONTRACT_DTYPES
    assert df.dtypes == [(f.name, f.dataType.simpleString()) for f in COMPANIES.fields]
    assert df.columns == [name for name, _ in CONTRACT_DTYPES]


def test_one_row_per_cik_whatever_the_number_of_filings_and_tickers(spark: SparkSession):
    """Apple has three filings and Alphabet two tickers; the table still has one row per CIK."""
    sub = [S(AAPL, n, 20230101 + n) for n in range(3)] + [S(GOOGL_CIK, 1, 20240202)]
    tickers = [
        (AAPL, "AAPL", "Apple Inc.", 0),
        (GOOGL_CIK, "GOOGL", "Alphabet Inc.", 7),
        (GOOGL_CIK, "GOOG", "Alphabet Inc.", 8),
    ]

    by_cik = companies(spark, sub, tickers)

    assert set(by_cik) == {AAPL, GOOGL_CIK}


def test_universe_is_the_union_of_sub_and_ticker_ciks(spark: SparkSession):
    """A CIK only in sub, a CIK only in the ticker map, and a CIK in both: all three appear once."""
    sub = [S(AAPL, 1, 20231103), S(SUB_ONLY, 1, 20230901, name="Sub Only Co")]
    tickers = [(AAPL, "AAPL", "Apple Inc.", 0), (TICKER_ONLY, "ZZZ", "Zed Corp", 500)]

    by_cik = companies(spark, sub, tickers)

    assert set(by_cik) == {AAPL, SUB_ONLY, TICKER_ONLY}


def test_ticker_map_only_company_is_kept_with_null_sic_and_fye(spark: SparkSession):
    """No filings in the lake yet: the company still exists, with unknown (NULL) SIC and FYE."""
    by_cik = companies(spark, [S(AAPL, 1, 20231103)], [(TICKER_ONLY, "ZZZ", "Zed Corp", 500)])

    row = by_cik[TICKER_ONLY]
    assert row.name == "Zed Corp"
    assert row.ticker == "ZZZ"
    assert row.sic is None
    assert row.fye_mmdd is None
    assert row.aliases == []
    assert row.in_sp500 is False
    assert row.in_financebench is None


def test_sub_only_company_has_no_ticker_empty_aliases_and_not_in_sp500(spark: SparkSession):
    """A filer without a current ticker: ticker NULL, aliases empty (not NULL), name from sub."""
    sub = [S(SUB_ONLY, 1, 20230901, name="Sub Only Co", sic=1000, fye="0630")]

    by_cik = companies(spark, sub, [(AAPL, "AAPL", "Apple Inc.", 0)], [("AAPL", "Apple Inc.")])

    row = by_cik[SUB_ONLY]
    assert row.ticker is None
    assert row.aliases == []
    assert row.in_sp500 is False
    assert row.name == "Sub Only Co"
    assert (row.sic, row.fye_mmdd) == (1000, "0630")


def test_no_sub_rows_at_all_still_builds_from_the_ticker_map(spark: SparkSession):
    """An empty sub table (nothing ingested) yields the ticker-map universe, not an error."""
    by_cik = companies(spark, [], [(AAPL, "AAPL", "Apple Inc.", 0)])

    assert set(by_cik) == {AAPL}
    assert (by_cik[AAPL].sic, by_cik[AAPL].fye_mmdd) == (None, None)


# --------------------------------------------------------------------------------------
# build_companies: ticker and aliases
# --------------------------------------------------------------------------------------


def test_multi_ticker_cik_takes_lowest_rank_as_ticker_and_the_rest_as_aliases(spark: SparkSession):
    """GOOGL (rank 7) is the ticker and GOOG (rank 8) the alias, though GOOG is listed first."""
    tickers = [
        (GOOGL_CIK, "GOOG", "Alphabet Inc. Class C", 8),
        (GOOGL_CIK, "GOOGL", "Alphabet Inc. Class A", 7),
    ]

    row = companies(spark, [S(GOOGL_CIK, 1, 20240202, name="ALPHABET INC.")], tickers)[GOOGL_CIK]

    assert row.ticker == "GOOGL"
    assert row.aliases == ["GOOG"]


def test_aliases_follow_rank_order_not_alphabetical_or_input_order(spark: SparkSession):
    """ZED (rank 5) is the ticker; aliases are MID (12) then ABC (30), i.e. by rank."""
    tickers = [(1, "ABC", "X", 30), (1, "ZED", "X", 5), (1, "MID", "X", 12)]

    row = companies(spark, [S(1, 1, 20230101)], tickers)[1]

    assert row.ticker == "ZED"
    assert row.aliases == ["MID", "ABC"]


def test_aliases_never_repeat_the_primary_ticker_or_each_other(spark: SparkSession):
    """A cik listed twice under one ticker must not list that ticker as its own alias."""
    tickers = [(1, "AAA", "X", 1), (1, "AAA", "X", 2), (1, "BBB", "X", 3), (1, "BBB", "X", 4)]

    row = companies(spark, [S(1, 1, 20230101)], tickers)[1]

    assert row.ticker == "AAA"
    assert row.aliases == ["BBB"]


def test_aliases_is_an_empty_array_not_null_for_a_single_ticker(spark: SparkSession):
    """Consumers can iterate aliases without a NULL check."""
    row = companies(spark, [S(AAPL, 1, 20231103)], [(AAPL, "AAPL", "Apple Inc.", 0)])[AAPL]

    assert row.aliases is not None
    assert row.aliases == []


# --------------------------------------------------------------------------------------
# build_companies: name
# --------------------------------------------------------------------------------------


def test_name_is_the_title_of_the_lowest_rank_ticker_row(spark: SparkSession):
    """The ticker-map title wins over the (upper-case, historical) sub name, and it is the
    lowest-rank row's title, not the other ticker's."""
    sub = [S(GOOGL_CIK, 1, 20240202, name="ALPHABET INC.")]
    tickers = [
        (GOOGL_CIK, "GOOG", "Alphabet Inc. Class C", 8),
        (GOOGL_CIK, "GOOGL", "Alphabet Inc. Class A", 7),
    ]

    assert companies(spark, sub, tickers)[GOOGL_CIK].name == "Alphabet Inc. Class A"


def test_name_falls_back_to_latest_non_blank_sub_name(spark: SparkSession):
    """No ticker row: use the newest sub name that is not NULL or empty (an 8-K with a blank
    name filed last must not blank the company)."""
    sub = [
        S(SUB_ONLY, 1, 20230101, name="Old Name"),
        S(SUB_ONLY, 2, 20230601, name="New Name"),
        S(SUB_ONLY, 3, 20231201, name=""),
        S(SUB_ONLY, 4, 20240101, name=None),
    ]

    assert companies(spark, sub, [])[SUB_ONLY].name == "New Name"


def test_name_ties_on_filed_resolve_to_the_greatest_accession(spark: SparkSession):
    """Two filings on the same day: the larger adsh is 'latest' (the same rule as sic/fye)."""
    sub = [
        S(SUB_ONLY, 2, 20230601, name="Second"),
        S(SUB_ONLY, 1, 20230601, name="First"),
        S(SUB_ONLY, 3, 20230101, name="Older"),
    ]

    assert companies(spark, sub, [])[SUB_ONLY].name == "Second"


# --------------------------------------------------------------------------------------
# build_companies: sic and fye_mmdd (latest non-null, independently per column)
# --------------------------------------------------------------------------------------

K_10 = {"form": "10-K"}
K_8 = {"form": "8-K"}

SIC_FYE_CASES = [
    pytest.param(
        [
            S(5, 1, 20231103, sic=3571, fye="0930", **K_10),
            S(5, 2, 20231201, sic=None, fye=None, **K_8),
        ],
        (3571, "0930"),
        id="8k_after_10k_with_nulls_does_not_null_out",
    ),
    pytest.param(
        [
            S(5, 1, 20231103, sic=3571, fye="0930", **K_10),
            S(5, 2, 20231201, sic=None, fye="", **K_8),
        ],
        (3571, "0930"),
        id="empty_string_fye_counts_as_blank",
    ),
    pytest.param(
        [S(5, 1, 20230101, sic=3571, fye="0630"), S(5, 2, 20240101, sic=3572, fye=None)],
        (3572, "0630"),
        id="columns_resolve_independently",
    ),
    pytest.param(
        [S(5, 1, 20240101, sic=None, fye="0630"), S(5, 2, 20230101, sic=3571, fye=None)],
        (3571, "0630"),
        id="each_column_from_a_different_row_input_order_reversed",
    ),
    pytest.param(
        [S(5, 1, 20220101, sic=3571, fye="1231"), S(5, 2, 20230101, sic=3571, fye="0930")],
        (3571, "0930"),
        id="fye_change_uses_the_latest_not_the_max_string",
    ),
    pytest.param(
        [S(5, 2, 20230601, sic=2222, fye="0331"), S(5, 1, 20230601, sic=1111, fye="1130")],
        (2222, "0331"),
        id="same_filed_date_greatest_adsh_wins",
    ),
    pytest.param(
        [S(5, 1, 20230601, sic=None, fye=None), S(5, 2, 20240601, sic=None, fye="")],
        (None, None),
        id="no_usable_value_gives_null",
    ),
]


@pytest.mark.parametrize(("sub_rows", "expected"), SIC_FYE_CASES)
def test_sic_and_fye_come_from_the_latest_non_blank_row(
    spark: SparkSession, sub_rows: list[dict[str, Any]], expected: tuple[int | None, str | None]
):
    """Each of sic and fye_mmdd is the value on the newest sub row (max filed, ties max adsh)
    where that column is non-null and non-empty. The two are resolved independently."""
    row = companies(spark, sub_rows, [])[5]

    assert (row.sic, row.fye_mmdd) == expected


def test_sic_and_fye_are_per_company(spark: SparkSession):
    """A later filing by another CIK is not 'latest' for this one; companies are independent."""
    sub = [
        S(AAPL, 1, 20230101, sic=3571, fye="0930"),
        S(BRK, 1, 20240101, sic=6331, fye="1231"),
    ]

    by_cik = companies(spark, sub, [])

    assert (by_cik[AAPL].sic, by_cik[AAPL].fye_mmdd) == (3571, "0930")
    assert (by_cik[BRK].sic, by_cik[BRK].fye_mmdd) == (6331, "1231")


# --------------------------------------------------------------------------------------
# build_companies: in_sp500 and in_financebench
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("company_tickers", "sp500", "expected"),
    [
        (["BRK-B"], ["BRK.B"], True),
        (["BRK.B"], ["BRK-B"], True),
        (["AAPL"], [" aapl "], True),
        (["GOOGL", "GOOG"], ["GOOG"], True),
        (["GOOGL", "GOOG"], ["GOOGL"], True),
        (["A"], ["AA"], False),
        (["AA"], ["A"], False),
        (["AAPL"], ["MSFT", "GOOG"], False),
        (["AAPL"], [], False),
    ],
    ids=[
        "dot_in_sp500_dash_in_map",
        "dot_in_map_dash_in_sp500",
        "case_and_whitespace",
        "alias_matches",
        "primary_matches",
        "prefix_is_not_a_match",
        "longer_is_not_a_match",
        "different_tickers",
        "empty_sp500_list",
    ],
)
def test_in_sp500_matches_any_ticker_after_normalization(
    spark: SparkSession, company_tickers: list[str], sp500: list[str], expected: bool
):
    """True iff any of the company's tickers equals any S&P ticker once both go through
    normalize_ticker. Always a real boolean, never NULL."""
    ticker_rows = [(1, t, "Co", rank) for rank, t in enumerate(company_tickers)]
    sp_rows = [(t, f"{t} Corp") for t in sp500]

    row = companies(spark, [S(1, 1, 20230101)], ticker_rows, sp_rows)[1]

    assert row.in_sp500 is expected


def test_in_sp500_is_per_company_and_never_null(spark: SparkSession):
    """Mixed universe: member, non-member with a ticker, ticker-less filer, ticker-map-only."""
    sub = [S(AAPL, 1, 20231103), S(BRK, 1, 20240301), S(SUB_ONLY, 1, 20230901)]
    tickers = [
        (AAPL, "AAPL", "Apple Inc.", 0),
        (BRK, "BRK-B", "BERKSHIRE HATHAWAY INC", 11),
        (TICKER_ONLY, "ZZZ", "Zed Corp", 500),
    ]

    by_cik = companies(spark, sub, tickers, [("AAPL", "Apple Inc."), ("BRK.B", "Berkshire")])

    assert {cik: r.in_sp500 for cik, r in by_cik.items()} == {
        AAPL: True,
        BRK: True,
        SUB_ONLY: False,
        TICKER_ONLY: False,
    }


def test_in_financebench_is_null_for_every_row(spark: SparkSession):
    """FinanceBench is not loaded until M2/M4: NULL (unknown), never False and never guessed."""
    sub = [S(AAPL, 1, 20231103), S(SUB_ONLY, 1, 20230901)]
    tickers = [(AAPL, "AAPL", "Apple Inc.", 0), (TICKER_ONLY, "ZZZ", "Zed Corp", 500)]

    out = build_companies(
        sub_df(spark, sub), tickers_df(spark, tickers), sp500_df(spark, [("AAPL", "Apple Inc.")])
    )

    rows = out.collect()
    assert len(rows) == 3
    assert [r.in_financebench for r in rows] == [None, None, None]


# --------------------------------------------------------------------------------------
# unmatched_sp500
# --------------------------------------------------------------------------------------


def _unmatched(
    spark: SparkSession, tickers: list[tuple[int, str, str, int]], sp: list[tuple[str, str]]
):
    df = unmatched_sp500(tickers_df(spark, tickers), sp500_df(spark, sp))
    return df, {(normalize_ticker(r.ticker), r.name) for r in df.collect()}


def test_unmatched_sp500_returns_rows_without_a_ticker_map_match(spark: SparkSession):
    """Reported, not silently dropped: XYZ and ZZ.Y have no ticker row; BRK.B matches BRK-B."""
    tickers = [(AAPL, "AAPL", "Apple Inc.", 0), (BRK, "BRK-B", "BERKSHIRE", 11)]
    sp = [
        ("AAPL", "Apple Inc."),
        ("BRK.B", "Berkshire"),
        ("XYZ", "Xyz Corp"),
        ("ZZ.Y", "Zed, Yankee & Co."),
    ]

    df, got = _unmatched(spark, tickers, sp)

    assert df.columns == ["ticker", "name"]
    assert got == {("XYZ", "Xyz Corp"), ("ZZ-Y", "Zed, Yankee & Co.")}


def test_unmatched_sp500_counts_an_alias_row_as_a_match(spark: SparkSession):
    """GOOG is a lower-priority row of the same CIK; it is still a ticker-map row, so it matches."""
    tickers = [(GOOGL_CIK, "GOOGL", "Alphabet", 7), (GOOGL_CIK, "GOOG", "Alphabet", 8)]

    _, got = _unmatched(spark, tickers, [("GOOG", "Alphabet C"), ("GOOGL", "Alphabet A")])

    assert got == set()


def test_unmatched_sp500_is_empty_when_everything_matches_and_keeps_its_columns(
    spark: SparkSession,
):
    df, got = _unmatched(spark, [(AAPL, "AAPL", "Apple", 0)], [("AAPL", "Apple")])

    assert got == set()
    assert df.columns == ["ticker", "name"]


def test_unmatched_sp500_with_an_empty_ticker_map_returns_every_sp500_row(spark: SparkSession):
    _, got = _unmatched(spark, [], [("AAPL", "Apple"), ("MSFT", "Microsoft")])

    assert got == {("AAPL", "Apple"), ("MSFT", "Microsoft")}


# --------------------------------------------------------------------------------------
# run(): files in, Iceberg table out
# --------------------------------------------------------------------------------------

TICKER_DOC: dict[str, Any] = {
    "0": {"cik_str": AAPL, "ticker": "AAPL", "title": "Apple Inc."},
    "7": {"cik_str": str(GOOGL_CIK), "ticker": "GOOGL", "title": "Alphabet Inc. Class A"},
    "8": {"cik_str": GOOGL_CIK, "ticker": "GOOG", "title": "Alphabet Inc. Class C"},
    "11": {"cik_str": BRK, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
    "500": {"cik_str": TICKER_ONLY, "ticker": "ZZZ", "title": "Zed Corp"},
}
SP500_ROWS = [
    ("AAPL", "Apple Inc."),
    ("BRK.B", "Berkshire Hathaway Inc., Class B"),
    ("GOOG", "Alphabet Inc. Class C"),
    ("NOPE", 'Nope "Quoted" Inc.'),
    ("ZZ.Y", "Zed, Yankee & Co., Class B"),
]
RUN_SUB = [
    S(AAPL, 1, 20231103, name="Apple Inc.", sic=3571, fye="0930", form="10-K"),
    S(AAPL, 2, 20231201, name="Apple Inc.", sic=None, fye=None, form="8-K"),
    S(GOOGL_CIK, 1, 20240202, name="Alphabet Inc.", sic=7370, fye="1231"),
    S(BRK, 1, 20240301, name="Berkshire Hathaway Inc", sic=6331, fye="1231"),
    S(SUB_ONLY, 1, 20230901, name="Sub Only Co", sic=1000, fye="0630"),
]
EXPECTED_RUN_ROWS = {
    AAPL: ("Apple Inc.", "AAPL", 3571, "0930", True, None, []),
    GOOGL_CIK: ("Alphabet Inc. Class A", "GOOGL", 7370, "1231", True, None, ["GOOG"]),
    BRK: ("BERKSHIRE HATHAWAY INC", "BRK-B", 6331, "1231", True, None, []),
    SUB_ONLY: ("Sub Only Co", None, 1000, "0630", False, None, []),
    TICKER_ONLY: ("Zed Corp", "ZZZ", None, None, False, None, []),
}
EXPECTED_RUN_COUNTS = {"companies": 5, "in_sp500": 3, "sp500_rows": 5, "sp500_unmatched": 2}


def write_tickers(path: Path, doc: dict[str, Any]) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def write_sp500(path: Path, rows: list[tuple[str, str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ticker", "name"])
        writer.writerows(rows)
    return path


def make_raw_sub(spark: SparkSession, ns: str, rows: list[dict[str, Any]]) -> None:
    sub_df(spark, rows).writeTo(f"{ns}.fsds_sub").using("iceberg").create()


def table_rows(spark: SparkSession, table: str) -> dict[int, tuple[Any, ...]]:
    out = spark.table(table).collect()
    by_cik = {
        r.cik: (r.name, r.ticker, r.sic, r.fye_mmdd, r.in_sp500, r.in_financebench, list(r.aliases))
        for r in out
    }
    assert len(by_cik) == len(out), f"duplicate cik rows in {table}"
    return by_cik


@pytest.fixture
def frozen(tmp_path: Path) -> tuple[Path, Path]:
    return (
        write_tickers(tmp_path / "company_tickers_2026-11-16.json", TICKER_DOC),
        write_sp500(tmp_path / "sp500_2026-11-16.csv", SP500_ROWS),
    )


def test_run_writes_the_expected_table_and_returns_counts(
    spark: SparkSession, lake_namespace: str, frozen: tuple[Path, Path]
):
    """End to end from the frozen files and raw sub: five companies (union), Alphabet's alias,
    S&P membership through BRK.B/BRK-B and the GOOG alias, and the four reported counts."""
    ns = lake_namespace
    make_raw_sub(spark, ns, RUN_SUB)

    result = run(spark, *frozen, raw_ns=ns, curated_ns=ns)

    assert result == EXPECTED_RUN_COUNTS
    assert table_rows(spark, f"{ns}.companies") == EXPECTED_RUN_ROWS
    assert spark.table(f"{ns}.companies").dtypes == CONTRACT_DTYPES


def test_run_twice_gives_identical_content_and_no_duplicates(
    spark: SparkSession, lake_namespace: str, frozen: tuple[Path, Path]
):
    """Idempotent: a rerun replaces the table rather than appending to it."""
    ns = lake_namespace
    make_raw_sub(spark, ns, RUN_SUB)

    first = run(spark, *frozen, raw_ns=ns, curated_ns=ns)
    content_after_first = table_rows(spark, f"{ns}.companies")
    second = run(spark, *frozen, raw_ns=ns, curated_ns=ns)

    assert second == first == EXPECTED_RUN_COUNTS
    assert spark.table(f"{ns}.companies").count() == 5
    assert table_rows(spark, f"{ns}.companies") == content_after_first


def test_run_replaces_the_whole_table_so_removed_companies_disappear(
    spark: SparkSession, lake_namespace: str, tmp_path: Path, frozen: tuple[Path, Path]
):
    """A second run on a smaller ticker map drops the company that is gone (replace, not merge)."""
    ns = lake_namespace
    make_raw_sub(spark, ns, RUN_SUB)
    run(spark, *frozen, raw_ns=ns, curated_ns=ns)
    smaller = {k: v for k, v in TICKER_DOC.items() if k != "500"}
    tickers2 = write_tickers(tmp_path / "company_tickers_2026-11-17.json", smaller)

    result = run(spark, tickers2, frozen[1], raw_ns=ns, curated_ns=ns)

    assert result["companies"] == 4
    assert set(table_rows(spark, f"{ns}.companies")) == {AAPL, GOOGL_CIK, BRK, SUB_ONLY}


def test_run_replaces_a_pre_existing_table_with_a_different_schema(
    spark: SparkSession, lake_namespace: str, frozen: tuple[Path, Path]
):
    """createOrReplace: a stale table with other columns and rows does not survive or block."""
    ns = lake_namespace
    make_raw_sub(spark, ns, RUN_SUB)
    spark.sql(f"CREATE TABLE {ns}.companies (junk int) USING iceberg")
    spark.sql(f"INSERT INTO {ns}.companies VALUES (42)")

    run(spark, *frozen, raw_ns=ns, curated_ns=ns)

    assert spark.table(f"{ns}.companies").dtypes == CONTRACT_DTYPES
    assert table_rows(spark, f"{ns}.companies") == EXPECTED_RUN_ROWS


@pytest.fixture
def curated_namespace(spark: SparkSession, lake_namespace: str) -> Iterator[str]:
    """A second throwaway namespace (``<raw>_cur``) so raw_ns and curated_ns can differ."""
    name = f"{lake_namespace}_cur"
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {name}")
    yield name
    for row in spark.sql(f"SHOW TABLES IN {name}").collect():
        spark.sql(f"DROP TABLE IF EXISTS {name}.{row.tableName} PURGE")
    spark.sql(f"DROP NAMESPACE IF EXISTS {name}")


def test_run_reads_raw_ns_and_writes_curated_ns(
    spark: SparkSession, lake_namespace: str, curated_namespace: str, frozen: tuple[Path, Path]
):
    """Input comes from {raw_ns}.fsds_sub and output goes to {curated_ns}.companies only."""
    make_raw_sub(spark, lake_namespace, RUN_SUB)

    run(spark, *frozen, raw_ns=lake_namespace, curated_ns=curated_namespace)

    assert table_rows(spark, f"{curated_namespace}.companies") == EXPECTED_RUN_ROWS
    raw_tables = {r.tableName for r in spark.sql(f"SHOW TABLES IN {lake_namespace}").collect()}
    assert raw_tables == {"fsds_sub"}


def test_run_with_an_invalid_ticker_file_raises_and_leaves_the_table_alone(
    spark: SparkSession, lake_namespace: str, tmp_path: Path, frozen: tuple[Path, Path]
):
    """An empty ticker document fails (parse_company_tickers) before the table is touched."""
    ns = lake_namespace
    make_raw_sub(spark, ns, RUN_SUB)
    run(spark, *frozen, raw_ns=ns, curated_ns=ns)
    bad = write_tickers(tmp_path / "bad.json", {})

    with pytest.raises(ValueError):
        run(spark, bad, frozen[1], raw_ns=ns, curated_ns=ns)

    assert table_rows(spark, f"{ns}.companies") == EXPECTED_RUN_ROWS


# --------------------------------------------------------------------------------------
# the two frozen files should come from the same day
# --------------------------------------------------------------------------------------


def test_snapshot_date_mismatch_is_warned_about(caplog: pytest.LogCaptureFixture):
    caplog.set_level("WARNING")

    warn_if_snapshot_dates_differ(
        Path("data/company_tickers_2026-11-17.json"), Path("data/sp500_2026-11-10.csv")
    )

    assert "2026-11-17" in caplog.text and "2026-11-10" in caplog.text


def test_matching_snapshot_dates_are_silent(caplog: pytest.LogCaptureFixture):
    caplog.set_level("WARNING")

    warn_if_snapshot_dates_differ(
        Path("data/company_tickers_2026-11-17.json"), Path("data/sp500_2026-11-17.csv")
    )

    assert caplog.text == ""


def test_undated_snapshot_names_are_not_an_error(caplog: pytest.LogCaptureFixture):
    """Explicit --tickers/--sp500 paths need not carry a date: nothing to compare, no warning."""
    caplog.set_level("WARNING")

    warn_if_snapshot_dates_differ(Path("tickers.json"), Path("sp500.csv"))

    assert caplog.text == ""
