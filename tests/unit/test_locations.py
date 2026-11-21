"""`pipelines.common.locations` reads a local file and an `s3://` object the same way (moto)."""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from pipelines.common import locations
from pipelines.glue.run import forwarded_args
from pipelines.spark.build_companies import snapshot_date, snapshot_properties
from pipelines.spark.parse_filings import _pdf_uri, read_financebench_companies

CSV = "company,cik\n3M,66740\nAMD,2488\n"


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def bucket():
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="lake")
        yield s3


def test_a_local_and_an_s3_file_read_the_same(bucket, tmp_path: Path):
    bucket.put_object(Bucket="lake", Key="reference/a.csv", Body=CSV.encode())
    (tmp_path / "a.csv").write_text(CSV)

    assert locations.read_text("s3://lake/reference/a.csv") == CSV
    assert locations.read_text(tmp_path / "a.csv") == CSV
    assert locations.exists("s3://lake/reference/a.csv") and locations.exists(tmp_path / "a.csv")
    assert not locations.exists("s3://lake/reference/nope.csv")
    assert not locations.exists(tmp_path / "nope.csv")


def test_newest_picks_the_latest_dated_name_locally_and_on_s3(bucket, tmp_path: Path):
    for day in ("2026-11-16", "2026-11-17"):
        bucket.put_object(Bucket="lake", Key=f"reference/sp500_{day}.csv", Body=b"x")
        (tmp_path / f"sp500_{day}.csv").write_text("x")
    bucket.put_object(Bucket="lake", Key="reference/sub/sp500_2026-12-31.csv", Body=b"x")

    assert locations.newest("s3://lake/reference", "sp500_*.csv") == (
        "s3://lake/reference/sp500_2026-11-17.csv"
    )
    assert locations.newest(tmp_path, "sp500_*.csv") == tmp_path / "sp500_2026-11-17.csv"
    with pytest.raises(FileNotFoundError):
        locations.newest("s3://lake/reference", "tickers_*.json")


def test_the_financebench_company_csv_is_read_from_s3(bucket):
    bucket.put_object(
        Bucket="lake", Key="financebench/financebench_companies.csv", Body=CSV.encode()
    )

    got = read_financebench_companies("s3://lake/financebench/financebench_companies.csv")

    assert got == {"3M": 66740, "AMD": 2488}


def test_a_pdf_row_points_at_the_s3_object_or_the_local_file_uri(tmp_path: Path):
    assert _pdf_uri("s3://lake/financebench/", "3M_2018_10K") == (
        "s3://lake/financebench/pdfs/3M_2018_10K.pdf"
    )
    assert _pdf_uri(tmp_path, "3M_2018_10K") == (tmp_path / "pdfs/3M_2018_10K.pdf").as_uri()


def test_snapshot_date_and_checksums_come_from_an_s3_file_name_and_bytes(bucket):
    bucket.put_object(Bucket="lake", Key="reference/sp500_2026-11-17.csv", Body=b"abc")
    bucket.put_object(Bucket="lake", Key="reference/company_tickers_2026-11-16.json", Body=b"{}")
    sp, tk = (
        "s3://lake/reference/sp500_2026-11-17.csv",
        "s3://lake/reference/company_tickers_2026-11-16.json",
    )

    props = snapshot_properties(tk, sp)

    assert snapshot_date(sp) == "2026-11-17"
    assert props["edgarqa.sp500_snapshot_date"] == "2026-11-17"
    assert props["edgarqa.ticker_map_snapshot_date"] == "2026-11-16"
    assert props["edgarqa.sp500_sha256"] == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"  # sha256(b"abc")
    )


def test_the_build_companies_glue_job_reads_the_frozen_files_from_the_lake_bucket():
    _, args = forwarded_args(
        ["data-dir", "tickers", "sp500"],
        ["--lake-bucket", "my-lake"],
        defaults={"data-dir": "s3://{bucket}/reference"},
    )

    assert args == ["--env", "glue", "--data-dir", "s3://my-lake/reference"]
