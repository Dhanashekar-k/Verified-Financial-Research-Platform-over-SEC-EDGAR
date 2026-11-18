"""Citation URLs (M02 section 3.3 step 6).

`primary_doc_url#anchor_id` when the chunk starts exactly at an element with an id; otherwise a
text fragment, `primary_doc_url#:~:text=<first ~8 words>`, which Chromium and recent Safari
highlight. In a text directive `,`, `-` and `&` are syntax, so they are percent-encoded along
with everything else outside the unreserved set. Filings are immutable on EDGAR, so a URL stays
valid.
"""

from __future__ import annotations

from urllib.parse import quote

FRAGMENT_WORDS = 8


def fragment_source(text: str, *, is_table: bool = False) -> str:
    """The words a text fragment should quote.

    Only the first line: a directive that spans two blocks matches nothing in the browser. A
    table quotes its first label (the text before the first `|`), otherwise the first 8 words.
    """
    clean = text.replace("\xa0", " ")
    lines = [ln for ln in clean.splitlines() if ln.strip()]
    first = lines[0] if lines else ""
    if is_table:
        first = first.split("|")[0]
    return " ".join(first.split()[:FRAGMENT_WORDS])


def citation_url(doc_url: str, anchor: str | None, text: str, *, is_table: bool = False) -> str:
    if anchor:
        return f"{doc_url}#{anchor}"
    words = fragment_source(text, is_table=is_table)
    if not words:
        return doc_url
    return f"{doc_url}#:~:text={quote(words, safe='').replace('-', '%2D')}"
