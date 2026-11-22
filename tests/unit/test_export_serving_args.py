"""Argument parsing and schema-constant tests for ``pipelines.spark.export_serving`` (test-first).

Every CLI case here must be decided by argparse BEFORE a SparkSession is built, so these run in
milliseconds. The end-to-end `main` run lives in ``tests/integration/test_export_serving.py``.
"""

from __future__ import annotations

import pytest

from pipelines.spark.curated_schemas import CORPUS_VERSIONS
from pipelines.spark.export_serving import ExportError, main

FLAGS = ["--env", "--export-root", "--version", "--namespace", "--git-sha", "--extra-ciks"]


def test_help_lists_every_documented_flag(capsys: pytest.CaptureFixture[str]):
    """`--help` exits 0 and names all six flags (the Glue job's argument list is built from it)."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert [f for f in FLAGS if f not in out] == []


def test_export_root_is_required():
    """No `--export-root` -> argparse usage error (exit 2) before any work: the job never
    guesses where to write."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--env", "local"])

    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "bad",
    [
        ["--env", "prod"],
        ["--version", "x"],
        ["--version", "1.5"],
        ["--extra-ciks", "a,b"],
        ["--extra-ciks", "1,,2x"],
    ],
)
def test_malformed_values_exit_with_a_usage_error(bad: list[str]):
    """An unknown --env, a non-integer --version and non-integer CIKs are rejected by argparse
    (exit 2), not deep inside the Spark job."""
    with pytest.raises(SystemExit) as exit_info:
        main(["--export-root", "/nonexistent/export", *bad])

    assert exit_info.value.code == 2


def test_export_error_is_an_exception_type():
    """`ExportError` is a plain Exception subclass the CLI can report."""
    assert issubclass(ExportError, Exception)


def test_corpus_versions_schema_follows_contract_section_2_2():
    """`CORPUS_VERSIONS` has the 13 contract columns, in order, with the contract types."""
    assert [(f.name, f.dataType.simpleString()) for f in CORPUS_VERSIONS.fields] == [
        ("corpus_version", "int"),
        ("created_at", "timestamp"),
        ("git_sha", "string"),
        ("facts_rows", "bigint"),
        ("chunks_rows", "bigint"),
        ("chunks_excluded", "bigint"),
        ("embedding_model", "string"),
        ("embedding_artifact_sha256", "string"),
        ("dim", "int"),
        ("facts_snapshot_id", "bigint"),
        ("chunks_snapshot_id", "bigint"),
        ("chunk_embeddings_snapshot_id", "bigint"),
        ("export_uri", "string"),
    ]
