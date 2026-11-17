"""Integration tests for `pipelines/spark/fsds_to_iceberg.py` (local Spark + Iceberg).

Synthetic mini quarters are written in the exact layout the downloader produces, with the real
FSDS headers (taken from the schemas, which were checked against the 2023q4 files).
"""

from __future__ import annotations

import gzip
from decimal import Decimal
from pathlib import Path

import pytest
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from pipelines.spark.fsds_to_iceberg import (
    HeaderMismatch,
    RowCountMismatch,
    count_source_rows,
    load_quarter,
    main,
)
from pipelines.spark.raw_schemas import RAW_SUB_COLUMNS, SOURCE_SCHEMAS

Row = dict[str, str]


def sec_field(value: str) -> str:
    """Quote a field like the SEC: wrap in " (doubling inner quotes) if it holds " or a tab."""
    if '"' in value or "\t" in value:
        return '"' + value.replace('"', '""') + '"'
    return value


def write_quarter(src: Path, quarter: str, rows: dict[str, list[Row]]) -> None:
    """Write `{src}/{quarter}/{name}.txt.gz` with the real header and the given rows.

    Missing keys become empty fields, as in the real files.
    """
    qdir = src / quarter
    qdir.mkdir(parents=True, exist_ok=True)
    for name, schema in SOURCE_SCHEMAS.items():
        cols = schema.fieldNames()
        lines = ["\t".join(cols)]
        for row in rows.get(name, []):
            unknown = set(row) - set(cols)
            assert not unknown, f"{name}: unknown columns {unknown}"
            lines.append("\t".join(sec_field(row.get(c, "")) for c in cols))
        with gzip.open(qdir / f"{name}.txt.gz", "wb") as fh:
            fh.write(("\n".join(lines) + "\n").encode("utf-8"))


def sub_row(adsh: str, **kw: str) -> Row:
    base = {
        "adsh": adsh,
        "cik": "320193",
        "name": "Apple Inc.",
        "sic": "3571",
        "countryba": "US",
        "wksi": "1",
        "fye": "0930",
        "form": "10-K",
        "period": "20230930",
        "fy": "2023",
        "fp": "FY",
        "filed": "20231103",
        "accepted": "2023-11-02 18:08:00.0",
        "prevrpt": "0",
        "detail": "0",
        "instance": "aapl-20230930_htm.xml",
        "nciks": "1",
    }
    return base | kw


def num_row(adsh: str, **kw: str) -> Row:
    base = {
        "adsh": adsh,
        "tag": "Assets",
        "version": "us-gaap/2023",
        "ddate": "20230930",
        "qtrs": "0",
        "uom": "USD",
        "value": "352583000000.0000",
    }
    return base | kw


def tag_row(**kw: str) -> Row:
    base = {
        "tag": "Assets",
        "version": "us-gaap/2023",
        "custom": "0",
        "abstract": "0",
        "datatype": "monetary",
        "iord": "I",
        "crdr": "D",
        "tlabel": "Assets",
        "doc": "Sum of the carrying amounts.",
    }
    return base | kw


def pre_row(adsh: str, **kw: str) -> Row:
    base = {
        "adsh": adsh,
        "report": "2",
        "line": "9",
        "stmt": "BS",
        "inpth": "0",
        "rfile": "H",
        "tag": "Assets",
        "version": "us-gaap/2023",
        "plabel": "Total assets",
        "negating": "0",
    }
    return base | kw


A1 = "0000320193-23-000106"
A2 = "0000320193-23-000077"


@pytest.fixture
def src(tmp_path: Path) -> Path:
    return tmp_path / "fsds"


def basic_rows() -> dict[str, list[Row]]:
    return {
        "sub": [sub_row(A1), sub_row(A2, form="10-Q", fp="Q3", period="20230701")],
        "num": [
            num_row(A1),
            num_row(A1, tag="Revenues", qtrs="4", value="383285000000.0000"),
            num_row(A2, tag="Revenues", qtrs="3", ddate="20230701", value="-1.5000"),
        ],
        "tag": [tag_row(), tag_row(tag="Revenues", datatype="monetary", iord="D", crdr="C")],
        "pre": [pre_row(A1), pre_row(A1, line="10", tag="Revenues")],
    }


