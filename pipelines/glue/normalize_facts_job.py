"""AWS Glue entry point for `pipelines.spark.normalize_facts` (all logic lives there).

Job arguments: --lake-bucket (always), --raw-namespace, --curated-namespace, --ciks,
--fiscal-years, --limit-filings.
"""

import sys

from pipelines.glue.run import run_glue_job
from pipelines.spark.normalize_facts import main

if __name__ == "__main__":
    run_glue_job(
        main,
        options=[
            "raw-namespace",
            "curated-namespace",
            "ciks",
            "fiscal-years",
            "limit-filings",
        ],
        argv=sys.argv[1:],
    )
