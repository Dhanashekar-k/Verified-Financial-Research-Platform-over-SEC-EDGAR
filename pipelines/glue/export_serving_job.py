"""AWS Glue entry point for `pipelines.spark.export_serving` (logic lives there; M03 section 3.2).

Job arguments: --lake-bucket (always), --export-root, --version, --namespace, --git-sha,
--extra-ciks.
`--export-root` defaults to `s3://<lake-bucket>/exports/serving`; omit `--version` for max + 1.
"""

import sys

from pipelines.glue.run import run_glue_job
from pipelines.spark.export_serving import main

if __name__ == "__main__":
    run_glue_job(
        main,
        options=["export-root", "version", "namespace", "git-sha", "extra-ciks"],
        argv=sys.argv[1:],
        defaults={"export-root": "s3://{bucket}/exports/serving"},
    )
