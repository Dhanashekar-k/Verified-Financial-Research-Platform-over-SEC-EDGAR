"""A file location that is either a local path or an `s3://bucket/key` URI (small reference files).

The Spark jobs read a few small inputs (the ticker map, the S&P 500 CSV, the FinanceBench question
file and company CSV). Locally they are files under `data/`; on AWS Glue they are staged in the lake
bucket. These helpers read both the same way. Large inputs go through Spark readers, not these.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import boto3

Location = Path | str  # a local path, or an `s3://bucket/key` URI

_NOT_FOUND = {"404", "NoSuchKey", "NotFound"}


def is_s3(location: Location) -> bool:
    return str(location).startswith("s3://")


def join(base: Location, *parts: str) -> str:
    """`base/part/...` as a string (a `Path` would collapse the `//` of an `s3://` URI)."""
    return "/".join([str(base).rstrip("/"), *parts])


def basename(location: Location) -> str:
    return str(location).rstrip("/").rsplit("/", 1)[-1]


def _split(uri: str) -> tuple[Any, str, str]:
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return boto3.client("s3"), bucket, key


def exists(location: Location) -> bool:
    if not is_s3(location):
        return Path(location).is_file()
    client, bucket, key = _split(str(location))
    try:
        client.head_object(Bucket=bucket, Key=key)
    except client.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] in _NOT_FOUND:
            return False
        raise
    return True


def read_bytes(location: Location) -> bytes:
    if not is_s3(location):
        return Path(location).read_bytes()
    client, bucket, key = _split(str(location))
    body: bytes = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    return body


def read_text(location: Location, encoding: str = "utf-8") -> str:
    return read_bytes(location).decode(encoding)


def newest(directory: Location, pattern: str) -> Location:
    """The sorted-last file in `directory` matching the glob `pattern` (names embed an ISO date).

    Raises FileNotFoundError when nothing matches.
    """
    if not is_s3(directory):
        found = sorted(Path(directory).glob(pattern))
        if not found:
            raise FileNotFoundError(f"no {pattern} in {directory}")
        return found[-1]
    from fnmatch import fnmatch

    client, bucket, prefix = _split(str(directory).rstrip("/") + "/")
    names: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"][len(prefix) :]
            if "/" not in name and fnmatch(name, pattern):
                names.append(name)
    if not names:
        raise FileNotFoundError(f"no {pattern} in {directory}")
    return join(directory, sorted(names)[-1])
