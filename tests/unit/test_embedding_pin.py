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


def test_sorting_by_length_changes_neither_the_vectors_nor_their_order(model_dir: Path):
    embedder = get_embedder(model_dir)
    texts = [
        "short",
        "A much longer passage about supply chain risk. " * 12,
        "mid length text about revenue growth in the quarter",
        "",
        "Another long one about gross margin and operating expenses. " * 9,
        "tiny",
    ]

    sorted_v = embedder.embed_passages(texts, batch_size=2, sort_by_length=True)
    plain_v = embedder.embed_passages(texts, batch_size=2, sort_by_length=False)

    assert sorted_v.shape == plain_v.shape == (len(texts), 384)
    cos = (sorted_v * plain_v).sum(axis=1)
    assert cos.min() >= 0.99999  # row i is still the embedding of texts[i]


def test_equal_length_texts_keep_their_own_vectors(model_dir: Path):
    embedder = get_embedder(model_dir)
    texts = ["aaaa bbbb", "cccc dddd", "eeee ffff", "gggg hhhh"]  # four texts of equal length

    both = embedder.embed_passages(texts, batch_size=2)
    singly = np.stack([embedder.embed_passages([t])[0] for t in texts])

    assert (both * singly).sum(axis=1).min() >= 0.99999


def test_a_single_text_and_the_query_path_survive_sorting(model_dir: Path):
    embedder = get_embedder(model_dir)
    questions = ["what drove margin", "how large is the dividend and when is it paid out", "risk"]

    sorted_q = embedder.embed_queries(questions, batch_size=2, sort_by_length=True)
    plain_q = embedder.embed_queries(questions, batch_size=2, sort_by_length=False)
    one = embedder.embed_passages(["only one text"])

    assert (sorted_q * plain_q).sum(axis=1).min() >= 0.99999
    assert one.shape == (1, 384)
