"""Unit tests for staging and fetching the embedding model directory (`model_store`, ADR-032).

The store is a local directory (`open_store` treats any non-`s3://` location as one), so no cloud
call happens. The files are small stand-ins: these tests are about file handling, not the model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipelines.common.embedding import ARTIFACT_ONNX_FILE, ModelIntegrityError, sha256_file
from pipelines.common.model_store import (
    MANIFEST_NAME,
    describe_dir,
    fetch_model_dir,
    upload_model_dir,
    write_manifest,
)


@pytest.fixture
def model_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / ARTIFACT_ONNX_FILE).write_bytes(b"onnx-bytes" * 1000)
    (src / "tokenizer.json").write_text('{"a": 1}', encoding="utf-8")
    (src / "sub" / "config.json").write_text("{}", encoding="utf-8")
    return src


def test_describe_dir_lists_regular_files_with_size_and_sha(model_src: Path):
    files = describe_dir(model_src)

    assert set(files) == {ARTIFACT_ONNX_FILE, "tokenizer.json", "sub/config.json"}
    assert files[ARTIFACT_ONNX_FILE]["bytes"] == 10_000
    assert files[ARTIFACT_ONNX_FILE]["sha256"] == sha256_file(model_src / ARTIFACT_ONNX_FILE)


def test_describe_dir_skips_hidden_files_and_the_manifest(model_src: Path):
    (model_src / ".cache").mkdir()
    (model_src / ".cache" / "lock").write_text("x", encoding="utf-8")
    (model_src / ".DS_Store").write_text("x", encoding="utf-8")
    (model_src / MANIFEST_NAME).write_text("{}", encoding="utf-8")

    assert set(describe_dir(model_src)) == {ARTIFACT_ONNX_FILE, "tokenizer.json", "sub/config.json"}


def test_write_manifest_requires_the_onnx_file(tmp_path: Path):
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ModelIntegrityError):
        write_manifest(tmp_path)


def test_write_manifest_is_valid_json_with_the_file_table(model_src: Path):
    files = write_manifest(model_src)

    assert json.loads((model_src / MANIFEST_NAME).read_text(encoding="utf-8")) == {"files": files}


def test_upload_then_fetch_round_trips_every_file(model_src: Path, tmp_path: Path):
    store = tmp_path / "store"
    keys = upload_model_dir(model_src, str(store))
    dest = tmp_path / "dest"

    out = fetch_model_dir(str(store), dest)

    assert out == dest
    assert set(keys) == {ARTIFACT_ONNX_FILE, "tokenizer.json", "sub/config.json", MANIFEST_NAME}
    for name in describe_dir(model_src):
        assert sha256_file(dest / name) == sha256_file(model_src / name)


def test_fetch_leaves_files_that_already_match(model_src: Path, tmp_path: Path):
    store = tmp_path / "store"
    upload_model_dir(model_src, str(store))
    dest = tmp_path / "dest"
    fetch_model_dir(str(store), dest)
    before = {p.name: p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()}

    fetch_model_dir(str(store), dest)

    after = {p.name: p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()}
    assert after == before


def test_fetch_repairs_a_corrupt_local_file(model_src: Path, tmp_path: Path):
    store = tmp_path / "store"
    upload_model_dir(model_src, str(store))
    dest = tmp_path / "dest"
    fetch_model_dir(str(store), dest)
    (dest / ARTIFACT_ONNX_FILE).write_bytes(b"truncated")

    fetch_model_dir(str(store), dest)

    assert sha256_file(dest / ARTIFACT_ONNX_FILE) == sha256_file(model_src / ARTIFACT_ONNX_FILE)


def test_fetch_rejects_an_object_that_does_not_match_the_manifest(model_src: Path, tmp_path: Path):
    store = tmp_path / "store"
    upload_model_dir(model_src, str(store))
    (store / ARTIFACT_ONNX_FILE).write_bytes(b"someone overwrote the staged model")

    with pytest.raises(ModelIntegrityError):
        fetch_model_dir(str(store), tmp_path / "dest")


def test_fetch_leaves_no_temporary_files_behind(model_src: Path, tmp_path: Path):
    store = tmp_path / "store"
    upload_model_dir(model_src, str(store))
    dest = tmp_path / "dest"

    fetch_model_dir(str(store), dest)

    assert [p for p in dest.rglob("*") if p.name.startswith(".")] == []


def test_a_bad_staged_object_never_appears_in_dest(model_src: Path, tmp_path: Path):
    store = tmp_path / "store"
    upload_model_dir(model_src, str(store))
    (store / ARTIFACT_ONNX_FILE).write_bytes(b"someone overwrote the staged model")
    dest = tmp_path / "dest"

    with pytest.raises(ModelIntegrityError):
        fetch_model_dir(str(store), dest)

    assert not (dest / ARTIFACT_ONNX_FILE).exists()
    assert [p for p in dest.rglob("*") if p.name.startswith(".")] == []


def test_fetch_checks_the_manifest_against_the_pin(model_src: Path, tmp_path: Path):
    store = tmp_path / "store"
    upload_model_dir(model_src, str(store))
    pinned = sha256_file(model_src / ARTIFACT_ONNX_FILE)

    fetch_model_dir(str(store), tmp_path / "ok", expected_onnx_sha256=pinned)
    with pytest.raises(ModelIntegrityError):
        fetch_model_dir(str(store), tmp_path / "bad", expected_onnx_sha256="0" * 64)

    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("name", ["../escape.txt", "/abs/escape.txt", "a/../../escape.txt"])
def test_fetch_rejects_manifest_names_that_leave_the_directory(
    model_src: Path, tmp_path: Path, name: str
):
    store = tmp_path / "store"
    upload_model_dir(model_src, str(store))
    manifest = json.loads((store / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["files"][name] = manifest["files"]["tokenizer.json"]
    (store / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    dest = tmp_path / "dest"

    with pytest.raises(ModelIntegrityError):
        fetch_model_dir(str(store), dest)

    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path.parent / "escape.txt").exists()
