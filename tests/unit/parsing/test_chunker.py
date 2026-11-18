"""Tests for ``pipelines/parsing/chunker.py`` (M02 section 3.3 step 5) with the property checks
the runbook asks for: no chunk over 400 embedder tokens, overlap <= 80, chunks reproduce the
section text, and sentences are not split when they fit.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pipelines.parsing.chunker import TextChunk, chunk_table, chunk_text
from pipelines.parsing.tokens import get_tokenizer

TOK = get_tokenizer()
WORDS = [
    "revenue", "increased", "because", "customers", "demand", "supply", "chain", "risk",
    "inflation", "currency", "Apple", "products", "services", "margin", "operating", "income",
    "fiscal", "quarter", "tax", "rate", "compared", "prior", "year", "net", "sales", "Inc.",
]  # fmt: skip


def sentence(n_words: int, seed: int = 0) -> str:
    words = [WORDS[(seed + i * 7) % len(WORDS)] for i in range(n_words)]
    return (" ".join(words)).capitalize() + "."


def rebuild(text: str, chunks: list[TextChunk]) -> str:
    """The chunks without their overlap: each chunk's text from where the previous one ended."""
    pieces, prev_end = [], 0
    for c in chunks:
        pieces.append(text[min(prev_end, c.start) if prev_end < c.start else prev_end : c.end])
        prev_end = c.end
    return "".join(pieces)


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------------------
# chunk_text: examples
# --------------------------------------------------------------------------------------


def test_short_text_is_one_chunk_with_no_overlap():
    [c] = chunk_text("Net sales rose. Margins held.")

    assert c.text == "Net sales rose. Margins held."
    assert (c.start, c.end, c.overlap_tokens) == (0, len(c.text), 0)
    assert c.n_tokens == TOK.count(c.text)


def test_empty_and_blank_text_have_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("  \n ") == []


def test_chunks_are_cut_between_sentences_and_overlap_by_whole_sentences():
    text = " ".join(sentence(30, seed=i) for i in range(40))

    chunks = chunk_text(text)

    assert len(chunks) > 1
    assert all(c.text.endswith(".") for c in chunks)
    assert all(c.n_tokens <= 400 for c in chunks)
    assert chunks[1].overlap_tokens > 0
    assert chunks[1].start < chunks[0].end  # they overlap in the text


def test_a_single_oversized_sentence_is_split_by_tokens_at_word_boundaries():
    text = " ".join(WORDS[i % len(WORDS)] for i in range(1500)) + "."

    chunks = chunk_text(text)

    assert len(chunks) >= 4
    assert all(c.n_tokens <= 400 for c in chunks)
    assert all(not c.text[0].isspace() and not c.text[-1].isspace() for c in chunks)
    for c in chunks:  # no chunk starts or ends mid-word
        assert c.start == 0 or text[c.start - 1] == " "
        assert c.end == len(text) or text[c.end] in " ."


def test_abbreviations_do_not_end_sentences():
    text = "Apple Inc. reported results. The U.S. market grew. See No. 5 for details."

    [c] = chunk_text(text, max_tokens=400)

    assert c.text == text  # one chunk; and below, the sentence splitter keeps 'Inc.' attached
    small = chunk_text(text, max_tokens=8, overlap_tokens=0)
    assert not any(ch.text.rstrip().endswith(("Inc.", "U.S.", "No.")) for ch in small[:-1])


def test_paragraph_breaks_are_kept_inside_a_chunk():
    [c] = chunk_text("First paragraph.\nSecond paragraph.")

    assert c.text == "First paragraph.\nSecond paragraph."


def test_the_overlap_parameter_is_respected():
    text = " ".join(sentence(25, seed=i) for i in range(60))

    chunks = chunk_text(text, max_tokens=200, overlap_tokens=30)

    assert all(c.overlap_tokens <= 30 for c in chunks)
    assert chunk_text(text, max_tokens=200, overlap_tokens=0)[1].overlap_tokens == 0


def test_invalid_parameters_are_rejected():
    with pytest.raises(ValueError, match="overlap"):
        chunk_text("x.", max_tokens=100, overlap_tokens=100)
    with pytest.raises(ValueError, match="max_tokens"):
        chunk_text("x.", max_tokens=0)


# --------------------------------------------------------------------------------------
# chunk_text: properties
# --------------------------------------------------------------------------------------

_sentences = st.lists(
    st.tuples(st.integers(1, 60), st.integers(0, 25)).map(lambda t: sentence(*t)),
    min_size=1,
    max_size=120,
)


@settings(max_examples=60, deadline=None)
@given(_sentences, st.sampled_from([" ", "\n"]))
def test_chunks_respect_the_limits_cover_the_text_and_cut_at_sentence_ends(
    sents: list[str], sep: str
):
    text = sep.join(sents)

    chunks = chunk_text(text)

    assert chunks
    for c in chunks:
        assert c.n_tokens == TOK.count(c.text) <= 400
        assert c.overlap_tokens <= 80
        assert text[c.start : c.end] == c.text
        assert c.text[-1] in ".!?"
    assert [c.start for c in chunks] == sorted({c.start for c in chunks})
    rebuilt = rebuild(text, chunks)
    assert squash(rebuilt) == squash(text)


