"""Stage the embedding model directory in an object store and fetch it back (ADR-032).

Glue has no custom image, so the model files live under one prefix
(`s3://<lake>/artifacts/models/bge-small-en-v1.5/`) next to a `MODEL_FILES.json` listing every
file's size and sha256. A Glue Python worker calls `fetch_model_dir` once; a file that is already
present with the right sha256 is not fetched again, so a rerun, or a second worker on the same
node, does no work.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath

from pipelines.common.embedding import ARTIFACT_ONNX_FILE, ModelIntegrityError, sha256_file
from pipelines.common.object_store import open_store

MANIFEST_NAME = "MODEL_FILES.json"


def describe_dir(model_dir: Path) -> dict[str, dict[str, int | str]]:
    """`{name: {bytes, sha256}}` of every regular file, without hidden files and the manifest."""
    root = Path(model_dir)
    files: dict[str, dict[str, int | str]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if (
            not path.is_file()
            or rel.name == MANIFEST_NAME
            or any(p.startswith(".") for p in rel.parts)
        ):
            continue
        files[rel.as_posix()] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return files


def write_manifest(model_dir: Path) -> dict[str, dict[str, int | str]]:
    """Write `MODEL_FILES.json` into `model_dir` and return the file table."""
    files = describe_dir(model_dir)
    if ARTIFACT_ONNX_FILE not in files:
        raise ModelIntegrityError(f"{ARTIFACT_ONNX_FILE} not found in {model_dir}")
    text = json.dumps({"files": files}, indent=2, sort_keys=True) + "\n"
    (Path(model_dir) / MANIFEST_NAME).write_text(text, encoding="utf-8")
    return files


def upload_model_dir(model_dir: Path, location: str) -> list[str]:
    """Copy every file of `model_dir` (and its manifest) to `location`; returns the keys written."""
    write_manifest(model_dir)
    store = open_store(location)
    keys = [*describe_dir(model_dir), MANIFEST_NAME]
    for key in keys:
        store.put(key, (Path(model_dir) / key).read_bytes())
    return keys


def _safe_name(name: str) -> str:
    """A manifest name must stay inside the model directory."""
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or ".." in parts:
        raise ModelIntegrityError(f"unsafe file name in {MANIFEST_NAME}: {name!r}")
    return name


def fetch_model_dir(location: str, dest: Path, *, expected_onnx_sha256: str | None = None) -> Path:
    """Make `dest` hold the staged model's files, each verified by sha256; returns `dest`.

    The manifest lives next to the files it describes, so it cannot vouch for itself:
    `expected_onnx_sha256` (the pin in `embedding.ARTIFACT_ONNX_SHA256`) is checked against the
    manifest entry before anything is written. A fetched file is verified BEFORE it is renamed into
    place, so a bad object never appears in `dest`.
    """
    store = open_store(location)
    listing = json.loads(store.get(MANIFEST_NAME))["files"]
    if ARTIFACT_ONNX_FILE not in listing:
        raise ModelIntegrityError(f"{location} has no {ARTIFACT_ONNX_FILE}")
    if (
        expected_onnx_sha256 is not None
        and listing[ARTIFACT_ONNX_FILE]["sha256"] != expected_onnx_sha256
    ):
        raise ModelIntegrityError(
            f"{location}: staged {ARTIFACT_ONNX_FILE} sha256 "
            f"{listing[ARTIFACT_ONNX_FILE]['sha256']}"
            f" != pinned {expected_onnx_sha256}"
        )
    root = Path(dest)
    for name, meta in listing.items():
        target = root / _safe_name(name)
        if target.is_file() and sha256_file(target) == meta["sha256"]:
            continue
        data = store.get(name)
        actual = hashlib.sha256(data).hexdigest()
        if actual != meta["sha256"]:
            raise ModelIntegrityError(f"{name}: sha256 {actual} != staged {meta['sha256']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary name then rename, so two workers on one node never read a half file.
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    return root
