"""Tests for ``pipelines/cloud``: the code bundle, the entry point and the EMR run request.

Nothing here touches a real AWS account: the bundle is built locally, S3 is moto, and the run
request is only built (and, with a stub client, polled). Cost math is checked on its own.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from pipelines.cloud import emr_run, package
from pipelines.cloud.emr_run import (
    RunConfig,
    build_request,
    cost_usd,
    metrics_record,
    module_of,
    usd_per_million,
    wait,
    write_metrics,
)

REPO = Path(__file__).resolve().parents[3]
CFG = RunConfig(
    "00app",
    "arn:aws:iam::123456789012:role/edgarqa-emr-job",
    "lake-b",
    "abc123def456",
    "parse_filings",
)
PRICES = {"vcpu_hour": "0.04", "memory_gb_hour": "0.005", "storage_gb_hour": "0.0001"}


# -- package ------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def test_the_bundle_has_the_package_and_the_tokenizer_but_no_tests_or_caches(tmp_path: Path):
    out = package.build_zip(REPO, tmp_path / "p.zip")

    names = set(zipfile.ZipFile(out).namelist())
    assert "pipelines/spark/parse_filings.py" in names
    assert "pipelines/parsing/assets/bge-small-en-v1.5.tokenizer.json" in names
    assert "pipelines/cloud/entrypoint.py" in names
    assert not any("__pycache__" in n or n.startswith("tests/") for n in names)
    assert all(n.startswith("pipelines/") for n in names)


def test_the_bundle_is_reproducible(tmp_path: Path):
    a = package.build_zip(REPO, tmp_path / "a.zip").read_bytes()
    b = package.build_zip(REPO, tmp_path / "b.zip").read_bytes()

    assert a == b


def test_upload_writes_the_zip_and_the_entrypoint_under_the_sha(tmp_path: Path):
    zip_path = package.build_zip(REPO, tmp_path / "p.zip")
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="lake-b")

        uris = package.upload(
            "s3://lake-b/artifacts",
            "abc123def456",
            zip_path,
            REPO / "pipelines/cloud/entrypoint.py",
        )

        keys = [o["Key"] for o in boto3.client("s3").list_objects_v2(Bucket="lake-b")["Contents"]]
    assert sorted(keys) == [
        "artifacts/abc123def456/entrypoint.py",
        "artifacts/abc123def456/pipelines.zip",
    ]
    assert uris[0] == "s3://lake-b/artifacts/abc123def456/pipelines.zip"


def test_main_builds_without_uploading_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    rc = package.main(["--root", str(REPO), "--out", str(tmp_path)])

    assert rc == 0
    assert "not uploaded" in capsys.readouterr().out
    assert len(list(tmp_path.glob("pipelines-*.zip"))) == 1


def test_main_rejects_a_non_s3_upload_target(tmp_path: Path):
    assert package.main(["--root", str(REPO), "--out", str(tmp_path), "--upload", "/tmp/x"]) == 2


# -- entrypoint ---------------------------------------------------------------------------


def test_the_entrypoint_runs_a_module_as_main_with_its_arguments(tmp_path: Path):
    (tmp_path / "demo_job.py").write_text(
        "import sys\nprint('ARGS', sys.argv[1:], __name__)\n", encoding="utf-8"
    )

    done = subprocess.run(
        [sys.executable, str(REPO / "pipelines/cloud/entrypoint.py"), "demo_job", "--env", "emr"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(tmp_path), "PATH": ""},
    )

    assert done.returncode == 0, done.stderr
    assert "ARGS ['--env', 'emr'] __main__" in done.stdout


# -- the run request ----------------------------------------------------------------------


def test_the_request_points_at_the_sha_scoped_bundle_and_the_python_312_image():
    req = build_request(CFG)

    sub = req["jobDriver"]["sparkSubmit"]
    assert sub["entryPoint"] == "s3://lake-b/artifacts/abc123def456/entrypoint.py"
    assert sub["entryPointArguments"] == ["pipelines.spark.parse_filings", "--env", "emr"]
    params = sub["sparkSubmitParameters"]
    assert "--py-files s3://lake-b/artifacts/abc123def456/pipelines.zip" in params
    assert "spark.executorEnv.PYSPARK_PYTHON=/usr/bin/python3.12" in params
    assert "spark.emr-serverless.driverEnv.PYSPARK_PYTHON=/usr/bin/python3.12" in params
    assert "spark.emr-serverless.driverEnv.EDGARQA_LAKE_BUCKET=lake-b" in params
    assert req["configurationOverrides"]["monitoringConfiguration"][
        "s3MonitoringConfiguration"
    ] == {"logUri": "s3://lake-b/emr-logs/"}
    assert req["executionTimeoutMinutes"] == 240  # a runaway job is capped, not unbounded


def test_the_job_name_cannot_inject_a_module_path():
    assert module_of("normalize_facts") == "pipelines.spark.normalize_facts"
    for bad in ("../x", "a.b", "a b", ""):
        with pytest.raises(ValueError):
            module_of(bad)


def test_the_launcher_pins_python_312_like_the_dockerfile():
    assert emr_run.PYTHON == "/usr/bin/python3.12"
    dockerfile = (REPO / "infra/emr/Dockerfile").read_text()
    assert "PYSPARK_PYTHON=/usr/bin/python3.12" in dockerfile


# -- cost ---------------------------------------------------------------------------------


def test_cost_is_billed_hours_times_the_supplied_unit_prices():
    billed = {"vCPUHour": 100.0, "memoryGBHour": 400.0, "storageGBHour": 1000.0}

    assert cost_usd(billed, PRICES) == Decimal("100") * Decimal("0.04") + Decimal("400") * Decimal(
        "0.005"
    ) + Decimal("1000") * Decimal("0.0001")


def test_without_all_three_prices_the_cost_is_none_never_a_default():
    billed = {"vCPUHour": 1, "memoryGBHour": 1, "storageGBHour": 1}

    assert cost_usd(billed, {**PRICES, "storage_gb_hour": None}) is None
    assert (
        cost_usd(billed, {"vcpu_hour": None, "memory_gb_hour": None, "storage_gb_hour": None})
        is None
    )


def test_dollars_per_million_facts_needs_a_cost_and_a_row_count():
    assert usd_per_million(Decimal("12"), 6_000_000) == Decimal("2")
    assert usd_per_million(None, 6_000_000) is None
    assert usd_per_million(Decimal("12"), None) is None
    assert usd_per_million(Decimal("12"), 0) is None


class FakeEmr:
    def __init__(self, states: list[str], **extra: Any) -> None:
        self.states, self.calls, self.extra = states, 0, extra

    def get_job_run(self, applicationId: str, jobRunId: str) -> dict[str, Any]:
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        return {"jobRun": {"jobRunId": jobRunId, "state": state, **self.extra}}


def test_wait_polls_until_a_terminal_state():
    client = FakeEmr(["SUBMITTED", "RUNNING", "RUNNING", "SUCCESS"])
    sleeps: list[float] = []

    run = wait(client, "app", "run1", sleep=sleeps.append, poll_seconds=7)

    assert run["state"] == "SUCCESS" and client.calls == 4 and sleeps == [7, 7, 7]


def test_wait_gives_up_instead_of_polling_forever():
    with pytest.raises(TimeoutError):
        wait(FakeEmr(["RUNNING"]), "app", "run1", sleep=lambda s: None, max_polls=3)


def test_the_metrics_file_records_billed_use_and_marks_missing_prices_not_measured(tmp_path: Path):
    run = {
        "jobRunId": "r1",
        "state": "SUCCESS",
        "totalExecutionDurationSeconds": 1800,
        "billedResourceUtilization": {"vCPUHour": 10.0, "memoryGBHour": 40.0, "storageGBHour": 5.0},
    }

    record = metrics_record(
        CFG, run, {"vcpu_hour": None, "memory_gb_hour": None, "storage_gb_hour": None}, 8_000_000
    )
    path = write_metrics(record, tmp_path)

    saved = json.loads(path.read_text())
    assert path.name == "parse_filings-r1.json"
    assert saved["cost_usd"] is None and saved["usd_per_million_facts"] is None
    assert "NOT MEASURED" in saved["note"]
    assert saved["billed_resource_utilization"]["vCPUHour"] == 10.0
    assert saved["total_execution_duration_seconds"] == 1800


def test_the_metrics_file_computes_dollars_per_million_facts_when_priced(tmp_path: Path):
    run = {
        "jobRunId": "r2",
        "state": "SUCCESS",
        "billedResourceUtilization": {"vCPUHour": 100, "memoryGBHour": 0, "storageGBHour": 0},
    }

    saved = metrics_record(CFG, run, PRICES, 4_000_000)

    assert Decimal(saved["cost_usd"]) == Decimal("4")
    assert Decimal(saved["usd_per_million_facts"]) == Decimal("1")


# -- the command line ---------------------------------------------------------------------


def test_main_without_yes_prints_the_request_and_starts_nothing(capsys: pytest.CaptureFixture[str]):
    class Boom:
        def start_job_run(self, **kw: Any) -> None:
            raise AssertionError("a dry run must not start a job")

    rc = emr_run.main(
        [
            "--job",
            "parse_filings",
            "--application-id",
            "a",
            "--role-arn",
            "r",
            "--bucket",
            "b",
            "--git-sha",
            "s",
        ],
        client=Boom(),
    )

    out = capsys.readouterr().out
    assert rc == 0 and "dry run: nothing was started" in out
    assert '"entryPoint": "s3://b/artifacts/s/entrypoint.py"' in out


def test_main_reports_missing_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    for name in (
        "EDGARQA_EMR_APP_ID",
        "EDGARQA_EMR_ROLE_ARN",
        "EDGARQA_LAKE_BUCKET",
        "EDGARQA_GIT_SHA",
    ):
        monkeypatch.delenv(name, raising=False)

    assert emr_run.main(["--job", "parse_filings"]) == 2
    assert "missing" in capsys.readouterr().err


# -- review fixes: the Makefile recipe, the zip, the request shape -------------------------

STUB = '#!/bin/sh\necho "$(basename $0) $*" >> "$STUB_LOG"\ncat >/dev/null 2>&1 || true\n'
ECR_URL = "123456789012.dkr.ecr.us-west-2.amazonaws.com/edgarqa-emr"


def _stub_bin(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("aws", "docker"):
        (bin_dir / name).write_text(STUB)
        (bin_dir / name).chmod(0o755)
    return bin_dir, tmp_path / "stub.log"


def _clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text((REPO / "Makefile").read_text())
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "Makefile"],
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    return repo


def _make(repo: Path, tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir, log = (
        _stub_bin(tmp_path)
        if not (tmp_path / "bin").exists()
        else (tmp_path / "bin", tmp_path / "stub.log")
    )
    done = subprocess.run(
        ["make", "-s", "emr-image-push", f"ECR_URL={ECR_URL}", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(log), "HOME": str(tmp_path)},
    )
    return done, log.read_text() if log.exists() else ""


@pytest.mark.parametrize("yes", [[], ["YES="], ["YES=0"], ["YES=no"], ["YES=true"]])
def test_the_image_push_only_pushes_with_exactly_yes_1(tmp_path: Path, yes: list[str]):
    repo = _clean_repo(tmp_path)

    done, log = _make(repo, tmp_path, *yes)

    assert done.returncode == 0, done.stderr
    assert "dry run" in done.stdout and "us-west-2" in done.stdout
    assert log == "", "a dry run must not call aws or docker"


def test_yes_1_logs_in_with_the_region_of_the_url_then_tags_and_pushes(tmp_path: Path):
    repo = _clean_repo(tmp_path)

    done, log = _make(repo, tmp_path, "YES=1")

    assert done.returncode == 0, done.stderr
    calls = log.splitlines()
    # the two ends of the login pipe start together, so their log order is not defined
    assert "aws ecr get-login-password --region us-west-2" in calls[:2]
    assert any(c.startswith("docker login --username AWS") for c in calls[:2])
    assert calls[2].startswith("docker tag edgarqa-emr:") and ECR_URL in calls[2]
    assert calls[3].startswith(f"docker push {ECR_URL}:")


def test_a_dirty_tree_blocks_the_push_because_ecr_tags_are_immutable(tmp_path: Path):
    repo = _clean_repo(tmp_path)
    (repo / "Makefile").write_text((repo / "Makefile").read_text() + "\n# local edit\n")

    done, log = _make(repo, tmp_path, "YES=1")

    assert done.returncode == 2 and "uncommitted changes" in done.stdout
    assert log == ""


@pytest.mark.parametrize(
    ("yes", "expect_yes"), [("YES=1", True), ("YES=0", False), ("YES=", False)]
)
def test_emr_run_adds_yes_only_for_exactly_1(yes: str, expect_yes: bool):
    done = subprocess.run(
        ["make", "-n", "emr-run", "JOB=parse_filings", yes],
        cwd=REPO,
        capture_output=True,
        text=True,
    )

    assert done.returncode == 0
    assert ("--yes" in done.stdout) is expect_yes
    assert "--git-sha $(git rev-parse --short=12 HEAD)" in done.stdout


def test_the_tokenizer_loads_when_the_code_is_only_available_from_the_bundle_zip(tmp_path: Path):
    zip_path = package.build_zip(REPO, tmp_path / "pipelines.zip")
    work = tmp_path / "work"
    work.mkdir()

    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "import pipelines, pipelines.parsing.tokens as t;"
            "print(pipelines.__file__);"
            "print(t.get_tokenizer().count('hello world'))",
            str(zip_path),
        ],
        cwd=work,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )

    assert done.returncode == 0, done.stderr
    lines = done.stdout.split()
    assert ".zip" in lines[0], "the test must import from the zip, not from the repo"
    assert lines[-1] == "2"


def test_extra_job_arguments_are_added_after_env_emr_never_replacing_it(
    capsys: pytest.CaptureFixture[str],
):
    rc = emr_run.main(
        [
            "--job",
            "parse_filings",
            "--application-id",
            "a",
            "--role-arn",
            "r",
            "--bucket",
            "b",
            "--git-sha",
            "s",
            "--arg=--html-glob",
            "--arg=s3://b/raw/filings",
        ]  # fmt: skip
    )

    args = json.loads(capsys.readouterr().out.split("\n\ndry run")[0])["jobDriver"]["sparkSubmit"]
    assert rc == 0
    assert args["entryPointArguments"] == [
        "pipelines.spark.parse_filings", "--env", "emr", "--html-glob", "s3://b/raw/filings",
    ]  # fmt: skip


def test_package_upload_refuses_a_dirty_tree(tmp_path: Path):
    repo = _clean_repo(tmp_path)
    (repo / "pipelines" / "cloud").mkdir(parents=True)
    (repo / "pipelines" / "cloud" / "entrypoint.py").write_text("x = 1\n")
    (repo / "pipelines" / "__init__.py").write_text("")
    subprocess.run(["git", "add", "pipelines"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "p"],
        cwd=repo, check=True, capture_output=True,
    )  # fmt: skip
    (repo / "pipelines" / "__init__.py").write_text("# dirty\n")

    assert (
        package.main(["--root", str(repo), "--out", str(tmp_path / "o"), "--upload", "s3://b/a"])
        == 2
    )


def test_the_run_request_is_accepted_by_the_emr_serverless_api_model(tmp_path: Path):
    """moto validates the request against botocore's service model, so a misspelt or mistyped
    field (a real run would be rejected the same way) fails here."""
    with mock_aws():
        client = boto3.client("emr-serverless", region_name="us-east-1")
        app = client.create_application(
            name="edgarqa-spark", releaseLabel="emr-7.14.0", type="SPARK", architecture="ARM64"
        )
        cfg = RunConfig(
            app["applicationId"],
            "arn:aws:iam::123456789012:role/edgarqa-emr-job",
            "lake-b",
            "abc123def456",
            "parse_filings",
        )

        started = client.start_job_run(**build_request(cfg))
        run = client.get_job_run(applicationId=cfg.application_id, jobRunId=started["jobRunId"])

    assert run["jobRun"]["name"] == "parse_filings-abc123def456"
