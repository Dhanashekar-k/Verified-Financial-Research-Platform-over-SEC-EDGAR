"""A tiny object store over a local directory or an S3 prefix (M02 section 3.2).

    store = open_store("data/raw/filings")            # LocalStore
    store = open_store("s3://<lake>/raw/filings")     # S3Store (credentials from the environment)

Both stores have the same four operations, so a job runs on a laptop and in AWS unchanged. Keys are
relative POSIX paths without `..`, empty or absolute parts. Writes are atomic: a local write goes
through a temp file in the same directory, an S3 put is atomic by itself.

Tests use moto; the cloud-facing code is exercised here and never run against a real account by
Claude (CLAUDE.md rule 8).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Protocol

from botocore.exceptions import ClientError

_NOT_FOUND = {"404", "NoSuchKey", "NotFound"}
_DENIED = {"403", "AccessDenied"}


class ObjectStore(Protocol):
    def exists(self, key: str) -> bool: ...
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def uri(self, key: str) -> str: ...


def check_key(key: str) -> str:
    parts = key.split("/")
    if not key or key.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ValueError(
            f"bad object key {key!r}: must be a relative path without empty or .. parts"
        )
    return key


class LocalStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None

    def uri(self, key: str) -> str:
        return self._path(key).resolve().as_uri()


class S3Store:
    def __init__(self, bucket: str, prefix: str = "", client: Any = None) -> None:
        if not bucket:
            raise ValueError("an S3 store needs a bucket")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import boto3  # imported late: local runs and unit tests need no AWS setup

            client = boto3.client("s3")
        self._client = client

    def _key(self, key: str) -> str:
        check_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def _denied(self, key: str, exc: ClientError) -> PermissionError:
        # Without s3:ListBucket, S3 answers 403 instead of 404 for a missing key, so a denied
        # read and an absent object look the same. Say which permissions are needed.
        return PermissionError(
            f"access denied reading s3://{self.bucket}/{self._key(key)}: the role needs "
            "s3:GetObject on the prefix and s3:ListBucket on the bucket (without ListBucket a "
            f"missing key is a 403, not a 404): {exc}"
        )

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code"))
            if code in _NOT_FOUND:
                return False
            if code in _DENIED:
                raise self._denied(key, exc) from exc
            raise
        return True

    def put(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get(self, key: str) -> bytes:
        try:
            body = self._client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"]
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code"))
            if code in _NOT_FOUND:
                raise KeyError(key) from None
            if code in _DENIED:
                raise self._denied(key, exc) from exc
            raise
        data: bytes = body.read()
        return data

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"


def open_store(location: str) -> ObjectStore:
    """`s3://bucket/prefix` gives an S3Store; anything else is a local directory."""
    if location.startswith("s3://"):
        rest = location[len("s3://") :]
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise ValueError(f"S3 location {location!r} has no bucket")
        return S3Store(bucket, prefix)
    return LocalStore(location)
