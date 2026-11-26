"""Tests for ``pipelines.common.embedding`` (test-first; the module does not exist yet).

The ONNX embedding model is a LOCAL artifact, like the JVM: it is never faked. The
``model_dir`` fixture fails (not skips) when the artifact has not been fetched, so a
green run always means the real model was exercised.

Pure helpers are tested without the model. ``Embedder`` behaviours are tested
against the real quantized bge-small model, so the instruction-prefix contract
(passages never get it, queries always do) is proved behaviourally, not with mocks.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from pipelines.common import embedding
from pipelines.common.embedding import (
    ARTIFACT_HF_REPO,
    ARTIFACT_ONNX_FILE,
    DIM,
    MODEL_NAME,
    QUERY_INSTRUCTION,
    Embedder,
    ModelIntegrityError,
    batched,
    get_embedder,
    l2_normalize,
    query_text,
    sha256_file,
    verify_model_dir,
)

QUERY = "what are the supply chain risks"
SUPPLY_PASSAGE = (
    "We depend on a limited number of contract manufacturers and suppliers, "
    "and disruptions in our supply chain could harm our business."
)
DIVIDEND_PASSAGE = "The company declared a quarterly dividend of $0.25 per share payable in March."

SAMPLE_TEXTS = [
    "Revenue increased 12% year over year driven by higher subscription volume.",
    "We are exposed to foreign currency exchange rate fluctuations.",
    "Item 1A. Risk Factors",
    "The Company's principal executive offices are located in Cupertino, California.",
    "Net cash provided by operating activities was $4.2 billion.",
    "We may be unable to protect our intellectual property rights.",
    "Our credit facility contains covenants that restrict our operations.",
    "Inventory write-downs reduced gross margin by 40 basis points.",
    "The Board authorized a share repurchase program of up to $10 billion.",
    "Cybersecurity incidents could disrupt our operations and damage our reputation.",
    "Goodwill is tested for impairment annually in the fourth quarter.",
    "Fiscal 2023 included a 53rd week.",
    "We lease office space under non-cancelable operating leases.",
    "Competition in our industry is intense and may intensify.",
    "Our effective tax rate was 14.7% compared with 16.2% in the prior year.",
    "Legal proceedings are described in Note 12 to the consolidated financial statements.",
    "Changes in regulation could increase our compliance costs.",
    "Segment operating income decreased due to higher input costs.",
    "We have identified no material weaknesses in internal control over financial reporting.",
    "Dividends are paid quarterly at the discretion of the Board.",
]


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def model_dir() -> Path:
    """Resolve the local ONNX model directory; FAIL (never skip) if it is absent."""
    path = Path(os.environ.get("EDGARQA_EMBED_MODEL_DIR", "data/models/bge-small-en-v1.5"))
    if not path.is_dir() or not (path / ARTIFACT_ONNX_FILE).is_file():
        pytest.fail(f"embedding model not found at {path}: run `make fetch-model`")
    return path


@pytest.fixture(scope="session")
def embedder(model_dir: Path) -> Embedder:
    """One real single-threaded Embedder shared by the model-backed tests."""
    return Embedder(model_dir, threads=1)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    a64 = a.astype(np.float64)
    b64 = b.astype(np.float64)
    return float(a64 @ b64 / (np.linalg.norm(a64) * np.linalg.norm(b64)))


# --------------------------------------------------------------------------- constants


def test_constants_match_the_pinned_artifact() -> None:
    """Constants pin the model identity; a silent change would invalidate stored vectors."""
    assert MODEL_NAME == "BAAI/bge-small-en-v1.5"
    assert DIM == 384
    assert QUERY_INSTRUCTION == "Represent this sentence for searching relevant passages: "
    assert ARTIFACT_HF_REPO == "Qdrant/bge-small-en-v1.5-onnx-Q"
    assert ARTIFACT_ONNX_FILE == "model_optimized.onnx"


def test_model_integrity_error_is_an_exception() -> None:
    """ModelIntegrityError must be a real exception type so callers can catch it."""
    assert issubclass(ModelIntegrityError, Exception)


# --------------------------------------------------------------------------- query_text


def test_query_text_prepends_instruction_only() -> None:
    """query_text is exactly instruction + question: nothing stripped, lowercased or appended."""
    assert query_text(QUERY) == QUERY_INSTRUCTION + QUERY


@given(st.text())
def test_query_text_is_pure_concatenation(question: str) -> None:
    """For any string (whitespace, unicode, empty) the question is preserved byte for byte."""
    out = query_text(question)
    assert out.startswith(QUERY_INSTRUCTION)
    assert out[len(QUERY_INSTRUCTION) :] == question


def test_query_text_empty_question_is_just_the_instruction() -> None:
    """Boundary: an empty question yields the bare instruction (trailing space kept)."""
    assert query_text("") == QUERY_INSTRUCTION


# --------------------------------------------------------------------------- l2_normalize


def test_l2_normalize_unit_rows_float32() -> None:
    """Every nonzero row ends with norm 1 (within 1e-6) and the dtype is float32."""
    rng = np.random.default_rng(0)
    out = l2_normalize(rng.normal(size=(8, DIM)).astype(np.float32))
    assert out.dtype == np.float32
    assert out.shape == (8, DIM)
    np.testing.assert_allclose(np.linalg.norm(out.astype(np.float64), axis=1), 1.0, atol=1e-6)


def test_l2_normalize_float64_input_returns_float32() -> None:
    """Float64 input is accepted but the output contract is always float32."""
    out = l2_normalize(np.array([[3.0, 4.0]], dtype=np.float64))
    assert out.dtype == np.float32
    np.testing.assert_allclose(out, [[0.6, 0.8]], atol=1e-6)


def test_l2_normalize_zero_row_stays_zero_no_nan() -> None:
    """An all-zero row must stay all zero (not NaN from 0/0) while other rows are normalized."""
    data = np.array([[0.0, 0.0, 0.0], [3.0, 0.0, 4.0]], dtype=np.float32)
    out = l2_normalize(data)
    assert np.all(np.isfinite(out))
    np.testing.assert_array_equal(out[0], np.zeros(3, dtype=np.float32))
    np.testing.assert_allclose(np.linalg.norm(out[1]), 1.0, atol=1e-6)


def test_l2_normalize_does_not_mutate_input() -> None:
    """The caller's array is left untouched (no in-place normalization)."""
    data = np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float32)
    before = data.copy()
    l2_normalize(data)
    np.testing.assert_array_equal(data, before)


