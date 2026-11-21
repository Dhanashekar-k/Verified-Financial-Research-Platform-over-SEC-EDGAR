"""Parse stored filings and FinanceBench PDFs into `curated.filings` and `curated.chunks` (M02 3.4).

    uv run python -m pipelines.spark.parse_filings --env local
        [--financebench-dir data/raw/financebench]
        [--financebench-companies data/financebench_companies.csv]

Inputs: `raw.filings_html` (HTTP 200 rows; the gzipped HTML objects it points at), `raw.fsds_sub`
(fye, fy, fp, period, accepted of the same accession), `curated.companies` (fallback fye) and
`curated.facts_history` (which accessions have facts: the label offset is computed over exactly
the submissions `normalize_facts` uses, so text and facts get the same labels, ADR-023).

The Spark side adds NO parsing logic: each row is `FilingMeta` JSON plus the object's bytes, a
`mapInPandas` calls `pipelines.parsing.parse_gz_document`, and the result comes back as JSON. The
Spark-vs-pure-Python equivalence test (`tests/integration/test_parse_filings.py`) holds it to that.
One bad document is a row with `parse_status='error'`, never a failed job; a filing whose object is
missing is an error row too, so there is one `curated.filings` row per fetched filing.

Writes are idempotent. `curated.filings` is merged on `accession` (the runbook says overwrite by
fiscal_year, but a re-parse can move a filing to another fiscal_year and leave a stale row behind).
`curated.chunks` is merged on `chunk_id` and then loses any chunk of a re-parsed document that the
new parse no longer produces.

Smoke runs (AWS Glue): `--output-namespace lake.corpus_smoke` writes `filings`/`chunks` there while
the read-only inputs (`raw.fsds_sub`, `curated.facts_history`) still come from the real namespaces,
`--filings-html-namespace` points at a smoke `filings_html`, and `--ciks`, `--filed-from`,
`--filed-to`, `--limit-filings` pick a few filings. A missing `curated.companies` only drops the
fye fallback (a warning), it does not stop the run.

FinanceBench PDFs need the owner's `data/financebench_companies.csv` (company,cik). Without it that
part is reported as not run; a CIK is never guessed.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sys
import uuid
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path

import pandas as pd
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import ArrayType, StringType, StructField, StructType

from pipelines.common import locations
from pipelines.common.fiscal_ref import SubmissionPeriod, label_offsets
from pipelines.common.locations import Location
from pipelines.common.spark_session import Env, spark_session
from pipelines.parsing.document import FilingMeta, parse_gz_document
from pipelines.parsing.pdf import PdfMeta, parse_pdf
from pipelines.spark.curated_schemas import CHUNKS, FILINGS
from pipelines.spark.iceberg_util import ensure_table
from pipelines.spark.normalize_facts import blank, yyyymmdd

log = logging.getLogger(__name__)

RAW_NAMESPACE = "lake.raw"
CURATED_NAMESPACE = "lake.curated"
ANNUAL_SUB_FORMS = ("10-K", "10-KT")
PARSED_SCHEMA = StructType(
    [StructField("filing_json", StringType()), StructField("chunks_json", StringType())]
)
ROW_SCHEMA = StructType([StructField("filing", FILINGS), StructField("chunks", ArrayType(CHUNKS))])


def normalized_path(column: str) -> F.Column:
    """Compare `file:///a%20b` and `file:/a b`, `s3a://` and `s3://` as the same object."""
    decoded = F.expr(f"url_decode({column})")
    return F.regexp_replace(F.regexp_replace(decoded, "^file:/+", "/"), "^s3[an]://", "s3://")


# -- label offsets ----------------------------------------------------------------------


def label_offset_frame(spark: SparkSession, raw_ns: str, curated_ns: str) -> DataFrame:
    """(cik, label_offset, offset_constant) from the same submissions `normalize_facts` uses."""
    sub = spark.table(f"{raw_ns}.fsds_sub")
    latest = sub.withColumn(
        "_rn",
        F.row_number().over(_latest_window()),
    ).where(F.col("_rn") == 1)
    annual = latest.where(
        F.col("form").isin(*ANNUAL_SUB_FORMS) & F.col("fy").isNotNull() & ~blank("fye")
    )
    if spark.catalog.tableExists(f"{curated_ns}.facts_history"):
        with_facts = spark.table(f"{curated_ns}.facts_history").select("accession").distinct()
        annual = annual.join(with_facts, annual["adsh"] == with_facts["accession"], "left_semi")
    else:
        log.warning(
            "%s.facts_history is missing: label offsets use every 10-K submission", curated_ns
        )
    rows = annual.select(
        "cik", "adsh", "form", F.col("fy").alias("sub_fy"),
        yyyymmdd("period").alias("sub_period"), F.col("fye").alias("fye_mmdd"),
    ).collect()  # fmt: skip
    subs = [
        SubmissionPeriod(r.cik, r.adsh, r.form, r.sub_fy, r.sub_period, r.fye_mmdd)
        for r in rows
        if r.sub_period is not None
    ]
    offsets = [(cik, lo.offset, lo.is_constant) for cik, lo in sorted(label_offsets(subs).items())]
    return spark.createDataFrame(offsets, "cik int, label_offset int, offset_constant boolean")


def _latest_window():  # type: ignore[no-untyped-def]
    from pyspark.sql import Window

    return Window.partitionBy("adsh").orderBy(
        F.col("src_quarter").desc(), F.col("ingested_at").desc()
    )


# -- inputs -----------------------------------------------------------------------------


def select_filings(
    html: DataFrame,
    accessions: list[str] | None = None,
    ciks: Sequence[int] = (),
    filed_from: date | None = None,
    filed_to: date | None = None,
    limit: int | None = None,
) -> DataFrame:
    """Restrict `filings_html` rows. Every criterion narrows; none means everything."""
    if accessions is not None:
        html = html.where(F.col("accession").isin(accessions))
    if ciks:
        html = html.where(F.col("cik").isin([int(c) for c in ciks]))
    if filed_from is not None:
        html = html.where(F.col("filed") >= F.lit(filed_from))
    if filed_to is not None:
        html = html.where(F.col("filed") <= F.lit(filed_to))
    if limit is not None:
        first = html.select("accession").distinct().orderBy("accession").limit(limit)
        html = html.join(first, "accession", "left_semi")
    return html


def companies_fye(spark: SparkSession, curated_ns: str) -> DataFrame:
    """(cik, co_fye) from `curated.companies`; empty, with a warning, when that table is absent.

    It only backs up a submission with no `fye` of its own, so its absence loses that fallback and
    nothing else (rows that needed it have `fye_mmdd` NULL, as for any company without a row).
    """
    table = f"{curated_ns}.companies"
    if spark.catalog.tableExists(table):
        return spark.table(table).select("cik", F.col("fye_mmdd").alias("co_fye"))
    log.warning("%s is missing: no company fiscal-year-end fallback", table)
    return spark.createDataFrame([], "cik int, co_fye string")


def build_html_inputs(
    spark: SparkSession,
    raw_ns: str,
    curated_ns: str,
    html_glob: str,
    corpus_version: int,
    accessions: list[str] | None = None,
    filings_html_ns: str | None = None,
    ciks: Sequence[int] = (),
    filed_from: date | None = None,
    filed_to: date | None = None,
    limit_filings: int | None = None,
) -> DataFrame:
    """One row per fetched filing: `meta_json` and the object's bytes (NULL if it is missing).

    `filings_html_ns` (default `raw_ns`) is where `filings_html` lives; the other selection
    arguments restrict which fetched filings are parsed (all of them when empty).
    """
    html = spark.table(f"{filings_html_ns or raw_ns}.filings_html").where(
        (F.col("http_status") == 200) & F.col("s3_uri").isNotNull()
    )
    html = select_filings(html, accessions, ciks, filed_from, filed_to, limit_filings)
    sub = (
        spark.table(f"{raw_ns}.fsds_sub")
        .withColumn("_rn", F.row_number().over(_latest_window()))
        .where(F.col("_rn") == 1)
        .select(
            F.col("adsh").alias("accession"),
            F.col("fye").alias("sub_fye"),
            F.col("fy").alias("sub_fy"),
            F.col("fp").alias("sub_fp"),
            yyyymmdd("period").alias("sub_period"),
            F.col("accepted").alias("accepted_at"),
        )
    )
    companies = companies_fye(spark, curated_ns)
    offsets = label_offset_frame(spark, raw_ns, curated_ns)
    objects = (
        spark.read.format("binaryFile")
        .option("pathGlobFilter", "*.htm.gz")
        .option("recursiveFileLookup", "true")
        .load(html_glob)
        .select(normalized_path("path").alias("object_path"), "content")
    )
    joined = (
        html.join(sub, "accession", "left")
        .join(companies, "cik", "left")
        .join(F.broadcast(offsets), "cik", "left")
        .withColumn("object_path", normalized_path("s3_uri"))
        .join(objects, "object_path", "left")
    )
    sub_fye_blank = blank("sub_fye")
    fye = F.when(~sub_fye_blank, F.col("sub_fye")).when(~blank("co_fye"), F.col("co_fye"))
    meta = F.to_json(
        F.struct(
            "accession",
            "cik",
            "form",
            "filed",
            "accepted_at",
            F.coalesce("report_date", "sub_period").alias("period_of_report"),
            F.col("url").alias("primary_doc_url"),
            F.col("s3_uri").alias("html_s3_uri"),
            fye.alias("fye_mmdd"),
            (sub_fye_blank & ~blank("co_fye")).alias("fye_from_companies"),
            F.when(
                F.col("label_offset").isNotNull(),
                F.struct(
                    F.col("label_offset").alias("offset"),
                    F.col("offset_constant").alias("is_constant"),
                ),
            ).alias("label_offset"),
            "sub_fy",
            "sub_fp",
            F.lit(corpus_version).alias("corpus_version"),
        )  # fmt: skip
    )
    return joined.select(meta.alias("meta_json"), "content")


def spread_for_parsing(inputs: DataFrame, partitions: int | None) -> DataFrame:
    """Spread documents over `partitions` tasks before the CPU-bound parse.

    Spark packs `binaryFile` objects into partitions by BYTES (128 MB), and parse time depends on
    document structure, not size, so a few big partitions leave most cores idle. A random
    `repartition` evens that out; it costs one shuffle of the compressed bytes.
    """
    return inputs.repartition(partitions) if partitions else inputs


def read_financebench_companies(path: Location) -> dict[str, int]:
    """company -> cik from the owner's CSV (columns `company,cik`); a local path or `s3://`."""
    reader = csv.DictReader(io.StringIO(locations.read_text(path), newline=""))
    if not reader.fieldnames or not {"company", "cik"} <= set(reader.fieldnames):
        raise ValueError(f"{path} must have the columns company,cik; has {reader.fieldnames}")
    return {r["company"].strip(): int(r["cik"]) for r in reader if r["cik"].strip()}


