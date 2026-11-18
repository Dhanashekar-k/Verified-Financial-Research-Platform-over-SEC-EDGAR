"""Static checks on the EMR image and its Terraform (write-only cloud code, CLAUDE.md rule 8).

They cannot prove the image or the modules work in AWS (nothing here runs against an account);
they pin the properties that are easy to break by accident: a digest-pinned base, requirement pins
that equal the lock, and the guardrails the project rules demand (cost caps, idle stop, pull
policy, no wildcard resources on the lake).
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = (REPO / "infra/emr/Dockerfile").read_text()
EMR_TF = (REPO / "infra/terraform/modules/emr/main.tf").read_text()
ECR_TF = (REPO / "infra/terraform/modules/ecr/main.tf").read_text()
EMR_VARS = (REPO / "infra/terraform/modules/emr/variables.tf").read_text()


def test_the_base_image_is_pinned_by_digest_not_a_tag():
    froms = re.findall(r"^FROM\s+(\S+)", DOCKERFILE, re.MULTILINE)

    assert len(froms) == 1
    assert re.fullmatch(
        r"public\.ecr\.aws/emr-serverless/spark/emr-7\.14\.0@sha256:[0-9a-f]{64}", froms[0]
    )
    assert ":latest" not in DOCKERFILE


def test_the_dockerfile_installs_python_312_and_runs_as_the_hadoop_user():
    assert "python3.12" in DOCKERFILE
    assert DOCKERFILE.strip().splitlines()[-1] == "USER hadoop:hadoop"


def test_the_release_label_matches_the_image_base():
    release = re.search(r'variable "release_label".*?default\s*=\s*"([^"]+)"', EMR_VARS, re.DOTALL)

    assert release is not None and release.group(1) == "emr-7.14.0"
    assert "emr-7.14.0@sha256" in DOCKERFILE


def test_every_pin_in_requirements_emr_equals_the_installed_lock_version():
    lines = [
        ln
        for ln in (REPO / "infra/emr/requirements-emr.txt").read_text().splitlines()
        if ln and not ln.startswith("#")
    ]

    assert len(lines) > 10
    for line in lines:
        name, version = line.split("==")
        assert metadata.version(name) == version, (
            f"{name} drifted from the lock; rerun make_requirements.py"
        )


def test_the_executors_get_the_packages_the_parse_job_imports():
    names = {
        ln.split("==")[0]
        for ln in (REPO / "infra/emr/requirements-emr.txt").read_text().splitlines()
        if "==" in ln
    }

    assert {
        "pydantic",
        "selectolax",
        "tokenizers",
        "pymupdf",
        "pandas",
        "pyarrow",
        "setuptools",
    } <= names


def test_the_emr_application_is_arm64_capped_and_stops_when_idle():
    assert 'architecture  = "ARM64"' in EMR_TF
    assert "auto_stop_configuration" in EMR_TF and "idle_timeout_minutes" in EMR_TF
    assert "maximum_capacity" in EMR_TF
    assert re.search(r'variable "max_vcpu".*?default\s*=\s*64', EMR_VARS, re.DOTALL)
    assert re.search(r'variable "idle_timeout_minutes".*?default\s*=\s*5\b', EMR_VARS, re.DOTALL)


def test_the_job_role_is_scoped_to_the_lake_bucket_and_the_lake_glue_databases():
    assert '"*"' not in re.sub(r"\$\{[^}]*\}/\*", "", EMR_TF).replace(
        '"${local.lake_bucket_arn}/*"', ""
    )
    assert "local.lake_bucket_arn" in EMR_TF
    assert "database/${db}" in EMR_TF and "table/${db}/*" in EMR_TF
    assert "values   = [var.account_id]" in EMR_TF  # the trust policy checks aws:SourceAccount


def test_ecr_tags_are_immutable_and_scanned_and_only_emr_serverless_may_pull():
    assert 'image_tag_mutability = "IMMUTABLE"' in ECR_TF
    assert "scan_on_push = true" in ECR_TF
    assert 'identifiers = ["emr-serverless.amazonaws.com"]' in ECR_TF
    assert "aws:SourceArn" in ECR_TF
    for write in ("ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:*"):
        assert write not in ECR_TF


def test_no_nat_gateway_or_load_balancer_anywhere_in_the_new_modules():
    for text in (EMR_TF, ECR_TF):
        assert "aws_nat_gateway" not in text and "aws_lb" not in text
