"""AWS Glue entry point for `pipelines.spark.build_companies` (all logic lives there).

Job arguments: --lake-bucket (always), --data-dir, --tickers, --sp500, --raw-namespace,
--curated-namespace. `--data-dir` defaults to `s3://<lake-bucket>/reference`, where the frozen
`company_tickers_<date>.json` and `sp500_<date>.csv` are staged; the newest of each is used.

The job imports `pipelines.fetch.reference_data` (the ticker-map parser), which needs `httpx` and
`python-dotenv`: Glue installs them from the job's `--additional-python-modules`.
"""

import sys

from pipelines.glue.run import run_glue_job
from pipelines.spark.build_companies import main

if __name__ == "__main__":
    run_glue_job(
        main,
        options=["data-dir", "tickers", "sp500", "raw-namespace", "curated-namespace"],
        argv=sys.argv[1:],
        defaults={"data-dir": "s3://{bucket}/reference"},
    )
