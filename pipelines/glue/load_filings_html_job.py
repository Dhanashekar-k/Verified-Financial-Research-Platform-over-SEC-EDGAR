"""AWS Glue entry point for `pipelines.spark.load_filings_html` (all logic lives there).

Job arguments: --lake-bucket (always), --manifest, --namespace.
`--manifest` defaults to `s3://<lake-bucket>/raw/filings/_manifest.jsonl`.
"""

import sys

from pipelines.glue.run import run_glue_job
from pipelines.spark.load_filings_html import main

if __name__ == "__main__":
    run_glue_job(
        main,
        options=["manifest", "namespace"],
        argv=sys.argv[1:],
        defaults={"manifest": "s3://{bucket}/raw/filings/_manifest.jsonl"},
    )