def _pdf_uri(pdf_dir: Location, doc_name: str) -> str:
    """The object a PDF row will be joined to: a `file:` URI locally, the `s3://` URI on AWS."""
    if locations.is_s3(pdf_dir):
        return locations.join(pdf_dir, "pdfs", f"{doc_name}.pdf")
    return (Path(pdf_dir) / "pdfs" / f"{doc_name}.pdf").resolve().as_uri()


def build_pdf_inputs(
    spark: SparkSession, pdf_dir: Location, companies: dict[str, int], corpus_version: int
) -> DataFrame:
    """One row per cited FinanceBench PDF; a company with no CIK gets `cik: null` (an error row).

    `pdf_dir` holds `financebench_open_source.jsonl` and `pdfs/`; it is a local directory or an
    `s3://` prefix (the PDFs are read by Spark's `binaryFile` source either way).
    """
    questions = [
        json.loads(line)
        for line in locations.read_text(
            locations.join(pdf_dir, "financebench_open_source.jsonl")
        ).splitlines()
        if line.strip()
    ]
    company_of = {q["doc_name"]: q["company"] for q in questions}
    rows = []
    for doc_name, company in sorted(company_of.items()):
        meta = PdfMeta(
            doc_name=doc_name,
            cik=companies.get(company),
            source_url=(
                "https://raw.githubusercontent.com/patronus-ai/financebench/main/pdfs/"
                f"{doc_name}.pdf"
            ),
            corpus_version=corpus_version,
        )
        rows.append((meta.model_dump_json(), _pdf_uri(pdf_dir, doc_name)))
    meta_df = spark.createDataFrame(rows, "meta_json string, uri string")
    pdf_glob = (
        locations.join(pdf_dir, "pdfs")
        if locations.is_s3(pdf_dir)
        else str((Path(pdf_dir) / "pdfs").resolve())  # Spark resolves a relative path in its cwd
    )
    objects = (
        spark.read.format("binaryFile")
        .option("pathGlobFilter", "*.pdf")
        .load(pdf_glob)
        .select(normalized_path("path").alias("object_path"), "content")
    )
    return (
        meta_df.withColumn("object_path", normalized_path("uri"))
        .join(objects, "object_path", "left")
        .select("meta_json", "content")
    )