def table(ns: str, name: str) -> str:
    return f"{ns}.fsds_{name}"


def test_row_counts_equal_source_lines_minus_header(
    spark: SparkSession, lake_namespace: str, src: Path
):
    write_quarter(src, "2023q4", basic_rows())

    counts = load_quarter(spark, src, "2023q4", lake_namespace)

    assert counts == {"sub": 2, "num": 3, "tag": 2, "pre": 2}
    for name, n in counts.items():
        assert spark.table(table(lake_namespace, name)).count() == n
        assert count_source_rows(src / "2023q4" / f"{name}.txt.gz") == n


def test_absent_coreg_and_segments_load_as_null_not_empty_string(
    spark: SparkSession, lake_namespace: str, src: Path
):
    """Decision recorded here: an empty field is NULL (Spark's default nullValue is '').

    `segments` and `coreg` are empty for the consolidated entity, so the consolidated filter
    is `coreg IS NULL AND segments IS NULL`; normalize_facts also accepts '' to stay safe.
    """
    rows = basic_rows()
    rows["num"].append(num_row(A1, tag="Revenues", segments="ProductOrServiceAxis=IPhoneMember"))
    rows["num"].append(num_row(A1, tag="Revenues", coreg="0000000001"))
    write_quarter(src, "2023q4", rows)

    load_quarter(spark, src, "2023q4", lake_namespace)
    num = spark.table(table(lake_namespace, "num"))

    consolidated = num.where(F.col("tag") == "Assets")
    assert consolidated.where(F.col("segments").isNull() & F.col("coreg").isNull()).count() == 1
    assert num.where(F.col("segments") == "").count() == 0
    assert num.where(F.col("coreg") == "").count() == 0
    assert num.where(F.col("segments").isNotNull()).count() == 1
    assert num.where(F.col("coreg").isNotNull()).count() == 1


def test_value_is_exact_decimal_28_4(spark: SparkSession, lake_namespace: str, src: Path):
    """A double would round 12345678901234567.8901; decimal(28,4) must not."""
    rows = basic_rows()
    rows["num"].append(num_row(A1, tag="Big", qtrs="4", value="12345678901234567890123.4567"))
    rows["num"].append(num_row(A1, tag="Neg", qtrs="4", value="-0.0001"))
    write_quarter(src, "2023q4", rows)

    load_quarter(spark, src, "2023q4", lake_namespace)
    num = spark.table(table(lake_namespace, "num"))

    assert dict(num.dtypes)["value"] == "decimal(28,4)"
    values = {r.tag: r.value for r in num.where(F.col("tag").isin("Big", "Neg")).collect()}
    assert values["Big"] == Decimal("12345678901234567890123.4567")
    assert values["Neg"] == Decimal("-0.0001")


def test_text_fields_keep_quotes_backslashes_and_unicode(
    spark: SparkSession, lake_namespace: str, src: Path
):
    """Quotes, backslashes and non-ASCII text survive a round trip through SEC-style quoting."""
    rows = basic_rows()
    nasty = 'He said "hi" \\ done \\" end café €'
    rows["sub"][0] = sub_row(A1, name=nasty)
    rows["tag"][0] = tag_row(tlabel='"Quoted" label', doc='C:\\path\\to "x"')
    write_quarter(src, "2023q4", rows)

    load_quarter(spark, src, "2023q4", lake_namespace)

    name = spark.table(table(lake_namespace, "sub")).where(F.col("adsh") == A1).first()
    assert name is not None and name["name"] == nasty
    tag = spark.table(table(lake_namespace, "tag")).where(F.col("tag") == "Assets").first()
    assert tag is not None
    assert tag["tlabel"] == '"Quoted" label'
    assert tag["doc"] == 'C:\\path\\to "x"'


def test_leading_and_trailing_spaces_are_not_trimmed(
    spark: SparkSession, lake_namespace: str, src: Path
):
    rows = basic_rows()
    rows["sub"][0] = sub_row(A1, name="  padded name  ")
    write_quarter(src, "2023q4", rows)

    load_quarter(spark, src, "2023q4", lake_namespace)

    row = spark.table(table(lake_namespace, "sub")).where(F.col("adsh") == A1).first()
    assert row is not None and row["name"] == "  padded name  "