@settings(max_examples=30, deadline=None)
@given(st.integers(400, 3000), st.integers(0, 20))
def test_oversized_runs_without_sentence_ends_still_respect_the_limits(n_words: int, seed: int):
    text = " ".join(WORDS[(seed + i) % len(WORDS)] for i in range(n_words))

    chunks = chunk_text(text)

    assert all(c.n_tokens <= 400 and c.overlap_tokens <= 80 for c in chunks)
    rebuilt = rebuild(text, chunks)
    assert squash(rebuilt) == squash(text)


# --------------------------------------------------------------------------------------
# chunk_table
# --------------------------------------------------------------------------------------


def test_a_small_table_is_one_chunk_with_its_header_first():
    [c] = chunk_table(["In millions | 2023 | 2022"], ["Net sales | $1 | $2", "Cost | $3 | $4"])

    assert c.text == "In millions | 2023 | 2022\nNet sales | $1 | $2\nCost | $3 | $4"


def test_a_big_table_splits_between_rows_and_repeats_the_header_in_every_chunk():
    header = ["In millions | 2023 | 2022"]
    rows = [f"Line item number {i} | ${i * 1000:,} | ${i * 900:,}" for i in range(400)]

    chunks = chunk_table(header, rows)

    assert len(chunks) > 1
    for c in chunks:
        assert c.text.startswith(header[0] + "\n")
        assert c.n_tokens == TOK.count(c.text) <= 400
    body = [ln for c in chunks for ln in c.text.splitlines()[1:]]
    assert body == rows


def test_a_table_without_a_header_just_packs_rows():
    chunks = chunk_table([], [f"Row {i} | {i},000" for i in range(300)])

    assert all(c.n_tokens <= 400 for c in chunks)
    assert [ln for c in chunks for ln in c.text.splitlines()] == [
        f"Row {i} | {i},000" for i in range(300)
    ]


def test_one_enormous_row_is_split_and_each_piece_keeps_the_header():
    long_row = "Description | " + " ".join(WORDS[i % len(WORDS)] for i in range(1200))

    chunks = chunk_table(["Header | A"], [long_row])

    assert len(chunks) > 1
    assert all(c.text.startswith("Header | A\n") and c.n_tokens <= 400 for c in chunks)


def test_an_empty_table_has_no_chunks():
    assert chunk_table(["Header"], []) == []


def test_an_overlap_chunk_that_would_only_repeat_the_previous_one_is_not_emitted():
    """Sentences of 340, 55 and 380 tokens: the 55-token overlap plus 380 does not fit, so the
    third sentence must start a fresh chunk instead of a chunk made only of the overlap."""

    def sized(n_tokens: int, seed: int) -> str:
        words, text = [], ""
        while TOK.count(text + ".") < n_tokens:
            words.append(WORDS[(seed + len(words) * 3) % (len(WORDS) - 1)])
            text = " ".join(words)
        return text.capitalize() + "."

    text = " ".join([sized(340, 1), sized(55, 2), sized(380, 3)])

    chunks = chunk_text(text)

    assert all(b.end > a.end for a, b in zip(chunks, chunks[1:], strict=False))
    assert all(b.start >= a.start for a, b in zip(chunks, chunks[1:], strict=False))
    assert squash(rebuild(text, chunks)) == squash(text)


@settings(max_examples=80, deadline=None)
@given(st.lists(st.integers(1, 390), min_size=2, max_size=25), st.integers(0, 9))
def test_every_chunk_adds_text_beyond_the_previous_chunk(sizes: list[int], seed: int):
    sents = []
    for n, size in enumerate(sizes):
        words, text = [], ""
        while TOK.count(text + ".") < size:
            words.append(WORDS[(seed + n + len(words) * 3) % (len(WORDS) - 1)])
            text = " ".join(words)
        sents.append(text.capitalize() + ".")
    text = " ".join(sents)

    chunks = chunk_text(text)

    assert all(b.end > a.end for a, b in zip(chunks, chunks[1:], strict=False))
    assert squash(rebuild(text, chunks)) == squash(text)


def test_a_long_unbroken_run_in_a_table_row_is_cut_at_the_token_limit_not_into_tiny_pieces():
    rows = ["Rule | " + "-" * 2000, "Net sales | $1 | $2"]

    chunks = chunk_table(["Header | A"], rows)

    assert 3 <= len(chunks) <= 8
    assert all(c.n_tokens <= 400 for c in chunks)
    assert sum(len(c.text.splitlines()) for c in chunks) < 30  # not ~2000 one-character lines


def test_a_multi_row_header_is_repeated_not_duplicated_if_a_group_is_split():
    header = ["In millions", "2023 | 2022"]
    rows = [f"Row {i} | {i},000" for i in range(300)]

    for c in chunk_table(header, rows):
        assert c.text.splitlines()[:2] == header
        assert c.text.splitlines()[2:3] != header[:1]