def test_l2_normalize_huge_values_do_not_overflow() -> None:
    """Adversarial: 1e30 squared overflows float32; result must stay finite and unit norm."""
    data = np.array([[1e30, 1e30, 1e30]], dtype=np.float32)
    out = l2_normalize(data)
    assert np.all(np.isfinite(out))
    np.testing.assert_allclose(np.linalg.norm(out.astype(np.float64), axis=1), 1.0, atol=1e-6)


def test_l2_normalize_empty_matrix() -> None:
    """Boundary: zero rows in, zero rows out, same width and float32."""
    out = l2_normalize(np.zeros((0, DIM), dtype=np.float32))
    assert out.shape == (0, DIM)
    assert out.dtype == np.float32


@settings(max_examples=60, deadline=None)
@given(
    hnp.arrays(
        dtype=np.float32,
        shape=hnp.array_shapes(min_dims=2, max_dims=2, min_side=1, max_side=12),
        elements=st.floats(-1e3, 1e3, width=32, allow_nan=False, allow_infinity=False),
    )
)
def test_l2_normalize_property(data: np.ndarray) -> None:
    """For any finite matrix: output finite, float32, rows are unit norm or exactly zero."""
    snapshot = data.copy()
    out = l2_normalize(data)
    np.testing.assert_array_equal(data, snapshot)
    assert out.dtype == np.float32
    assert out.shape == data.shape
    assert np.all(np.isfinite(out))
    norms = np.linalg.norm(out.astype(np.float64), axis=1)
    for row_in, norm in zip(data, norms, strict=True):
        if np.linalg.norm(row_in.astype(np.float64)) == 0.0:
            assert norm == 0.0
        else:
            assert abs(norm - 1.0) < 1e-6


# --------------------------------------------------------------------------- batched


def test_batched_keeps_order_and_last_batch_is_short() -> None:
    """Batches preserve input order; the final batch holds the remainder."""
    out = list(batched(["a", "b", "c", "d", "e"], 2))
    assert out == [["a", "b"], ["c", "d"], ["e"]]


