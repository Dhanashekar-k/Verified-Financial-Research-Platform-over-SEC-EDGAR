"""The one place a SparkSession is built (M01 section 3.3).

Local (`env="local"`): Iceberg `hadoop` catalog `lake` at `./warehouse`, runtime jar fetched by
Spark from Maven on first use and cached under `~/.ivy2`.

EMR Serverless (`env="emr"`): Glue catalog, S3 file IO, the Iceberg runtime jar that ships with
the EMR release. `[Likely]` per the runbook: check the EMR Serverless Iceberg docs for the
release you pick before the first cloud run.

AWS Glue (`env="glue"`): the same Glue catalog and S3 file IO. The Iceberg runtime jar comes from
the job parameter `--datalake-formats iceberg`, so nothing here points at a jar. This is the path
used while an Organization policy denies EMR Serverless (ADR-027).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pyspark.sql import SparkSession

Env = Literal["local", "emr", "glue"]

CATALOG = "lake"
# Iceberg runtime for Spark 3.5 / Scala 2.12. This is the LOCAL pin. For EMR the bundled jar is
# used instead: the emr-7.14.0 image ships iceberg-spark-runtime-3.5_2.12-1.10.1-amzn-1 on the
# driver and executor classpath (read from the image, ADR-024). Reading tables written locally
# with 1.6.1 is expected to work; that is NOT yet verified by an EMR run (M01 section 3.8).
ICEBERG_VERSION = "1.6.1"
ICEBERG_PACKAGE = f"org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:{ICEBERG_VERSION}"


def build_spark_conf(
    env: Env,
    warehouse: str | None = None,
    extra_conf: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The Spark configuration for `env`, as a plain dict (testable without a JVM).

    `warehouse` overrides the default: `./warehouse` locally, and
    `s3://$EDGARQA_LAKE_BUCKET/warehouse` on EMR and Glue. `extra_conf` wins over everything.
    """
    conf: dict[str, str] = {
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        f"spark.sql.catalog.{CATALOG}": "org.apache.iceberg.spark.SparkCatalog",
        # Dates and timestamps must not depend on the machine's time zone.
        "spark.sql.session.timeZone": "UTC",
        # With pruning on, Spark parses only the CSV columns a query uses, so FAILFAST misses a
        # malformed row in the others (a short `sub` row loads with NULLs). Raw loads must
        # validate whole rows.
        "spark.sql.csv.parser.columnPruning.enabled": "false",
    }
    if env == "local":
        conf |= {
            f"spark.sql.catalog.{CATALOG}.type": "hadoop",
            f"spark.sql.catalog.{CATALOG}.warehouse": warehouse or "./warehouse",
            "spark.jars.packages": ICEBERG_PACKAGE,
            # Local only. Iceberg's vectorized (Arrow) reader crashes the JVM on this machine:
            # Homebrew OpenJDK 17.0.21 on macOS 26 dies with SIGSEGV/SIGBUS inside the JIT
            # compiler thread (libjvm SymbolTableLookup / ciSignature) once a query over millions
            # of rows makes the reader hot. Reproduced 6/6 with it on (also with -Xshare:off and
            # -XX:-TieredCompilation), 0/2 with it off. Row-based reads are slower but safe.
            # EMR runs Linux and is not affected as far as we know; the first EMR run will tell.
            "spark.sql.iceberg.vectorization.enabled": "false",
            "spark.driver.memory": os.environ.get("EDGARQA_SPARK_DRIVER_MEMORY", "8g"),
            "spark.sql.shuffle.partitions": "64",
            "spark.ui.enabled": "false",
        }
    elif env in ("emr", "glue"):
        bucket = os.environ.get("EDGARQA_LAKE_BUCKET")
        if warehouse is None:
            if not bucket:
                raise ValueError(f"set EDGARQA_LAKE_BUCKET (or pass warehouse=) for env={env!r}")
            warehouse = f"s3://{bucket}/warehouse"
        conf |= {
            f"spark.sql.catalog.{CATALOG}.catalog-impl": "org.apache.iceberg.aws.glue.GlueCatalog",
            f"spark.sql.catalog.{CATALOG}.io-impl": "org.apache.iceberg.aws.s3.S3FileIO",
            f"spark.sql.catalog.{CATALOG}.warehouse": warehouse,
        }
    else:
        raise ValueError(f"unknown env {env!r}; expected 'local', 'emr' or 'glue'")
    if extra_conf:
        conf |= dict(extra_conf)
    return conf


def spark_session(
    env: Env,
    app_name: str = "edgarqa",
    warehouse: str | None = None,
    extra_conf: Mapping[str, str] | None = None,
) -> SparkSession:
    """Build (or reuse) the SparkSession for `env`."""
    builder = SparkSession.builder.appName(app_name)
    if env == "local":
        builder = builder.master("local[*]")
    for key, value in build_spark_conf(env, warehouse, extra_conf).items():
        builder = builder.config(key, value)
    return builder.getOrCreate()
