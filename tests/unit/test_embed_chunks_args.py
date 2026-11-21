"""Argument-parsing tests for ``pipelines.spark.embed_chunks.main`` (test-first; no Spark started).

Every case here must be decided by argparse BEFORE a SparkSession or the model is touched, so
these run in milliseconds. The end-to-end `main` run (metrics JSON, filters, return code) lives
in ``tests/integration/test_embed_chunks.py``.
"""

from __future__ import annotations

import pytest
from pipelines.spark.embed_chunks import main

FLAGS = [
    "--env",
    "--chunks-table",
    "--output-table",
    "--model-location",
    "--forms",
    "--ciks",
    "--max-chunks",
    "--rows-per-partition",
    "--batch-size",
    "--metrics-out",
]


def test_help_lists_every_documented_flag(capsys: pytest.CaptureFixture[str]):
    """`--help` exits 0 and names all ten flags (the Glue job's argument list is built from it)."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert [f for f in FLAGS if f not in out] == []


def test_model_location_is_required():
    """No `--model-location` -> argparse usage error (exit 2) before any work: the job never
    guesses a model path or downloads a model."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--env", "local"])

    assert exit_info.value.code == 2


@pytest.mark.parametrize("env", ["aws", "", "LOCAL"])
def test_env_must_be_local_emr_or_glue(env: str):
    """Only the three `spark_session` environments are accepted (exit 2 otherwise)."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--env", env, "--model-location", "/m"])

    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--ciks", "320193,abc"),
        ("--max-chunks", "ten"),
        ("--rows-per-partition", "1.5"),
        ("--batch-size", "x"),
    ],
)
def test_non_numeric_values_are_rejected_before_spark_starts(flag: str, value: str):
    """A typo in a numeric flag is a usage error or ValueError, never a run on partial input."""
    with pytest.raises((SystemExit, ValueError)):
        main(["--model-location", "/m", flag, value])
