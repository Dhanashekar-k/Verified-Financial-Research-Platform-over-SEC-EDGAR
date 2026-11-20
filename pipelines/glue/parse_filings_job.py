"""AWS Glue entry point for `pipelines.spark.parse_filings` (all logic lives there).

Job arguments: --lake-bucket (always), --html-glob, --raw-namespace, --curated-namespace,
--output-namespace, --filings-html-namespace, --ciks, --filed-from, --filed-to, --limit-filings,
--repartition, --corpus-version. `--html-glob` defaults to `s3://<lake-bucket>/raw/filings`.

The parsing libraries (selectolax, tokenizers, pymupdf, pydantic) are installed by Glue from the
job's `--additional-python-modules`; the code itself arrives in pipelines.zip.
"""

import sys

from pipelines.glue.run import run_glue_job
from pipelines.spark.parse_filings import main

if __name__ == "__main__":
    run_glue_job(
        main,
        options=[
            "html-glob",
            "raw-namespace",
            "curated-namespace",
            "output-namespace",
            "filings-html-namespace",
            "ciks",
            "filed-from",
            "filed-to",
            "limit-filings",
            "repartition",
            "corpus-version",
        ],
        argv=sys.argv[1:],
        defaults={"html-glob": "s3://{bucket}/raw/filings"},
    )
