"""Unit tests for the Spark configuration (no JVM needed): `build_spark_conf`."""

from __future__ import annotations

import pytest

from pipelines.common.spark_session import CATALOG, ICEBERG_PACKAGE, build_spark_conf


def test_local_conf_matches_the_runbook(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EDGARQA_SPARK_DRIVER_MEMORY", raising=False)
    conf = build_spark_conf("local")

    assert conf["spark.sql.extensions"].endswith("IcebergSparkSessionExtensions")
    assert conf[f"spark.sql.catalog.{CATALOG}"] == "org.apache.iceberg.spark.SparkCatalog"
    assert conf[f"spark.sql.catalog.{CATALOG}.type"] == "hadoop"
    assert conf[f"spark.sql.catalog.{CATALOG}.warehouse"] == "./warehouse"
    assert conf["spark.jars.packages"] == ICEBERG_PACKAGE
    assert conf["spark.driver.memory"] == "8g"
    assert conf["spark.sql.shuffle.partitions"] == "64"


def test_local_disables_iceberg_vectorized_reads():
    """Workaround for a reproducible JVM crash in the Arrow reader on this machine (see conf)."""
    assert build_spark_conf("local")["spark.sql.iceberg.vectorization.enabled"] == "false"
    assert "spark.sql.iceberg.vectorization.enabled" not in build_spark_conf(
        "emr", warehouse="s3://b/w"
    )


def test_iceberg_runtime_matches_spark_3_5_scala_2_12():
    assert "iceberg-spark-runtime-3.5_2.12" in ICEBERG_PACKAGE


def test_csv_column_pruning_is_off_so_failfast_validates_whole_rows():
    for env_conf in (build_spark_conf("local"), build_spark_conf("emr", warehouse="s3://b/w")):
        assert env_conf["spark.sql.csv.parser.columnPruning.enabled"] == "false"


def test_all_environments_pin_utc():
    """Fiscal dates must not depend on the machine's time zone."""
    assert build_spark_conf("local")["spark.sql.session.timeZone"] == "UTC"
    assert build_spark_conf("emr", warehouse="s3://b/w")["spark.sql.session.timeZone"] == "UTC"


def test_local_warehouse_and_extra_conf_override():
    conf = build_spark_conf(
        "local", warehouse="/tmp/wh", extra_conf={"spark.sql.shuffle.partitions": "4"}
    )

    assert conf[f"spark.sql.catalog.{CATALOG}.warehouse"] == "/tmp/wh"
    assert conf["spark.sql.shuffle.partitions"] == "4"


def test_driver_memory_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EDGARQA_SPARK_DRIVER_MEMORY", "3g")

    assert build_spark_conf("local")["spark.driver.memory"] == "3g"


def test_emr_uses_glue_catalog_and_s3_io(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EDGARQA_LAKE_BUCKET", "my-lake")
    conf = build_spark_conf("emr")

    assert conf[f"spark.sql.catalog.{CATALOG}.catalog-impl"].endswith("GlueCatalog")
    assert conf[f"spark.sql.catalog.{CATALOG}.io-impl"].endswith("S3FileIO")
    assert conf[f"spark.sql.catalog.{CATALOG}.warehouse"] == "s3://my-lake/warehouse"
    # EMR ships its own Iceberg runtime jar; never ask Maven for one there.
    assert "spark.jars.packages" not in conf
    assert f"spark.sql.catalog.{CATALOG}.type" not in conf


def test_emr_without_a_bucket_or_warehouse_fails(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EDGARQA_LAKE_BUCKET", raising=False)

    with pytest.raises(ValueError, match="EDGARQA_LAKE_BUCKET"):
        build_spark_conf("emr")


def test_unknown_env_is_rejected():
    with pytest.raises(ValueError):
        build_spark_conf("cloud")  # type: ignore[arg-type]
