"""Integration tests for ``pipelines/spark/parse_filings.py`` (M02 section 3.4).

The mandatory check is the equivalence test: for a set of filings covering every join and
fallback, the rows in ``curated.filings`` / ``curated.chunks`` must equal what the pure-Python
``parse_document`` returns for the same bytes and an independently, hand-built ``FilingMeta``.
The Spark job may not add or change any parsing logic. The expected metas below are written out
by hand from the fixture tables, not produced by the code under test.
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from pyspark.sql import SparkSession

from pipelines.common.fiscal_ref import LabelOffset
from pipelines.parsing.document import FilingMeta, FilingRow, parse_document
from pipelines.parsing.pdf import PdfMeta, parse_pdf
from pipelines.spark.curated_schemas import CHUNKS, COMPANIES, FACTS_HISTORY, FILINGS
from pipelines.spark.parse_filings import run
from pipelines.spark.raw_schemas import FILINGS_HTML
from tests.raw_builders import INGESTED, sub_df

ACCEPTED = datetime(2023, 11, 2, 18, 8, 27, tzinfo=UTC)
ACCEPTED_TEXT = "2023-11-02 18:08:27"


def html(n: int, sections: tuple[str, ...] = ("1", "1A", "7", "8"), part: str = "") -> str:
    def para(topic: str) -> str:
        return "".join(
            f"<p>{topic} paragraph {i}: revenue, margins, demand and supply chains moved "
            f"during the period because of pricing, currency and volume.</p>"
            for i in range(n)
        )

    body = "".join(
        f'<div id="s{code}">Item {code}. Heading {code}</div>{para("Section " + code)}'
        for code in sections
    )
    table = (
        "<table><tr><td>In millions</td><td>2023</td><td>2022</td></tr>"
        "<tr><td>Net sales</td><td>$</td><td>383,285</td><td>$</td><td>394,328</td></tr>"
        "<tr><td>Net income</td><td>$</td><td>96,995</td><td>$</td><td>99,803</td></tr></table>"
    )
    return f"<html><body>{part}{body}{table}<div>SIGNATURES</div></body></html>"


def q_html(n: int) -> str:
    return html(n, ("1", "2"), part="<div>PART I</div>")


class Fixture:
    """One filing: its table rows, its object bytes and the hand-written expected FilingMeta."""

    def __init__(self, accession: str, meta: FilingMeta, content: bytes | None, text: bytes | None):
        self.accession, self.meta, self.content, self.text = accession, meta, content, text


def meta(accession: str, cik: int, form: str = "10-K", **kw: Any) -> FilingMeta:
    base: dict[str, Any] = {
        "accession": accession,
        "cik": cik,
        "form": form,
        "filed": date(2023, 11, 3),
        "primary_doc_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/x.htm",
    }
    return FilingMeta(**(base | kw))


@pytest.fixture
def lake(spark: SparkSession, lake_namespace: str, tmp_path: Path):
    """Tables and objects for eight filings; returns (namespace, object root, fixtures)."""
    ns = lake_namespace
    root = tmp_path / "raw" / "filings"
    fixtures: list[Fixture] = []

    def add(m: FilingMeta, text: str | None, *, raw: bytes | None = None, write: bool = True):
        path = root / str(m.cik) / f"{m.accession}.htm.gz"
        content = raw if raw is not None else (gzip.compress(text.encode()) if text else None)
        if write and content is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        m = m.model_copy(update={"html_s3_uri": path.resolve().as_uri()})
        fixtures.append(Fixture(m.accession, m, content, text.encode() if text else None))

    add(  # A: the 10-K; fye from this accession's sub row; sub.accepted carries a timestamp
        meta("0000320193-23-000106", 320193, period_of_report=date(2023, 9, 30), fye_mmdd="0930",
             accepted_at=ACCEPTED, label_offset=LabelOffset(0, True), sub_fy=2023, sub_fp="FY"),
        html(40),
    )  # fmt: skip
    add(  # B: a 10-Q of the same company
        meta("0000320193-23-000077", 320193, "10-Q", period_of_report=date(2023, 7, 1),
             fye_mmdd="0930", label_offset=LabelOffset(0, True), sub_fy=2023, sub_fp="Q3"),
        q_html(6),
    )  # fmt: skip
    add(  # C: a January retailer named for the year it began: offset -1, from facts-bearing subs
        meta("0001000002-24-000005", 1000002, period_of_report=date(2024, 2, 3), fye_mmdd="0131",
             label_offset=LabelOffset(-1, True), sub_fy=2023, sub_fp="FY"),
        html(5),
    )  # fmt: skip
    add(  # D: no sub row, no company row: labels cannot be derived and there is no fallback
        meta("0000999001-23-000001", 999001, period_of_report=date(2023, 12, 31)),
        html(4),
    )  # fmt: skip
    add(  # E: no report_date in raw.filings_html; the period comes from sub.period
        meta("0001000003-24-000001", 1000003, period_of_report=date(2023, 12, 31),
             fye_mmdd="1231", label_offset=LabelOffset(0, True), sub_fy=2023, sub_fp="FY"),
        html(4),
    )  # fmt: skip
    add(  # F: no sub row; fye comes from curated.companies (and is flagged)
        meta("0001000004-22-000001", 1000004, period_of_report=date(2022, 6, 30),
             fye_mmdd="0630", fye_from_companies=True),
        html(4),
    )  # fmt: skip
    add(meta("0000320193-23-000999", 320193, "10-Q", period_of_report=date(2023, 7, 1),
             fye_mmdd="0930", label_offset=LabelOffset(0, True)),
        None, raw=b"this is not gzip")  # fmt: skip  # G: corrupt object
    add(meta("0000320193-23-000998", 320193, "10-Q", period_of_report=date(2023, 7, 1),
             fye_mmdd="0930", label_offset=LabelOffset(0, True)),
        None, write=False)  # fmt: skip  # H: row says 200 but the object is gone

    def sub(adsh: str, cik: int, form: str, fye: str, period: int, fy: int, fp: str, **kw: Any):
        return {"adsh": adsh, "cik": cik, "form": form, "fye": fye, "period": period, "fy": fy,
                "fp": fp, "accepted": None, **kw}  # fmt: skip

    subs = [
        sub(
            "0000320193-23-000106", 320193, "10-K", "0930", 20230930, 2023, "FY", accepted=ACCEPTED
        ),
        sub("0000320193-23-000077", 320193, "10-Q", "0930", 20230701, 2023, "Q3"),
        sub("0001000002-24-000005", 1000002, "10-K", "0131", 20240203, 2023, "FY"),
        sub("0001000002-23-000004", 1000002, "10-K", "0131", 20230128, 2022, "FY"),
        # a later 10-K with a different label but NO facts: it must not move the offset
        sub("0001000002-25-000009", 1000002, "10-K", "0131", 20250201, 2025, "FY"),
        sub("0001000003-24-000001", 1000003, "10-K", "1231", 20231231, 2023, "FY"),
        sub("0000320193-23-000999", 320193, "10-Q", "0930", 20230701, 2023, "Q3"),
        sub("0000320193-23-000998", 320193, "10-Q", "0930", 20230701, 2023, "Q3"),
    ]
    spark.createDataFrame(  # sub rows via the shared builder, with `accepted` set per row
        sub_df(spark, subs).rdd, sub_df(spark, subs).schema
    ).writeTo(f"{ns}.fsds_sub").using("iceberg").create()

    spark.createDataFrame(
        [(1000004, "Fye From Companies", "FFC", None, "0630", False, None, [])], COMPANIES
    ).writeTo(f"{ns}.companies").using("iceberg").create()

    facts_accessions = [  # the 10-Ks the offset is computed over (not 0001000002-25-000009)
        "0000320193-23-000106",
        "0001000002-24-000005",
        "0001000002-23-000004",
        "0001000003-24-000001",
    ]
    spark.createDataFrame(
        [
            tuple(a if f.name == "accession" else None for f in FACTS_HISTORY.fields)
            for a in facts_accessions
        ],
        FACTS_HISTORY,
    ).writeTo(f"{ns}.facts_history").using("iceberg").create()

    def html_row(f: Fixture):
        m = f.meta
        report = None if m.accession == "0001000003-24-000001" else m.period_of_report
        return (m.accession, m.cik, m.form, m.filed, report, m.primary_doc_url, m.html_s3_uri,
                100, "ab" * 32, 200, INGESTED)  # fmt: skip

    spark.createDataFrame([html_row(f) for f in fixtures], FILINGS_HTML).writeTo(
        f"{ns}.filings_html"
    ).using("iceberg").create()
    return ns, root, fixtures


def spark_rows(spark: SparkSession, ns: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    cols = [c for c in spark.table(f"{ns}.filings").columns if c != "accepted_at"]
    filings = {
        r["accession"]: r
        for r in (
            x.asDict()
            for x in spark.table(f"{ns}.filings")
            .selectExpr(*cols, "date_format(accepted_at, 'yyyy-MM-dd HH:mm:ss') AS accepted_at")
            .collect()
        )
    }
    chunks = {r.chunk_id: r.asDict() for r in spark.table(f"{ns}.chunks").collect()}
    return filings, chunks


def expected(fixtures: list[Fixture]):
    filings: dict[str, dict[str, Any]] = {}
    chunks: dict[str, dict[str, Any]] = {}
    for f in fixtures:
        if f.content is None:
            row, rows = _error(f.meta, f"object missing: {f.meta.html_s3_uri}")
        elif f.text is None:
            row, rows = _error(f.meta, None)
        else:
            row, rows = parse_document(f.text, f.meta)
        d = row.model_dump()
        d["accepted_at"] = ACCEPTED_TEXT if f.meta.accepted_at else None
        filings[f.accession] = d
        chunks.update({c.chunk_id: c.model_dump() for c in rows})
    return filings, chunks


def _error(m: FilingMeta, message: str | None):
    from pipelines.parsing.document import error_result

    return error_result(m, message or "")


def test_the_spark_output_equals_the_pure_python_parse_for_every_filing(spark: SparkSession, lake):
    ns, root, fixtures = lake

    counts = run(spark, str(root), raw_ns=ns, curated_ns=ns)

    got_filings, got_chunks = spark_rows(spark, ns)
    want_filings, want_chunks = expected(fixtures)
    for acc, want in want_filings.items():
        if want["parse_status"] == "error" and acc.endswith("-000999"):
            want["parse_error"] = got_filings[acc]["parse_error"]  # the gzip message is the OS's
            assert got_filings[acc]["parse_error"].startswith("BadGzipFile")
        assert got_filings[acc] == want, acc
    assert got_filings.keys() == want_filings.keys()
    assert got_chunks == want_chunks
    assert counts == {"filings": len(fixtures), "chunks": len(want_chunks)}


def test_label_offset_and_fye_joins_have_the_intended_effect(spark: SparkSession, lake):
    ns, root, _ = lake

    run(spark, str(root), raw_ns=ns, curated_ns=ns)

    got, _ = spark_rows(spark, ns)
    by = {a[-9:]: r for a, r in got.items()}
    assert (by["24-000005"]["fiscal_year"], by["24-000005"]["fiscal_period"]) == (2023, "FY")
    assert (
        by["24-000005"]["dq_flags"] == []
    )  # offset -1 is constant: the unused 2025 sub is ignored
    assert (by["23-000999"]["parse_status"], by["23-000998"]["parse_status"]) == ("error", "error")
    assert by["23-000001"]["fiscal_year"] is None
    assert by["23-000001"]["dq_flags"] == ["fiscal_unresolved"]
    assert by["22-000001"]["dq_flags"] == ["label_offset_default", "fye_from_companies"]
    assert by["24-000001"]["period_of_report"] == date(2023, 12, 31)


def test_every_chunk_is_within_400_tokens_and_inside_one_section(spark: SparkSession, lake):
    ns, root, _ = lake

    run(spark, str(root), raw_ns=ns, curated_ns=ns)

    _, chunks = spark_rows(spark, ns)
    assert chunks and all(c["n_tokens"] <= 400 for c in chunks.values())
    assert all(c["chunk_id"].split("#")[1] == c["section"] for c in chunks.values())


def test_a_rerun_changes_nothing(spark: SparkSession, lake):
    ns, root, _ = lake
    run(spark, str(root), raw_ns=ns, curated_ns=ns)
    first = spark_rows(spark, ns)

    counts = run(spark, str(root), raw_ns=ns, curated_ns=ns)

    assert spark_rows(spark, ns) == first
    assert counts["filings"] == 8


def test_a_reparse_that_yields_fewer_chunks_leaves_no_orphans(spark: SparkSession, lake):
    ns, root, fixtures = lake
    run(spark, str(root), raw_ns=ns, curated_ns=ns)
    _, before = spark_rows(spark, ns)
    target = fixtures[0]
    (root / "320193" / f"{target.accession}.htm.gz").write_bytes(gzip.compress(html(2).encode()))

    run(spark, str(root), raw_ns=ns, curated_ns=ns, accessions=[target.accession])

    _, after = spark_rows(spark, ns)
    mine_before = {k for k, c in before.items() if c["accession"] == target.accession}
    mine_after = {k for k, c in after.items() if c["accession"] == target.accession}
    assert 0 < len(mine_after) < len(mine_before)
    assert {k: c for k, c in after.items() if k not in mine_after} == {
        k: c for k, c in before.items() if k not in mine_before
    }
    assert {c["n_tokens"] for c in after.values()} <= set(range(1, 401))


def test_a_changed_fiscal_year_moves_the_filing_without_leaving_a_duplicate(
    spark: SparkSession, lake
):
    ns, root, _ = lake
    run(spark, str(root), raw_ns=ns, curated_ns=ns)  # filing D has no fiscal labels
    spark.createDataFrame(
        [(999001, "Late Company", "LATE", None, "1231", False, None, [])], COMPANIES
    ).writeTo(f"{ns}.companies").append()

    run(spark, str(root), raw_ns=ns, curated_ns=ns, accessions=["0000999001-23-000001"])

    rows = spark.table(f"{ns}.filings").where("accession = '0000999001-23-000001'").collect()
    assert [(r.fiscal_year, r.fiscal_period) for r in rows] == [(2023, "FY")]
    assert (
        spark.table(f"{ns}.chunks")
        .where("accession = '0000999001-23-000001'")
        .where("fiscal_year IS NULL")
        .count()
        == 0
    )


def test_the_schemas_match_the_contract_column_order_and_types():
    assert [(f.name, f.dataType.simpleString()) for f in FILINGS.fields] == [
        ("accession", "string"), ("cik", "int"), ("form", "string"), ("filed", "date"),
        ("accepted_at", "timestamp"), ("period_of_report", "date"), ("fiscal_year", "int"),
        ("fiscal_period", "string"), ("primary_doc_url", "string"), ("html_s3_uri", "string"),
        ("parse_status", "string"), ("parse_error", "string"), ("n_sections", "int"),
        ("n_chunks", "int"), ("doc_source", "string"), ("dq_flags", "array<string>"),
    ]  # fmt: skip
    assert [(f.name, f.dataType.simpleString()) for f in CHUNKS.fields] == [
        ("chunk_id", "string"), ("cik", "int"), ("accession", "string"), ("form", "string"),
        ("fiscal_year", "int"), ("fiscal_period", "string"), ("section", "string"),
        ("section_title", "string"), ("chunk_index", "int"), ("text", "string"),
        ("n_tokens", "int"), ("is_table", "boolean"), ("source_url", "string"),
        ("anchor", "string"), ("page", "int"), ("doc_source", "string"), ("corpus_version", "int"),
    ]  # fmt: skip
    assert [f.name for f in FILINGS.fields] == list(FilingRow.model_fields)


# --------------------------------------------------------------------------------------
# FinanceBench PDFs
# --------------------------------------------------------------------------------------


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 780), text, fontsize=9)
    return doc.tobytes()


@pytest.fixture
def financebench(tmp_path: Path):
    base = tmp_path / "fb"
    (base / "pdfs").mkdir(parents=True)
    pdfs = {
        "3M_2018_10K": make_pdf(["Net sales were 32,765 million.", "Risk factors follow here."]),
        "ACME_2022_10K": make_pdf(["Acme revenue grew."]),
    }
    for name, data in pdfs.items():
        (base / "pdfs" / f"{name}.pdf").write_bytes(data)
    rows = [
        {"company": "3M", "doc_name": "3M_2018_10K", "question": "q"},
        {"company": "Acme", "doc_name": "ACME_2022_10K", "question": "q"},
    ]
    (base / "financebench_open_source.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )
    return base, pdfs


def test_financebench_pdfs_equal_the_pure_python_parse_and_a_missing_cik_is_an_error_row(
    spark: SparkSession, lake, financebench, tmp_path: Path
):
    ns, root, _ = lake
    base, pdfs = financebench
    csv_path = tmp_path / "financebench_companies.csv"
    csv_path.write_text("company,cik\n3M,66740\n")  # Acme deliberately absent

    run(spark, str(root), raw_ns=ns, curated_ns=ns, financebench_dir=base,
        financebench_companies=csv_path)  # fmt: skip

    got_f, got_c = spark_rows(spark, ns)
    for name, cik in (("3M_2018_10K", 66740), ("ACME_2022_10K", None)):
        url = f"https://raw.githubusercontent.com/patronus-ai/financebench/main/pdfs/{name}.pdf"
        want_row, want_chunks = parse_pdf(
            pdfs[name], PdfMeta(doc_name=name, cik=cik, source_url=url)
        )
        want = want_row.model_dump() | {"accepted_at": None}
        assert got_f[f"fb:{name}"] == want
        for c in want_chunks:
            assert got_c[c.chunk_id] == c.model_dump()
    assert got_f["fb:ACME_2022_10K"]["parse_status"] == "error"
    assert "financebench_companies.csv" in got_f["fb:ACME_2022_10K"]["parse_error"]
    assert {c["page"] for c in got_c.values() if c["doc_source"] == "financebench_pdf"} == {1, 2}


def test_a_relative_financebench_directory_works_like_the_cli_default(
    spark: SparkSession, lake, financebench, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`make parse-filings FB=1` passes a relative `data/raw/financebench`: both `Path.as_uri` and
    Spark's own working directory need it made absolute."""
    ns, root, _ = lake
    base, _ = financebench
    csv_path = tmp_path / "financebench_companies.csv"
    csv_path.write_text("company,cik\n3M,66740\nAcme,1\n")
    monkeypatch.chdir(base.parent)

    run(spark, str(root), raw_ns=ns, curated_ns=ns, financebench_dir=Path(base.name),
        financebench_companies=csv_path)  # fmt: skip

    pdf_rows = spark.table(f"{ns}.filings").where("doc_source = 'financebench_pdf'").collect()
    assert sorted((r.accession, r.parse_status) for r in pdf_rows) == [
        ("fb:3M_2018_10K", "ok"),
        ("fb:ACME_2022_10K", "ok"),
    ]


def test_without_the_owners_company_file_the_pdfs_are_reported_not_parsed(
    spark: SparkSession, lake, financebench, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    ns, root, fixtures = lake
    base, _ = financebench

    with caplog.at_level(logging.WARNING):
        run(spark, str(root), raw_ns=ns, curated_ns=ns, financebench_dir=base,
            financebench_companies=tmp_path / "does_not_exist.csv")  # fmt: skip

    assert "FinanceBench PDFs NOT parsed" in caplog.text
    assert spark.table(f"{ns}.filings").count() == len(fixtures)
    assert spark.table(f"{ns}.chunks").where("doc_source = 'financebench_pdf'").count() == 0
