"""AWS Glue entry point for `pipelines.spark.fsds_to_iceberg` (all logic lives there).

Job arguments: --lake-bucket (always), --quarters, --src, --namespace, --ciks, --parallel,
--repartition.
`--src` defaults to `s3://<lake-bucket>/raw/fsds`.
"""

import sys

from pipelines.glue.run import run_glue_job
from pipelines.spark.fsds_to_iceberg import main

if __name__ == "__main__":
    run_glue_job(
        main,
        options=["quarters", "src", "namespace", "ciks", "parallel", "repartition"],
        argv=sys.argv[1:],
        defaults={"src": "s3://{bucket}/raw/fsds"},
    )