def test_sub_keeps_the_contract_columns_in_order_with_contract_types(
    spark: SparkSession, lake_namespace: str, src: Path
):
    write_quarter(src, "2023q4", basic_rows())

    load_quarter(spark, src, "2023q4", lake_namespace)
    sub = spark.table(table(lake_namespace, "sub"))

    assert sub.columns == [*RAW_SUB_COLUMNS, "src_quarter", "ingested_at"]
    dtypes = dict(sub.dtypes)
    assert dtypes["cik"] == "int" and dtypes["period"] == "int" and dtypes["filed"] == "int"
    assert dtypes["accepted"] == "timestamp" and dtypes["ingested_at"] == "timestamp"
    assert dtypes["fye"] == "string"  # '0930', not 930
    row = sub.where(F.col("adsh") == A1).first()
    assert row is not None
    assert row["fye"] == "0930" and row["period"] == 20230930 and row["filed"] == 20231103
    # Format inside Spark (session zone UTC): collect() would shift to the machine's local zone.
    # Note: the SEC's `accepted` is US Eastern wall-clock time, stored here as written.
    accepted = sub.where(F.col("adsh") == A1).select(
        F.date_format("accepted", "yyyy-MM-dd HH:mm:ss").alias("a")
    )
    assert accepted.first()["a"] == "2023-11-02 18:08:00"


def test_num_tag_and_pre_mirror_their_source_columns(
    spark: SparkSession, lake_namespace: str, src: Path
):
    write_quarter(src, "2023q4", basic_rows())

    load_quarter(spark, src, "2023q4", lake_namespace)

    for name in ("num", "tag", "pre"):
        cols = spark.table(table(lake_namespace, name)).columns
        assert cols == [*SOURCE_SCHEMAS[name].fieldNames(), "src_quarter", "ingested_at"]


def test_src_quarter_and_ingested_at_are_populated(
    spark: SparkSession, lake_namespace: str, src: Path
):
    write_quarter(src, "2023q4", basic_rows())

    load_quarter(spark, src, "2023q4", lake_namespace)

    for name in SOURCE_SCHEMAS:
        df = spark.table(table(lake_namespace, name))
        assert df.where(F.col("src_quarter") != "2023q4").count() == 0
        assert df.where(F.col("ingested_at").isNull()).count() == 0


def test_tables_are_partitioned_by_src_quarter(spark: SparkSession, lake_namespace: str, src: Path):
    write_quarter(src, "2023q3", basic_rows())
    write_quarter(src, "2023q4", basic_rows())
    load_quarter(spark, src, "2023q3", lake_namespace)
    load_quarter(spark, src, "2023q4", lake_namespace)

    parts = spark.sql(
        f"SELECT partition.src_quarter AS q FROM {table(lake_namespace, 'num')}.partitions"
    )

    assert sorted(r.q for r in parts.collect()) == ["2023q3", "2023q4"]


def test_rerunning_a_quarter_is_idempotent(spark: SparkSession, lake_namespace: str, src: Path):
    write_quarter(src, "2023q4", basic_rows())

    first = load_quarter(spark, src, "2023q4", lake_namespace)
    second = load_quarter(spark, src, "2023q4", lake_namespace)

    assert first == second
    assert spark.table(table(lake_namespace, "num")).count() == 3


def test_rerunning_a_quarter_replaces_only_that_quarter(
    spark: SparkSession, lake_namespace: str, src: Path
):
    write_quarter(src, "2023q3", basic_rows())
    write_quarter(src, "2023q4", basic_rows())
    load_quarter(spark, src, "2023q3", lake_namespace)
    load_quarter(spark, src, "2023q4", lake_namespace)
    # The SEC reissues 2023q4 with one fewer fact; q3 must be untouched.
    smaller = basic_rows()
    smaller["num"] = smaller["num"][:1]
    write_quarter(src, "2023q4", smaller)

    load_quarter(spark, src, "2023q4", lake_namespace)

    by_quarter = {
        r.src_quarter: r["count"]
        for r in spark.table(table(lake_namespace, "num")).groupBy("src_quarter").count().collect()
    }
    assert by_quarter == {"2023q3": 3, "2023q4": 1}


