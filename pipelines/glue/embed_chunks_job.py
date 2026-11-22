"""AWS Glue entry point for `pipelines.spark.embed_chunks` (all logic lives there; ADR-032).

Job arguments: --lake-bucket (always), --chunks-table, --output-table, --model-location, --forms,
--ciks, --max-chunks, --rows-per-partition, --batch-size, --scope, --sort-by-length,
--metrics-out.
`--scope` defaults to `served` here (10-K family + FinanceBench, ADR-037; the Python job itself
defaults to `all`). `--model-location` defaults to the staged model, `s3://<lake-bucket>/artifacts/models/
bge-small-en-v1.5` (`make stage-model`); the default tables are the real `lake.curated.chunks` and
`lake.curated.chunk_embeddings`. A smoke run passes
`--output-table lake.corpus_smoke.chunk_embeddings` and `--max-chunks 2000`, never the real output
table first.

fastembed and onnxruntime are installed by Glue from the job's `--additional-python-modules`; the
code arrives in pipelines.zip.
"""

import sys

from pipelines.glue.run import run_glue_job
from pipelines.spark.embed_chunks import main

if __name__ == "__main__":
    run_glue_job(
        main,
        options=[
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
        ],
        argv=sys.argv[1:],
        defaults={
            "model-location": "s3://{bucket}/artifacts/models/bge-small-en-v1.5",
            # ADR-037: the embedding scope is the served set; `--scope all` is an explicit opt-in.
            "scope": "served",
        },
    )
