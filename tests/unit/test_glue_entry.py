"""Unit tests for the Glue entry-point plumbing (`pipelines/glue/run.py`)."""

from __future__ import annotations

import os

import pytest

from pipelines.glue.run import forwarded_args, run_glue_job

# What Glue really puts in sys.argv: its own arguments next to ours.
GLUE_ARGV = [
    "--JOB_NAME", "edgarqa-fsds-to-iceberg",
    "--TempDir", "s3://lake/tmp/",
    "--enable-metrics", "true",
    "--lake-bucket", "my-lake",
    "--quarters", "2023q3,2023q4",
    "--ciks", "320193",
]  # fmt: skip


def test_glue_own_arguments_are_ignored_and_the_job_gets_env_glue():
    bucket, args = forwarded_args(["quarters", "ciks", "namespace"], GLUE_ARGV)

    assert bucket == "my-lake"
    assert args == ["--env", "glue", "--quarters", "2023q3,2023q4", "--ciks", "320193"]


def test_dashes_in_option_names_are_kept():
    _, args = forwarded_args(
        ["fiscal-years", "limit-filings"],
        ["--lake-bucket", "b", "--fiscal-years", "2023"],
    )

    assert args == ["--env", "glue", "--fiscal-years", "2023"]


def test_empty_values_are_not_forwarded_so_the_job_default_applies():
    _, args = forwarded_args(["ciks"], ["--lake-bucket", "b", "--ciks", ""])

    assert args == ["--env", "glue"]


def test_default_may_use_the_bucket():
    _, args = forwarded_args(
        ["src"], ["--lake-bucket", "my-lake"], defaults={"src": "s3://{bucket}/raw/fsds"}
    )

    assert args == ["--env", "glue", "--src", "s3://my-lake/raw/fsds"]


def test_an_explicit_value_beats_the_default():
    _, args = forwarded_args(
        ["src"],
        ["--lake-bucket", "my-lake", "--src", "s3://other/x"],
        defaults={"src": "s3://{bucket}/raw/fsds"},
    )

    assert args == ["--env", "glue", "--src", "s3://other/x"]


def test_the_lake_bucket_is_required():
    with pytest.raises(SystemExit):
        forwarded_args(["quarters"], ["--quarters", "2023q3"])


def test_run_sets_the_bucket_for_the_spark_session_and_calls_main(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("EDGARQA_LAKE_BUCKET", raising=False)
    seen: list[list[str]] = []

    def main(args: list[str]) -> int:
        seen.append(args)
        assert os.environ["EDGARQA_LAKE_BUCKET"] == "my-lake"
        return 0

    run_glue_job(main, ["quarters"], ["--lake-bucket", "my-lake", "--quarters", "2023q3"])

    assert seen == [["--env", "glue", "--quarters", "2023q3"]]


def test_a_nonzero_exit_fails_the_glue_job(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EDGARQA_LAKE_BUCKET", raising=False)

    with pytest.raises(RuntimeError, match="exited with code 1"):
        run_glue_job(lambda _args: 1, [], ["--lake-bucket", "b"])


def test_parallel_and_repartition_reach_the_fsds_job():
    _, args = forwarded_args(
        ["quarters", "parallel", "repartition"],
        ["--lake-bucket", "b", "--quarters", "2020q1", "--parallel", "4", "--repartition", "8"],
    )

    assert args == [
        "--env", "glue", "--quarters", "2020q1", "--parallel", "4", "--repartition", "8",
    ]  # fmt: skip
