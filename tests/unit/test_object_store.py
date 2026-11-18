"""Unit tests for ``pipelines/common/object_store.py`` (local directory or S3 prefix).

The S3 store runs against moto's in-process S3 (rule 4: no real AWS call). The same behavioural
tests run on both stores, so the fetcher can move between a laptop directory and a bucket.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from pipelines.common.object_store import LocalStore, ObjectStore, S3Store, open_store


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture(params=["local", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ObjectStore]:
    if request.param == "local":
        yield LocalStore(tmp_path / "root")
    else:
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="lake")
            yield S3Store("lake", "raw/filings", client=client)


def test_put_then_get_round_trips_bytes(store: ObjectStore):
    store.put("320193/0000320193-23-000106.htm.gz", b"\x1f\x8bpayload")

    assert store.get("320193/0000320193-23-000106.htm.gz") == b"\x1f\x8bpayload"


def test_exists_is_false_before_and_true_after_put(store: ObjectStore):
    assert not store.exists("a/b.bin")

    store.put("a/b.bin", b"x")

    assert store.exists("a/b.bin")


def test_put_overwrites_with_the_new_bytes(store: ObjectStore):
    store.put("k", b"one")
    store.put("k", b"two")

    assert store.get("k") == b"two"


def test_get_of_a_missing_key_raises_keyerror(store: ObjectStore):
    with pytest.raises(KeyError):
        store.get("missing")


def test_uri_names_the_store_and_the_key(store: ObjectStore, tmp_path: Path):
    uri = store.uri("a/b.bin")

    assert uri.endswith("a/b.bin")
    assert uri.startswith(("file://", "s3://lake/raw/filings/"))


@pytest.mark.parametrize("key", ["../escape", "/abs/path", "a/../../b", "", "a//b"])
def test_bad_keys_are_rejected_by_both_stores(store: ObjectStore, key: str):
    with pytest.raises(ValueError, match="key"):
        store.put(key, b"x")


def test_local_put_is_atomic_and_world_readable(tmp_path: Path):
    local = LocalStore(tmp_path / "root")

    local.put("a/b.bin", b"x")

    files = [p.name for p in (tmp_path / "root" / "a").iterdir()]
    assert files == ["b.bin"]
    assert (tmp_path / "root" / "a" / "b.bin").stat().st_mode & 0o777 == 0o644


def test_local_put_failure_leaves_no_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    local = LocalStore(tmp_path / "root")

    def boom(*a: object, **k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        local.put("a/b.bin", b"x")

    assert list((tmp_path / "root").rglob("*.bin")) == []
    assert [p for p in (tmp_path / "root" / "a").iterdir()] == []


def test_s3_store_applies_its_prefix_in_the_bucket():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="lake")
        s3 = S3Store("lake", "raw/filings/", client=client)  # trailing slash is tolerated

        s3.put("320193/x.htm.gz", b"x")

        keys = [o["Key"] for o in client.list_objects_v2(Bucket="lake")["Contents"]]
        assert keys == ["raw/filings/320193/x.htm.gz"]


def test_s3_exists_reraises_errors_other_than_not_found():
    """A 403 means we cannot tell; treating it as 'missing' would re-download forever."""

    class Denied:
        def head_object(self, **kwargs: object) -> None:
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")

    s3 = S3Store("lake", "p", client=Denied())

    with pytest.raises(PermissionError, match="s3:ListBucket"):
        s3.exists("k")


def test_s3_get_denied_says_which_permissions_are_missing_and_other_errors_pass_through():
    class Denied:
        def get_object(self, **kwargs: object) -> None:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "GetObject")

    class Throttled:
        def head_object(self, **kwargs: object) -> None:
            raise ClientError({"Error": {"Code": "SlowDown", "Message": "x"}}, "HeadObject")

    with pytest.raises(PermissionError, match="s3:GetObject"):
        S3Store("lake", "p", client=Denied()).get("k")
    with pytest.raises(ClientError):  # not a permission problem: left as it is
        S3Store("lake", "p", client=Throttled()).exists("k")


def test_open_store_picks_the_implementation_from_the_location(tmp_path: Path):
    assert isinstance(open_store(str(tmp_path / "x")), LocalStore)
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="lake")
        assert isinstance(open_store("s3://lake/raw/filings"), S3Store)


def test_open_store_rejects_an_s3_location_without_a_bucket():
    with pytest.raises(ValueError, match="bucket"):
        open_store("s3:///prefix")