def test_header_that_does_not_match_the_schema_is_rejected(
    spark: SparkSession, lake_namespace: str, src: Path
):
    """If the SEC reorders or renames a column, fail instead of loading shifted data."""
    write_quarter(src, "2023q4", basic_rows())
    path = src / "2023q4" / "num.txt.gz"
    text = gzip.decompress(path.read_bytes()).decode()
    header, rest = text.split("\n", 1)
    swapped = header.replace("ddate\tqtrs", "qtrs\tddate")
    path.write_bytes(gzip.compress((swapped + "\n" + rest).encode()))

    with pytest.raises(Exception, match="(?i)header|ddate|qtrs"):
        load_quarter(spark, src, "2023q4", lake_namespace)


def test_a_malformed_number_fails_the_load(spark: SparkSession, lake_namespace: str, src: Path):
    rows = basic_rows()
    rows["num"].append(num_row(A1, tag="Bad", value="not-a-number"))
    write_quarter(src, "2023q4", rows)

    with pytest.raises(Exception, match="(?i)malformed|not-a-number|cast|parse"):
        load_quarter(spark, src, "2023q4", lake_namespace)


def test_a_row_with_too_many_columns_fails_the_load(
    spark: SparkSession, lake_namespace: str, src: Path
):
    """A stray tab inside a text field would shift columns; FAILFAST must catch it."""
    write_quarter(src, "2023q4", basic_rows())
    path = src / "2023q4" / "tag.txt.gz"
    text = gzip.decompress(path.read_bytes()).decode().rstrip("\n") + "\textra\n"
    path.write_bytes(gzip.compress(text.encode()))

    with pytest.raises(Exception, match="(?i)malformed|column|field"):
        load_quarter(spark, src, "2023q4", lake_namespace)


def test_an_unquoted_newline_inside_a_field_fails_the_load(
    spark: SparkSession, lake_namespace: str, src: Path
):
    """A newline in a text field splits the row; FAILFAST must refuse it."""
    rows = basic_rows()
    rows["sub"][0] = sub_row(A1, name="line one")
    write_quarter(src, "2023q4", rows)
    path = src / "2023q4" / "sub.txt.gz"
    text = gzip.decompress(path.read_bytes()).decode().replace("line one", "line one\nline two")
    path.write_bytes(gzip.compress(text.encode()))

    with pytest.raises(Exception, match="(?i)malformed"):
        load_quarter(spark, src, "2023q4", lake_namespace)


def test_a_blank_line_in_the_file_is_caught_by_the_row_count_check(
    spark: SparkSession, lake_namespace: str, src: Path
):
    """Spark silently skips blank lines, so only the line count notices the missing row."""
    write_quarter(src, "2023q4", basic_rows())
    path = src / "2023q4" / "num.txt.gz"
    lines = gzip.decompress(path.read_bytes()).decode().split("\n")
    lines.insert(2, "")  # a blank line between two data rows
    path.write_bytes(gzip.compress("\n".join(lines).encode()))

    with pytest.raises(RowCountMismatch, match="num 2023q4"):
        load_quarter(spark, src, "2023q4", lake_namespace)


def _rewrite(path: Path, edit) -> None:
    path.write_bytes(gzip.compress(edit(gzip.decompress(path.read_bytes()).decode()).encode()))


def test_a_short_sub_row_fails_even_though_sub_drops_most_columns(
    spark: SparkSession, lake_namespace: str, src: Path
):
    """raw.fsds_sub keeps 16 of 36 columns; with CSV column pruning on, Spark would parse only
    those 16 and load a 20-field row with NULLs. Whole rows must be validated."""
    write_quarter(src, "2023q4", basic_rows())
    _rewrite(
        src / "2023q4" / "sub.txt.gz",
        lambda text: (
            text.rstrip("\n") + "\n" + "\t".join(["0000000000-23-000009"] + ["x"] * 19) + "\n"
        ),
    )

    with pytest.raises(Exception, match="(?i)malformed"):
        load_quarter(spark, src, "2023q4", lake_namespace)