# -- the per-document step (the only Python that runs on workers) ------------------------


def parse_html_batches(batches: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
    for frame in batches:
        filings, chunks = [], []
        for meta_json, content in zip(frame["meta_json"], frame["content"], strict=True):
            meta = FilingMeta.model_validate_json(meta_json)
            filing, rows = parse_gz_document(None if content is None else bytes(content), meta)
            filings.append(filing.model_dump_json())
            chunks.append("[" + ",".join(c.model_dump_json() for c in rows) + "]")
        yield pd.DataFrame({"filing_json": filings, "chunks_json": chunks})


def parse_pdf_batches(batches: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
    for frame in batches:
        filings, chunks = [], []
        for meta_json, content in zip(frame["meta_json"], frame["content"], strict=True):
            meta = PdfMeta.model_validate_json(meta_json)
            if content is None:
                from pipelines.parsing.pdf import missing_pdf_result

                filing, rows = missing_pdf_result(meta)
            else:
                filing, rows = parse_pdf(bytes(content), meta)
            filings.append(filing.model_dump_json())
            chunks.append("[" + ",".join(c.model_dump_json() for c in rows) + "]")
        yield pd.DataFrame({"filing_json": filings, "chunks_json": chunks})


def parse_to_rows(inputs: DataFrame, pdf: bool = False) -> DataFrame:
    parsed = inputs.mapInPandas(parse_pdf_batches if pdf else parse_html_batches, PARSED_SCHEMA)
    return parsed.select(
        F.from_json("filing_json", FILINGS).alias("filing"),
        F.from_json("chunks_json", ArrayType(CHUNKS)).alias("chunks"),
    )


# -- writing ----------------------------------------------------------------------------


def merge_curated(spark: SparkSession, curated_ns: str, parsed: DataFrame) -> dict[str, int]:
    """MERGE filings on accession and chunks on chunk_id; drop orphaned chunks of re-parsed docs."""
    filings = parsed.select("filing.*")
    chunks = parsed.select(F.explode("chunks").alias("c")).select("c.*")
    filings_table, chunks_table = f"{curated_ns}.filings", f"{curated_ns}.chunks"
    ensure_table(spark, filings_table, filings, "fiscal_year")
    ensure_table(spark, chunks_table, chunks, "doc_source, fiscal_year")
    tag = uuid.uuid4().hex
    f_view, c_view = f"_filings_{tag}", f"_chunks_{tag}"
    filings.createOrReplaceTempView(f_view)
    chunks.createOrReplaceTempView(c_view)
    try:
        spark.sql(
            f"MERGE INTO {filings_table} t USING {f_view} s ON t.accession = s.accession "
            "WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *"
        )
        spark.sql(
            f"MERGE INTO {chunks_table} t USING {c_view} s ON t.chunk_id = s.chunk_id "
            "WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *"
        )
        spark.sql(
            f"DELETE FROM {chunks_table} WHERE accession IN (SELECT accession FROM {f_view}) "
            f"AND chunk_id NOT IN (SELECT chunk_id FROM {c_view})"
        )
    finally:
        spark.catalog.dropTempView(f_view)
        spark.catalog.dropTempView(c_view)
    return {
        "filings": spark.table(filings_table).count(),
        "chunks": spark.table(chunks_table).count(),
    }


def run(
    spark: SparkSession,
    html_glob: str,
    raw_ns: str = RAW_NAMESPACE,
    curated_ns: str = CURATED_NAMESPACE,
    corpus_version: int = 1,
    financebench_dir: Path | None = None,
    financebench_companies: Path | None = None,
    accessions: list[str] | None = None,
    output_ns: str | None = None,
    filings_html_ns: str | None = None,
    ciks: Sequence[int] = (),
    filed_from: date | None = None,
    filed_to: date | None = None,
    limit_filings: int | None = None,
    repartition: int | None = None,
    skip_html: bool = False,
) -> dict[str, int]:
    """Parse and merge. Reads come from `raw_ns`/`curated_ns`; writes go to `output_ns`
    (default `curated_ns`), so a smoke run can read the real tables and write somewhere else.
    `skip_html` parses only the FinanceBench PDFs (it needs `financebench_dir`)."""
    output_ns = output_ns or curated_ns
    if skip_html and financebench_dir is None:
        raise ValueError("--skip-html needs --financebench-dir: there would be nothing to parse")
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {output_ns}")
    html_rows = (
        None
        if skip_html
        else parse_to_rows(
            spread_for_parsing(
                build_html_inputs(
                    spark, raw_ns, curated_ns, html_glob, corpus_version, accessions,
                    filings_html_ns, ciks, filed_from, filed_to, limit_filings,
                ),
                repartition,
            )
        )
    )  # fmt: skip
    counts: dict[str, int] = {}
    parsed = html_rows
    if financebench_dir is not None:
        if financebench_companies is None or not locations.exists(financebench_companies):
            log.warning(
                "FinanceBench PDFs NOT parsed: %s is missing (the owner writes company,cik)",
                financebench_companies,
            )
            if skip_html:
                raise FileNotFoundError(f"{financebench_companies} is missing; nothing to parse")
        else:
            companies = read_financebench_companies(financebench_companies)
            pdf_rows = parse_to_rows(
                build_pdf_inputs(spark, financebench_dir, companies, corpus_version), pdf=True
            )
            parsed = pdf_rows if html_rows is None else html_rows.unionByName(pdf_rows)
    assert parsed is not None
    parsed = parsed.persist()
    try:
        counts = merge_curated(spark, output_ns, parsed)
    finally:
        parsed.unpersist()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse filings into curated.filings and chunks.")
    parser.add_argument("--env", choices=["local", "emr", "glue"], default="local")
    parser.add_argument("--html-glob", default="data/raw/filings")
    parser.add_argument("--raw-namespace", default=RAW_NAMESPACE)
    parser.add_argument("--curated-namespace", default=CURATED_NAMESPACE)
    parser.add_argument("--output-namespace", default=None, help="write here (default: curated)")
    parser.add_argument("--filings-html-namespace", default=None, help="default: raw namespace")
    parser.add_argument("--ciks", default="", help="comma-separated CIKs (smoke subset)")
    parser.add_argument("--filed-from", type=date.fromisoformat, default=None)
    parser.add_argument("--filed-to", type=date.fromisoformat, default=None)
    parser.add_argument("--limit-filings", type=int, default=None, help="first N accessions")
    parser.add_argument("--repartition", type=int, default=0, help="spread documents over N tasks")
    parser.add_argument("--corpus-version", type=int, default=1)
    parser.add_argument("--financebench-dir", default=None, help="a directory or s3:// prefix")
    parser.add_argument("--financebench-companies", default=None, help="a file or s3:// URI")
    parser.add_argument(
        "--skip-html",
        type=lambda v: v.lower() in ("1", "true", "yes"),
        default=False,
        help="true: parse only the FinanceBench PDFs",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    env: Env = args.env
    spark = spark_session(env, app_name="parse_filings")
    try:
        counts = run(
            spark, args.html_glob, args.raw_namespace, args.curated_namespace,
            args.corpus_version, args.financebench_dir, args.financebench_companies,
            output_ns=args.output_namespace,
            filings_html_ns=args.filings_html_namespace,
            ciks=[int(c) for c in args.ciks.split(",") if c.strip()],
            filed_from=args.filed_from,
            filed_to=args.filed_to,
            limit_filings=args.limit_filings,
            repartition=args.repartition or None,
            skip_html=args.skip_html,
        )  # fmt: skip
    finally:
        spark.stop()
    log.info("curated.filings %d rows, curated.chunks %d rows", counts["filings"], counts["chunks"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
