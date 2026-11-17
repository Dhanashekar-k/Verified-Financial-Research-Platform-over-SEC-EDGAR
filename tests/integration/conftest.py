"""Fixtures for integration tests (local Spark with an Iceberg hadoop catalog in a tmp dir)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator

import pytest
from pyspark.sql import SparkSession

from pipelines.common.spark_session import spark_session


@pytest.fixture(scope="session")
def spark(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SparkSession]:
    """One local SparkSession per test run, with its own empty warehouse.

    The first run downloads the Iceberg runtime jar from Maven Central (cached afterwards under
    ~/.ivy2); that is dependency resolution, not a test of any external service. Skipped when no
    JVM is available so `make test` still works on a machine without Java.
    """
    # /usr/bin/java on macOS is a stub that exists but fails, so run it rather than look it up.
    java = shutil.which("java")
    if java is None or subprocess.run([java, "-version"], capture_output=True).returncode != 0:
        pytest.skip(
            "no working java; install JDK 17 (and set JAVA_HOME) for Spark integration tests"
        )
    warehouse = tmp_path_factory.mktemp("warehouse")
    session = spark_session(
        "local",
        app_name="edgarqa-tests",
        warehouse=str(warehouse),
        extra_conf={
            "spark.driver.memory": "2g",
            "spark.sql.shuffle.partitions": "4",
            "spark.default.parallelism": "4",
            "spark.sql.adaptive.enabled": "false",
        },
    )
    yield session
    session.stop()


@pytest.fixture
def lake_namespace(spark: SparkSession, request: pytest.FixtureRequest) -> Iterator[str]:
    """A throwaway namespace `lake.t_<test>` dropped afterwards, so tests don't see each other."""
    name = "t_" + "".join(c if c.isalnum() else "_" for c in request.node.name)[:40].lower()
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS lake.{name}")
    yield f"lake.{name}"
    for row in spark.sql(f"SHOW TABLES IN lake.{name}").collect():
        spark.sql(f"DROP TABLE IF EXISTS lake.{name}.{row.tableName} PURGE")
    spark.sql(f"DROP NAMESPACE IF EXISTS lake.{name}")
