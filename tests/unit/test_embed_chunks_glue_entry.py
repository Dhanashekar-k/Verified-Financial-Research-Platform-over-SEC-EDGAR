"""The Glue entry point for `embed_chunks` forwards its options and defaults the model location."""

from __future__ import annotations

from pipelines.glue.run import forwarded_args

OPTIONS = [
    "chunks-table",
    "output-table",
    "model-location",
    "forms",
    "ciks",
    "max-chunks",
    "rows-per-partition",
    "batch-size",
    "scope",
    "sort-by-length",
    "metrics-out",
]
DEFAULTS = {
    "model-location": "s3://{bucket}/artifacts/models/bge-small-en-v1.5",
    "scope": "served",
}


def test_the_staged_model_is_the_default_location():
    bucket, args = forwarded_args(OPTIONS, ["--lake-bucket", "my-lake"], DEFAULTS)

    assert bucket == "my-lake"
    assert args == [
        "--env",
        "glue",
        "--model-location",
        "s3://my-lake/artifacts/models/bge-small-en-v1.5",
        "--scope",
        "served",
    ]


def test_a_smoke_run_forwards_its_arguments_and_glues_own_are_ignored():
    argv = [
        "--JOB_NAME", "edgarqa-embed-chunks",
        "--TempDir", "s3://my-lake/tmp/",
        "--lake-bucket", "my-lake",
        "--output-table", "lake.corpus_smoke.chunk_embeddings",
        "--max-chunks", "2000",
        "--forms", "10-K,10-K/A",
        "--metrics-out", "s3://my-lake/metrics/embed/smoke.json",
    ]  # fmt: skip

    _, args = forwarded_args(OPTIONS, argv, DEFAULTS)

    assert args == [
        "--env", "glue",
        "--output-table", "lake.corpus_smoke.chunk_embeddings",
        "--model-location", "s3://my-lake/artifacts/models/bge-small-en-v1.5",
        "--forms", "10-K,10-K/A",
        "--max-chunks", "2000",
        "--scope", "served",
        "--metrics-out", "s3://my-lake/metrics/embed/smoke.json",
    ]  # fmt: skip


def test_the_entry_script_lists_the_same_options_as_the_job_parser():
    import ast
    from pathlib import Path

    from pipelines.spark.embed_chunks import build_parser

    entry = ast.parse(Path("pipelines/glue/embed_chunks_job.py").read_text(encoding="utf-8"))
    listed = next(
        [e.value for e in kw.value.elts]
        for node in ast.walk(entry)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "options"
    )
    parser_flags = {
        a.option_strings[0].removeprefix("--")
        for a in build_parser()._actions
        if a.option_strings and a.dest not in {"help", "env"}
    }

    assert set(listed) == parser_flags
