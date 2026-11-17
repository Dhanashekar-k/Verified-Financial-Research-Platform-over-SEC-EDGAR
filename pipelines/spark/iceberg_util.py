"""Small Iceberg helpers shared by the Spark jobs."""

from __future__ import annotations

import logging
import uuid

from pyspark.sql import DataFrame, SparkSession

log = logging.getLogger(__name__)


def ensure_table(spark: SparkSession, table: str, df: DataFrame, partition_column: str) -> None:
    """Create `table` with `df`'s schema, partitioned by `partition_column`, if it is missing.

    Two jobs creating the same table at the same moment can both pass `IF NOT EXISTS` (Spark
    checks before it commits the staged table), and the loser then fails at commit with
    "already exists". If the table exists after such a failure, someone else created it, which
    is all this function promises; any other failure is re-raised.
    """
    view = f"_ensure_{uuid.uuid4().hex}"
    df.limit(0).createOrReplaceTempView(view)
    try:
        spark.sql(
            f"CREATE TABLE IF NOT EXISTS {table} USING iceberg "
            f"PARTITIONED BY ({partition_column}) AS SELECT * FROM {view}"
        )
    except Exception:
        if not spark.catalog.tableExists(table):
            raise
        log.info("%s was created concurrently; continuing", table)
    finally:
        spark.catalog.dropTempView(view)