def test_a_sub_row_with_an_extra_trailing_field_fails(
    spark: SparkSession, lake_namespace: str, src: Path
):
    write_quarter(src, "2023q4", basic_rows())
    _rewrite(
        src / "2023q4" / "sub.txt.gz",
        lambda text: "\n".join(
            (ln + "\textra") if ln.startswith(A1) else ln for ln in text.split("\n")
        ),
    )

    with pytest.raises(Exception, match="(?i)malformed"):
        load_quarter(spark, src, "2023q4", lake_namespace)


def test_a_bad_value_in_a_dropped_sub_column_fails_the_load(
    spark: SparkSession, lake_namespace: str, src: Path
):
    """`nciks` is not kept in raw.fsds_sub, but it is still type-checked."""
    rows = basic_rows()
    rows["sub"][0] = sub_row(A1, nciks="oops")
    write_quarter(src, "2023q4", rows)

    with pytest.raises(Exception, match="(?i)malformed|oops|cast|parse"):
        load_quarter(spark, src, "2023q4", lake_namespace)


def test_header_drift_in_a_dropped_sub_column_is_rejected(
    spark: SparkSession, lake_namespace: str, src: Path
):
    """Renaming `former` to `formerx` moves no kept column, so only a full-header check sees it."""
    write_quarter(src, "2023q4", basic_rows())
    _rewrite(
        src / "2023q4" / "sub.txt.gz",
        lambda text: text.replace("\tformer\t", "\tformerx\t", 1),
    )

    with pytest.raises(HeaderMismatch, match="formerx"):
        load_quarter(spark, src, "2023q4", lake_namespace)


def test_an_added_header_column_is_rejected(spark: SparkSession, lake_namespace: str, src: Path):
    write_quarter(src, "2023q4", basic_rows())
    _rewrite(
        src / "2023q4" / "sub.txt.gz",
        lambda text: text.replace("\n", "\tnewcol\n", 1),
    )

    with pytest.raises(HeaderMismatch, match="newcol"):
        load_quarter(spark, src, "2023q4", lake_namespace)


def test_a_bad_file_leaves_the_existing_quarter_completely_untouched(
    spark: SparkSession, lake_namespace: str, src: Path
):
    """All four files are validated before anything is written: a broken `pre` must not leave
    new sub/num/tag next to old pre."""
    write_quarter(src, "2023q4", basic_rows())
    load_quarter(spark, src, "2023q4", lake_namespace)
    before = {n: spark.table(table(lake_namespace, n)).count() for n in SOURCE_SCHEMAS}
    bigger = basic_rows()
    bigger["num"].append(num_row(A1, tag="Extra"))
    bigger["sub"].append(sub_row("0000320193-23-000999"))
    write_quarter(src, "2023q4", bigger)
    (src / "2023q4" / "pre.txt.gz").unlink()

    with pytest.raises(FileNotFoundError):
        load_quarter(spark, src, "2023q4", lake_namespace)

    after = {n: spark.table(table(lake_namespace, n)).count() for n in SOURCE_SCHEMAS}
    assert after == before


def test_a_header_only_file_empties_that_quarter(
    spark: SparkSession, lake_namespace: str, src: Path
):
    """A file with no data rows must replace (clear) the quarter, not leave stale rows."""
    write_quarter(src, "2023q3", basic_rows())
    write_quarter(src, "2023q4", basic_rows())
    load_quarter(spark, src, "2023q3", lake_namespace)
    load_quarter(spark, src, "2023q4", lake_namespace)
    write_quarter(src, "2023q4", {**basic_rows(), "pre": []})

    counts = load_quarter(spark, src, "2023q4", lake_namespace)

    assert counts["pre"] == 0
    by_quarter = {
        r.src_quarter: r["count"]
        for r in spark.table(table(lake_namespace, "pre")).groupBy("src_quarter").count().collect()
    }
    assert by_quarter == {"2023q3": 2}


