"""`pipelines.cloud.stage_manifest`: a local manifest rewritten to name S3 objects."""

from __future__ import annotations

import json

import pytest

from pipelines.cloud.stage_manifest import object_key, rewrite_lines

LOCAL = (
    "file:///Users/me/project%201.%20financial%20report/edgar-verified-qa/"
    "data/raw/filings/19617/0000019617-20-000299.htm.gz"
)
ROW = {
    "accession": "0000019617-20-000299", "cik": 19617, "form": "10-Q", "filed": "2020-05-07",
    "url": "https://www.sec.gov/Archives/edgar/data/19617/x.htm", "s3_uri": LOCAL,
    "bytes": 692835, "sha256": "ab" * 32, "http_status": 200, "fetched_at": "2026-11-18T00:30:12Z",
}  # fmt: skip


def test_the_key_is_cik_slash_accession_even_with_spaces_and_dots_in_the_local_path():
    assert object_key(LOCAL) == "19617/0000019617-20-000299.htm.gz"


def test_an_s3_uri_gives_the_same_key():
    assert object_key("s3://lake/raw/filings/19617/0000019617-20-000299.htm.gz") == (
        "19617/0000019617-20-000299.htm.gz"
    )


def test_only_the_uri_changes():
    [out] = rewrite_lines([json.dumps(ROW)], "s3://lake/raw/filings/")
    row = json.loads(out)

    assert row["s3_uri"] == "s3://lake/raw/filings/19617/0000019617-20-000299.htm.gz"
    assert {k: v for k, v in row.items() if k != "s3_uri"} == {
        k: v for k, v in ROW.items() if k != "s3_uri"
    }


def test_a_failed_fetch_row_without_an_object_is_untouched():
    failed = ROW | {"s3_uri": None, "bytes": None, "sha256": None, "http_status": 404}

    [out] = rewrite_lines([json.dumps(failed)], "s3://lake/raw/filings")

    assert json.loads(out) == failed


def test_blank_lines_are_skipped():
    assert rewrite_lines(["", json.dumps(ROW), "  "], "s3://lake/p") != []
    assert len(rewrite_lines(["", json.dumps(ROW), "  "], "s3://lake/p")) == 1


def test_a_non_s3_destination_is_rejected():
    with pytest.raises(ValueError, match="s3://"):
        rewrite_lines([json.dumps(ROW)], "/tmp/out")


def test_a_uri_that_is_not_a_filing_object_is_rejected():
    with pytest.raises(ValueError, match="not a fetched-filing"):
        object_key("file:///tmp/other.txt")