def test_batched_is_an_iterator_of_lists() -> None:
    """Contract: returns a lazy Iterator whose items are lists."""
    result = batched(["a", "b", "c"], 2)
    assert isinstance(result, Iterator)
    assert all(isinstance(b, list) for b in result)


def test_batched_exact_multiple_has_no_empty_tail() -> None:
    """Boundary: when len is a multiple of size there is no trailing empty batch."""
    assert list(batched(["a", "b", "c", "d"], 2)) == [["a", "b"], ["c", "d"]]


def test_batched_size_larger_than_input_yields_single_batch() -> None:
    """Boundary: size > len yields everything in one batch."""
    assert list(batched(["a", "b"], 100)) == [["a", "b"]]


def test_batched_empty_input_yields_nothing() -> None:
    """Empty input produces no batches at all (not one empty batch)."""
    assert list(batched([], 4)) == []


@pytest.mark.parametrize("size", [0, -1, -256])
def test_batched_rejects_non_positive_size(size: int) -> None:
    """size < 1 raises ValueError (a zero size would otherwise loop forever or drop data)."""
    with pytest.raises(ValueError):
        list(batched(["a"], size))


@given(st.lists(st.text(max_size=5), max_size=40), st.integers(min_value=1, max_value=17))
def test_batched_property_concatenation_roundtrip(items: list[str], size: int) -> None:
    """Concatenating batches gives back the input; all but the last are full and none is empty."""
    batches = list(batched(items, size))
    assert [x for b in batches for x in b] == items
    assert all(len(b) == size for b in batches[:-1])
    assert all(1 <= len(b) <= size for b in batches)


# --------------------------------------------------------------------------- sha256 / integrity


def test_sha256_file_matches_hashlib_and_is_hex(tmp_path: Path) -> None:
    """sha256_file returns the lowercase 64-char hex digest of the file contents."""
    path = tmp_path / "f.bin"
    path.write_bytes(b"hello embedding")
    digest = sha256_file(path)
    assert digest == hashlib.sha256(b"hello embedding").hexdigest()
    assert len(digest) == 64


def test_sha256_file_multi_chunk_file(tmp_path: Path) -> None:
    """A file larger than any plausible read chunk hashes correctly (no truncation)."""
    payload = os.urandom(5 * 1024 * 1024 + 17)
    path = tmp_path / "big.bin"
    path.write_bytes(payload)
    assert sha256_file(path) == hashlib.sha256(payload).hexdigest()


def test_sha256_file_empty_file(tmp_path: Path) -> None:
    """Boundary: an empty file hashes to the well-known empty-input digest."""
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    assert sha256_file(path) == hashlib.sha256(b"").hexdigest()


def test_verify_model_dir_accepts_matching_hash(tmp_path: Path) -> None:
    """A matching digest passes silently (returns None)."""
    (tmp_path / ARTIFACT_ONNX_FILE).write_bytes(b"model-bytes")
    expected = hashlib.sha256(b"model-bytes").hexdigest()
    assert verify_model_dir(tmp_path, expected) is None


def test_verify_model_dir_rejects_hash_mismatch(tmp_path: Path) -> None:
    """A tampered or wrong artifact is rejected; its vectors would be incomparable."""
    (tmp_path / ARTIFACT_ONNX_FILE).write_bytes(b"model-bytes")
    wrong = hashlib.sha256(b"other-bytes").hexdigest()
    with pytest.raises(ModelIntegrityError):
        verify_model_dir(tmp_path, wrong)


def test_verify_model_dir_rejects_missing_onnx_file(tmp_path: Path) -> None:
    """A directory without model_optimized.onnx is an integrity failure, not a FileNotFoundError."""
    with pytest.raises(ModelIntegrityError):
        verify_model_dir(tmp_path, hashlib.sha256(b"x").hexdigest())


def test_verify_model_dir_ignores_other_files(tmp_path: Path) -> None:
    """Only model_optimized.onnx is hashed; a different file with the right bytes does not count."""
    (tmp_path / "model.onnx").write_bytes(b"model-bytes")
    with pytest.raises(ModelIntegrityError):
        verify_model_dir(tmp_path, hashlib.sha256(b"model-bytes").hexdigest())


# --------------------------------------------------------------------------- real model


