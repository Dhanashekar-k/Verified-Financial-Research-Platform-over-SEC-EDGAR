"""Tests for ``pipelines/parsing/tokens.py``: the embedder's own tokenizer, bundled in the repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipelines.parsing.tokens import TOKENIZER_SHA256, Tokenizer, get_tokenizer


def test_counts_wordpiece_tokens_without_special_tokens():
    tok = get_tokenizer()

    assert tok.count("hello world") == 2
    assert tok.count("") == 0
    assert tok.count("unaffordable") > 1  # split into word pieces, which tiktoken would not do


def test_count_many_matches_count():
    tok = get_tokenizer()
    texts = ["Net sales increased 5%.", "Risk factors", ""]

    assert tok.count_many(texts) == [tok.count(t) for t in texts]


def test_offsets_are_char_spans_into_the_text():
    tok = get_tokenizer()
    text = "Revenue grew 12% year over year."

    spans = tok.offsets(text)

    assert len(spans) == tok.count(text)
    assert all(0 <= a < b <= len(text) for a, b in spans)
    assert [a for a, _ in spans] == sorted(a for a, _ in spans)
    assert text[spans[0][0] : spans[0][1]] == "Revenue"


def test_long_text_is_not_truncated_at_512():
    assert get_tokenizer().count("word " * 2000) == 2000


def test_the_bundled_file_matches_its_pinned_checksum():
    assert Tokenizer().sha256 == TOKENIZER_SHA256


def test_a_modified_tokenizer_file_is_rejected(tmp_path: Path):
    bad = tmp_path / "tokenizer.json"
    bad.write_text('{"not": "the tokenizer"}')

    with pytest.raises(ValueError, match="sha256"):
        Tokenizer(bad)