def test_a_file_without_a_trailing_newline_loads_and_counts_correctly(
    spark: SparkSession, lake_namespace: str, src: Path
):
    write_quarter(src, "2023q4", basic_rows())
    _rewrite(src / "2023q4" / "tag.txt.gz", lambda text: text.rstrip("\n"))

    counts = load_quarter(spark, src, "2023q4", lake_namespace)

    assert counts["tag"] == 2


def test_s3_sources_are_rejected_with_a_clear_message(capsys: pytest.CaptureFixture[str]):
    status = main(["--quarters", "2023q4", "--src", "s3://lake/raw/fsds"])

    assert status == 2
    assert "local" in capsys.readouterr().err


def test_missing_source_file_is_a_clear_error(spark: SparkSession, lake_namespace: str, src: Path):
    write_quarter(src, "2023q4", basic_rows())
    (src / "2023q4" / "pre.txt.gz").unlink()

    with pytest.raises(FileNotFoundError, match="make fsds"):
        load_quarter(spark, src, "2023q4", lake_namespace)


def test_count_source_rows_counts_lines_minus_header(tmp_path: Path):
    path = tmp_path / "x.txt.gz"
    path.write_bytes(gzip.compress(b"a\tb\n1\t2\n3\t4\n"))

    assert count_source_rows(path) == 2
    path.write_bytes(gzip.compress(b"a\tb\n"))
    assert count_source_rows(path) == 0
    path.write_bytes(gzip.compress(b"a\tb\n1\t2\n3\t4"))  # no final newline
    assert count_source_rows(path) == 2


def test_doubled_quotes_in_a_quoted_field_become_one_literal_quote(
    spark: SparkSession, lake_namespace: str, src: Path
):
    """Taken from the real 2023q3 pre.txt: `"Vacation ownership interest (""VOI"") inventory"`.

    The SEC quotes any field containing a quote and doubles the inner quotes, so the loader must
    unquote it (the runbook's first guess, no quote handling, would keep the quotes).
    """
    write_quarter(src, "2023q4", basic_rows())
    path = src / "2023q4" / "pre.txt.gz"
    raw_line = (
        f"{A1}\t2\t11\tBS\t0\tH\tInventory\tus-gaap/2023\t"
        '"Vacation ownership interest (""VOI"") inventory"\t0\n'
    )
    path.write_bytes(gzip.compress(gzip.decompress(path.read_bytes()) + raw_line.encode()))

    load_quarter(spark, src, "2023q4", lake_namespace)

    row = spark.table(table(lake_namespace, "pre")).where(F.col("line") == 11).first()
    assert row is not None
    assert row["plabel"] == 'Vacation ownership interest ("VOI") inventory'


def test_a_quoted_field_may_contain_a_tab(spark: SparkSession, lake_namespace: str, src: Path):
    """Taken from the real 2023q4 num.txt: a quoted `segments` value with an embedded tab.

    12 rows in that file would otherwise be split into 11 columns.
    """
    rows = basic_rows()
    segments = "InvestmentIdentifier=Ocular Therapeutix, Inc.,\tPharma-ceuticals, First Lien;"
    rows["num"].append(num_row(A1, tag="InvestmentOwnedAtCost", segments=segments))
    write_quarter(src, "2023q4", rows)

    counts = load_quarter(spark, src, "2023q4", lake_namespace)

    assert counts["num"] == 4  # the tab did not add a row or break the load
    row = (
        spark.table(table(lake_namespace, "num"))
        .where(F.col("tag") == "InvestmentOwnedAtCost")
        .first()
    )
    assert row is not None
    assert row["segments"] == segments
    assert row["value"] == Decimal("352583000000.0000")  # the value column did not shift


def test_ensure_table_survives_concurrent_first_creation(spark: SparkSession, lake_namespace: str):
    """Four threads creating the same new table: none may fail, and one table results."""
    import threading

    from pipelines.spark.iceberg_util import ensure_table

    df = spark.createDataFrame([("2023q4", 1)], "src_quarter string, n int")
    errors: list[BaseException] = []

    def work() -> None:
        try:
            ensure_table(spark, f"{lake_namespace}.race", df, "src_quarter")
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert errors == []
    assert spark.table(f"{lake_namespace}.race").count() == 0