def test_model_dir_artifact_has_companion_files(model_dir: Path) -> None:
    """The fetched directory carries the tokenizer/config files fastembed needs offline."""
    for name in (
        ARTIFACT_ONNX_FILE,
        "tokenizer.json",
        "config.json",
        "special_tokens_map.json",
        "tokenizer_config.json",
    ):
        assert (model_dir / name).is_file(), name


def test_embedder_passages_shape_dtype_and_unit_norm(embedder: Embedder) -> None:
    """Passages come back (n, 384) float32 with L2 norm 1 within 1e-5."""
    out = embedder.embed_passages(SAMPLE_TEXTS[:5], batch_size=2)
    assert out.shape == (5, DIM)
    assert out.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(out.astype(np.float64), axis=1), 1.0, atol=1e-5)


def test_embedder_queries_shape_dtype_and_unit_norm(embedder: Embedder) -> None:
    """Queries share the same shape/dtype/norm contract as passages."""
    out = embedder.embed_queries([QUERY, "who is the CFO"])
    assert out.shape == (2, DIM)
    assert out.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(out.astype(np.float64), axis=1), 1.0, atol=1e-5)


def test_embedder_empty_input_returns_zero_by_dim(embedder: Embedder) -> None:
    """Empty input returns shape (0, 384) float32 rather than raising or returning a 1-D array."""
    for out in (embedder.embed_passages([]), embedder.embed_queries([])):
        assert out.shape == (0, DIM)
        assert out.dtype == np.float32


def test_embedder_is_deterministic(embedder: Embedder) -> None:
    """The same text embedded twice yields bit-identical vectors (no dropout or nondeterminism)."""
    a = embedder.embed_passages([SUPPLY_PASSAGE])
    b = embedder.embed_passages([SUPPLY_PASSAGE])
    np.testing.assert_array_equal(a, b)


def test_embedder_batch_size_invariance(embedder: Embedder) -> None:
    """Padding differs between batch_size 1 and 256, but vectors must agree to cosine >= 0.99999."""
    one = embedder.embed_passages(SAMPLE_TEXTS[:8], batch_size=1)
    many = embedder.embed_passages(SAMPLE_TEXTS[:8], batch_size=256)
    assert one.shape == many.shape == (8, DIM)
    for i in range(8):
        assert cosine(one[i], many[i]) >= 0.99999


def test_embedder_preserves_input_order(embedder: Embedder) -> None:
    """Row i of a batched call corresponds to input i (compared with single-text calls)."""
    batch = embedder.embed_passages(SAMPLE_TEXTS[:4], batch_size=3)
    for i, text in enumerate(SAMPLE_TEXTS[:4]):
        single = embedder.embed_passages([text])[0]
        assert cosine(batch[i], single) >= 0.99999


@pytest.mark.parametrize("text", ["", "   ", "\n\t  \n"], ids=["empty", "spaces", "mixed-ws"])
def test_embedder_empty_and_whitespace_text_is_finite_unit_norm(
    embedder: Embedder, text: str
) -> None:
    """Empty/whitespace chunks must not yield NaN or zero vectors (would break ANN indexes)."""
    for out in (embedder.embed_passages([text]), embedder.embed_queries([text])):
        assert out.shape == (1, DIM)
        assert np.all(np.isfinite(out))
        np.testing.assert_allclose(np.linalg.norm(out.astype(np.float64)), 1.0, atol=1e-5)


def test_embedder_long_chunk_embeds(embedder: Embedder) -> None:
    """A ~400-token chunk (the corpus max) embeds to a finite unit vector."""
    text = " ".join(["Our revenue depends on a limited number of large customers."] * 40)
    out = embedder.embed_passages([text])
    assert out.shape == (1, DIM)
    assert np.all(np.isfinite(out))
    np.testing.assert_allclose(np.linalg.norm(out.astype(np.float64)), 1.0, atol=1e-5)


def test_embedder_over_length_text_is_truncated_not_fatal(embedder: Embedder) -> None:
    """Adversarial: text far past the model's 512-token window must not raise or yield NaN."""
    text = " ".join(["Supply chain disruption risk factor."] * 1500)
    out = embedder.embed_passages([text])
    assert out.shape == (1, DIM)
    assert np.all(np.isfinite(out))


