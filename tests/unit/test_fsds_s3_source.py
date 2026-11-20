"""The FSDS checks read an `s3://` source exactly like a local one (moto, no real AWS)."""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from pipelines.spark.fsds_to_iceberg import (
    HeaderMismatch,
    check_header,
    count_source_rows,
    join_location,
    source_exists,
)
from pipelines.spark.raw_schemas import SOURCE_SCHEMAS

HEADER = "\t".join(SOURCE_SCHEMAS["tag"].fieldNames())


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def bucket() -> Iterator[str]:
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="lake")
        yield "lake"


def put(bucket: str, key: str, text: str) -> None:
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=gzip.compress(text.encode()))


def test_join_location_keeps_the_s3_scheme_and_handles_trailing_slashes():
    assert join_location("s3://lake/raw/fsds/", "2023q3", "num.txt.gz") == (
        "s3://lake/raw/fsds/2023q3/num.txt.gz"
    )
    assert join_location(Path("data/raw/fsds"), "2023q3", "sub.txt.gz") == (
        "data/raw/fsds/2023q3/sub.txt.gz"
    )


def test_count_rows_is_lines_minus_header_on_s3(bucket: str):
    put(bucket, "raw/fsds/2023q3/tag.txt.gz", f"{HEADER}\na\nb\nc\n")

    assert count_source_rows("s3://lake/raw/fsds/2023q3/tag.txt.gz") == 3


def test_count_rows_counts_a_last_line_without_a_newline(bucket: str):
    put(bucket, "k.gz", f"{HEADER}\na\nb")

    assert count_source_rows("s3://lake/k.gz") == 2


def test_s3_and_local_agree(bucket: str, tmp_path: Path):
    text = f"{HEADER}\n1\n\n2\n"
    put(bucket, "k.gz", text)
    local = tmp_path / "k.gz"
    local.write_bytes(gzip.compress(text.encode()))

    assert count_source_rows("s3://lake/k.gz") == count_source_rows(local) == 3


def test_a_correct_header_passes_on_s3(bucket: str):
    put(bucket, "k.gz", f"{HEADER}\n")

    check_header("s3://lake/k.gz", "tag")


def test_a_wrong_header_is_rejected_on_s3(bucket: str):
    put(bucket, "k.gz", "tag\tversion\n")

    with pytest.raises(HeaderMismatch):
        check_header("s3://lake/k.gz", "tag")


def test_source_exists_on_s3(bucket: str):
    put(bucket, "here.gz", HEADER)

    assert source_exists("s3://lake/here.gz")
    assert not source_exists("s3://lake/missing.gz")
