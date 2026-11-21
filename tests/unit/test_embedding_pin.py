"""The model on disk is the pinned artifact, and the cache never skips a requested pin (ADR-032).

These tests use the real quantized model from the local model dir (`make fetch-model`); a missing
dir fails the test, it does not skip it.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from pipelines.common.embedding import (
    ARTIFACT_HF_REVISION,
    ARTIFACT_ONNX_FILE,
    ARTIFACT_ONNX_SHA256,
    ModelIntegrityError,
    get_embedder,
    sha256_file,
)
from pipelines.parsing.tokens import TOKENIZER_SHA256


@pytest.fixture(scope="session")
def model_dir() -> Path:
    path = Path(os.environ.get("EDGARQA_EMBED_MODEL_DIR", "data/models/bge-small-en-v1.5"))
    if not (path / ARTIFACT_ONNX_FILE).is_file():
        pytest.fail(f"embedding model not found at {path}: run `make fetch-model`")
    return path


def test_the_local_onnx_file_is_the_pinned_artifact(model_dir: Path):
    assert sha256_file(model_dir / ARTIFACT_ONNX_FILE) == ARTIFACT_ONNX_SHA256


def test_the_model_tokenizer_is_the_one_that_counted_the_chunks(model_dir: Path):
    # `n_tokens` in curated.chunks comes from the bundled tokenizer; if the model's own tokenizer
    # differed, the "no chunk above 400 tokens" guarantee against the 512 limit would not hold.
    assert sha256_file(model_dir / "tokenizer.json") == TOKENIZER_SHA256


def test_the_pinned_revision_is_a_full_commit_sha():
    assert len(ARTIFACT_HF_REVISION) == 40
    assert set(ARTIFACT_HF_REVISION) <= set("0123456789abcdef")


def test_queries_without_the_instruction_equal_passages(model_dir: Path):
    embedder = get_embedder(model_dir)
    q = "what drove the change in gross margin"

    plain = embedder.embed_queries([q], instruction=False)[0]
    passage = embedder.embed_passages([q])[0]

    assert float(np.dot(plain, passage)) >= 0.99999


def test_a_requested_pin_is_never_skipped_by_the_cache(model_dir: Path):
    get_embedder(model_dir)  # an unverified instance is now cached

    with pytest.raises(ModelIntegrityError):
        get_embedder(model_dir, expected_onnx_sha256="0" * 64)


def test_a_verified_embedder_is_cached_and_works(model_dir: Path):
    first = get_embedder(model_dir, expected_onnx_sha256=ARTIFACT_ONNX_SHA256)
    second = get_embedder(model_dir, expected_onnx_sha256=ARTIFACT_ONNX_SHA256)

    assert first is second
    assert first.embed_passages(["a sentence"]).shape == (1, 384)


def test_a_relative_and_an_absolute_path_share_one_embedder(model_dir: Path):
    assert get_embedder(model_dir) is get_embedder(model_dir.resolve())
