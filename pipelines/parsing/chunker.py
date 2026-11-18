"""Chunking (M02 section 3.3 step 5): <= 400 embedder tokens, ~60 tokens of overlap.

`chunk_text` packs whole sentences (paragraph breaks are kept as newlines) into chunks no larger
than `max_tokens`, measured with the embedder's own tokenizer, and starts each next chunk with
the trailing sentences of the previous one that fit in `overlap_tokens`. A sentence longer than
`max_tokens` is cut at word boundaries by token offsets. Every chunk is a contiguous slice of the
input (`text[start:end]`), so the chunks reproduce the text exactly: take `text[prev.end:cur.end]`
from each in turn.

`chunk_table` splits a serialised table between rows and repeats the header lines at the top of
every chunk; a single row longer than the budget is cut like a long sentence, header kept.

Sizes are always re-measured on the final chunk text: summing per-sentence counts is only used to
pick the candidate, never to certify the limit.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pipelines.parsing.tokens import Tokenizer, get_tokenizer

MAX_TOKENS = 400
OVERLAP_TOKENS = 60

_ABBREVIATIONS = {
    "inc.", "corp.", "co.", "ltd.", "no.", "nos.", "u.s.", "u.k.", "mr.", "mrs.", "ms.", "dr.",
    "st.", "vs.", "e.g.", "i.e.", "approx.", "fig.", "etc.", "jan.", "feb.", "mar.", "apr.",
    "jun.", "jul.", "aug.", "sep.", "sept.", "oct.", "nov.", "dec.", "l.p.", "s.a.", "n.a.",
}  # fmt: skip
_SENTENCE_END = re.compile(r"(?<=[.!?])([\"')\]”’]*)\s+(?=[\"'(\[“‘A-Z0-9])")


@dataclass(frozen=True, slots=True)
class TextChunk:
    text: str
    n_tokens: int
    start: int  # character offsets into the chunked text (tables: 0 and len(text))
    end: int
    overlap_tokens: int  # tokens shared with the previous chunk


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence; paragraph breaks always end a sentence."""
    spans: list[tuple[int, int]] = []
    for para in re.finditer(r"[^\n]+", text):
        seg_start, seg = para.start(), para.group()
        cut = 0
        for m in _SENTENCE_END.finditer(seg):
            before = seg[cut : m.end(1)].split()
            if before and before[-1].lower() in _ABBREVIATIONS:
                continue
            spans.append((seg_start + cut, seg_start + m.end(1)))
            cut = m.end()
        spans.append((seg_start + cut, seg_start + len(seg.rstrip())))
    return [(a, b) for a, b in spans if text[a:b].strip()]


def _word_start(piece: str, pos: int) -> bool:
    return pos == 0 or piece[pos - 1].isspace()


def _split_long(
    text: str, a: int, b: int, tok: Tokenizer, max_tokens: int
) -> list[tuple[int, int]]:
    """Cut text[a:b] into pieces of <= max_tokens, at word boundaries when one is in reach.

    A run with no whitespace inside the window (a rule of dashes, a long identifier) is cut at the
    token limit instead: tiny pieces would be worse than one mid-word cut.
    """
    piece = text[a:b]
    spans = tok.offsets(piece)
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(spans):
        j = min(i + max_tokens, len(spans))
        if j < len(spans):
            k = j
            while k > i + 1 and not _word_start(piece, spans[k][0]):
                k -= 1  # back up to a token that starts a word
            if k > i and _word_start(piece, spans[k][0]):
                j = k
        out.append((a + spans[i][0], a + spans[j - 1][1]))
        i = j
    return out


def chunk_text(
    text: str,
    tok: Tokenizer | None = None,
    max_tokens: int = MAX_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[TextChunk]:
    if max_tokens < 1:
        raise ValueError("max_tokens must be >= 1")
    if not 0 <= overlap_tokens < max_tokens:
        raise ValueError("overlap_tokens must be >= 0 and < max_tokens")
    tok = tok or get_tokenizer()

    spans: list[tuple[int, int]] = []
    for a, b in _sentence_spans(text):
        if tok.count(text[a:b]) > max_tokens:
            spans.extend(_split_long(text, a, b, tok, max_tokens))
        else:
            spans.append((a, b))
    if not spans:
        return []
    counts = tok.count_many([text[a:b] for a, b in spans])

    chunks: list[TextChunk] = []
    i, prev_end = 0, 0
    n = len(spans)
    while i < n:
        j, total = i, 0
        while j < n and total + counts[j] <= max_tokens:
            total += counts[j]
            j += 1
        j = max(j, i + 1)
        while True:  # certify on the joined text; the sum of parts is only an estimate
            chunk = text[spans[i][0] : spans[j - 1][1]]
            actual = tok.count(chunk)
            if actual <= max_tokens or j - 1 <= i:
                break
            j -= 1
        overlap = (
            tok.count(text[spans[i][0] : prev_end]) if chunks and spans[i][0] < prev_end else 0
        )
        chunks.append(TextChunk(chunk, actual, spans[i][0], spans[j - 1][1], overlap))
        prev_end = spans[j - 1][1]
        if j >= n:
            break
        k, acc = j, 0
        while k - 1 > i and acc + counts[k - 1] <= overlap_tokens:
            k -= 1
            acc += counts[k]
        # An overlap chunk must reach past this chunk's end, or it only repeats the overlap.
        reach, size = k, 0
        while reach < n and size + counts[reach] <= max_tokens:
            size += counts[reach]
            reach += 1
        i = k if reach > j else j
    return chunks


def chunk_table(
    header: Sequence[str],
    rows: Sequence[str],
    tok: Tokenizer | None = None,
    max_tokens: int = MAX_TOKENS,
) -> list[TextChunk]:
    tok = tok or get_tokenizer()
    if not rows:
        return []
    head = "\n".join(header)
    head_tokens = tok.count(head) if head else 0
    if head_tokens > max_tokens // 2:  # a monster header would starve the rows: do not repeat it
        head, head_tokens = "", 0
    n_head = len(header) if head else 0
    budget = max_tokens - head_tokens - (1 if head else 0)

    lines: list[str] = []
    for row in rows:
        if tok.count(row) > budget:
            lines.extend(row[a:b] for a, b in _split_long(row, 0, len(row), tok, budget))
        else:
            lines.append(row)
    counts = tok.count_many(lines)

    def emit(group: list[str]) -> TextChunk:
        text = "\n".join([head, *group]) if head else "\n".join(group)
        return TextChunk(text, tok.count(text), 0, len(text), 0)

    chunks: list[TextChunk] = []
    group: list[str] = []
    total = 0
    for line, cnt in zip(lines, counts, strict=True):
        if group and total + cnt + 1 > budget:
            chunks.append(emit(group))
            group, total = [], 0
        group.append(line)
        total += cnt + 1
    if group:
        chunks.append(emit(group))
    # certify on the real text: trim a group if the estimate was optimistic
    final: list[TextChunk] = []
    for c in chunks:
        if c.n_tokens <= max_tokens:
            final.append(c)
            continue
        body = c.text.splitlines()[n_head:]
        half = max(1, len(body) // 2)
        final.extend([emit(body[:half]), emit(body[half:])])
    return final
