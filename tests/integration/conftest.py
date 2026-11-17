"""Fixtures for integration tests (local Spark with an Iceberg hadoop catalog in a tmp dir)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from pyspark.sql import SparkSession

from pipelines.common.spark_session import spark_session

# Where Homebrew's `brew install openjdk@17` puts the JDK. Used only when JAVA_HOME is unset.
_BREW_JDK17 = "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"


def resolve_java() -> str:
    """The `java` binary Spark will use; raises `pytest.fail` (never a skip) when there is none.

    JAVA_HOME wins; when unset, Homebrew's openjdk@17 is used if present and exported so the
    PySpark launcher finds it too. A skipped Spark test is a silent hole in the suite (the
    fiscal and version tests run through Spark), so a missing JDK is a failure.
    """
    home = os.environ.get("JAVA_HOME")
    if not home and Path(_BREW_JDK17).is_dir():
        home = os.environ["JAVA_HOME"] = _BREW_JDK17
    if not home:
        pytest.fail(
            "JAVA_HOME is not set and no JDK 17 was found at "
            f"{_BREW_JDK17}. Install one (brew install openjdk@17) and export JAVA_HOME, or run "
            "through `make test-integration`. Spark tests are never skipped.",
            pytrace=False,
        )
    java = Path(home) / "bin" / "java"
    try:
        ok = subprocess.run([str(java), "-version"], capture_output=True).returncode == 0
    except OSError:
        ok = False
    if not ok:
        pytest.fail(f"JAVA_HOME={home} does not contain a working bin/java.", pytrace=False)
    os.environ["PATH"] = f"{java.parent}{os.pathsep}{os.environ.get('PATH', '')}"
    return str(java)


@pytest.fixture(scope="session")
def spark(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SparkSession]:
    """One local SparkSession per test run, with its own empty warehouse.

    The first run downloads the Iceberg runtime jar from Maven Central (cached afterwards under
    ~/.ivy2); that is dependency resolution, not a test of any external service. Without a
    working JDK this fixture fails (see `resolve_java`); it does not skip.
    """
    resolve_java()
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