def test_embedder_unicode_text_is_finite(embedder: Embedder) -> None:
    """Non-ASCII filing text (em dashes, currency symbols, CJK) embeds without error."""
    out = embedder.embed_passages(["Revenue — €1.2bn £ ¥ 中文 ™ “quoted”"])
    assert out.shape == (1, DIM)
    assert np.all(np.isfinite(out))


def test_query_embedding_differs_from_passage_embedding(embedder: Embedder) -> None:
    """The instruction is applied for queries only, so the same string embeds differently."""
    q = embedder.embed_queries([QUERY])[0]
    p = embedder.embed_passages([QUERY])[0]
    assert cosine(q, p) < 1 - 1e-4


def test_query_prefix_is_explicit_and_the_only_difference(embedder: Embedder) -> None:
    """Passage-embedding query_text(q) equals query-embedding q: proves passages get no hidden
    prefix, queries get exactly QUERY_INSTRUCTION, and nothing else differs."""
    via_passage = embedder.embed_passages([query_text(QUERY)])[0]
    via_query = embedder.embed_queries([QUERY])[0]
    assert cosine(via_passage, via_query) >= 0.99999


def test_semantic_sanity_supply_chain_query(embedder: Embedder) -> None:
    """The on-topic passage must outscore the unrelated dividend passage for the query."""
    q = embedder.embed_queries([QUERY])[0]
    passages = embedder.embed_passages([SUPPLY_PASSAGE, DIVIDEND_PASSAGE])
    assert cosine(q, passages[0]) > cosine(q, passages[1])


def test_threads_1_and_multi_thread_agree(model_dir: Path, embedder: Embedder) -> None:
    """Thread count is a performance knob only: 20 sample texts agree to cosine >= 0.9999."""
    multi = Embedder(model_dir, threads=4)
    a = embedder.embed_passages(SAMPLE_TEXTS)
    b = multi.embed_passages(SAMPLE_TEXTS)
    assert a.shape == b.shape == (20, DIM)
    for i in range(20):
        assert cosine(a[i], b[i]) >= 0.9999


def test_embedder_default_threads_constructs(model_dir: Path) -> None:
    """The documented default (threads=1, no hash) constructs and embeds."""
    out = Embedder(model_dir).embed_passages(["hello"])
    assert out.shape == (1, DIM)


def test_embedder_accepts_correct_expected_hash(model_dir: Path) -> None:
    """Passing the true sha256 of the onnx file lets the Embedder load normally."""
    expected = sha256_file(model_dir / ARTIFACT_ONNX_FILE)
    out = Embedder(model_dir, expected_onnx_sha256=expected).embed_passages(["hello"])
    assert out.shape == (1, DIM)


def test_embedder_rejects_wrong_expected_hash(model_dir: Path) -> None:
    """A wrong expected hash must fail at construction, before any vector is produced."""
    with pytest.raises(ModelIntegrityError):
        Embedder(model_dir, expected_onnx_sha256="0" * 64)


# --------------------------------------------------------------------------- get_embedder cache


def test_get_embedder_returns_same_object_for_same_args(model_dir: Path) -> None:
    """One model load per process: repeated calls with identical args return the cached instance."""
    first = get_embedder(model_dir, threads=1)
    second = get_embedder(model_dir, threads=1)
    assert first is second
    assert isinstance(first, Embedder)


def test_get_embedder_default_threads_equals_explicit_one(model_dir: Path) -> None:
    """get_embedder(dir) and get_embedder(dir, threads=1) share a cache entry (default is 1)."""
    assert get_embedder(model_dir) is get_embedder(model_dir, threads=1)


def test_get_embedder_different_threads_is_a_different_instance(model_dir: Path) -> None:
    """The cache key includes threads, so a different thread count is a distinct Embedder."""
    assert get_embedder(model_dir, threads=1) is not get_embedder(model_dir, threads=2)


def test_get_embedder_is_usable(model_dir: Path) -> None:
    """The cached embedder is a working Embedder, not a placeholder."""
    out = get_embedder(model_dir).embed_queries([QUERY])
    assert out.shape == (1, DIM)


def test_module_exposes_public_names() -> None:
    """Guard against the module silently dropping part of the spec'd public API."""
    for name in (
        "query_text",
        "l2_normalize",
        "batched",
        "sha256_file",
        "verify_model_dir",
        "Embedder",
        "get_embedder",
        "ModelIntegrityError",
    ):
        assert hasattr(embedding, name), name
