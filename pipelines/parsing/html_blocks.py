"""Filing HTML -> an ordered list of paragraph and table blocks (M02 section 3.3 steps 1-2).

* Dropped entirely: `<head>`, `<script>`, `<style>`, the inline-XBRL header (`ix:header`, which
  holds the hidden facts) and anything with `display:none`, so no number soup reaches the text.
* A block is a run of inline content between block-level boundaries. `font` and `ix:*` wrappers
  are transparent: when they contain blocks the walk goes through them.
* `anchor`: the first `id` found on the block (own, nested, or an empty element just before it,
  or a container's id passed to its first block), preferring ids that an internal `href="#id"`
  points at (the table of contents' targets).
* `is_link`: at least half the block's text is internal-link text (a TOC entry).
* Tables with numbers (`tables.is_data_table`) become one `table` block with cleaned rows;
  other tables are read as paragraphs (a one-cell table is read through to its content).
* Bare page numbers, "Table of Contents" back-links and running page headers (the same short
  paragraph, digits aside, at least six times) are removed. When the repeated line is an item
  heading with a title (Berkshire repeats "Item 2. Management's Discussion ..." on every MD&A
  page), its first copy is kept: it is the heading, and dropping all of them would erase the
  section. A bare repeated "Item 1" (a navigation label) is still dropped.

Pure Python on selectolax's lexbor parser. A document that cannot be parsed raises; the caller
(`parse_document`) records it as a per-document error.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from selectolax.lexbor import LexborHTMLParser, LexborNode

from pipelines.parsing.tables import clean_cells, header_row_count, is_data_table, render_row

_SKIP = {"head", "script", "style", "title", "meta", "link", "noscript", "ix:header", "-comment"}
_BLOCK = {
    "div", "p", "section", "article", "main", "header", "footer", "aside", "nav", "address",
    "li", "ul", "ol", "dl", "dt", "dd", "blockquote", "pre", "center", "form", "fieldset",
    "figure", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "body", "html",
    "table", "tr", "tbody", "thead", "tfoot", "td", "th",
}  # fmt: skip
_TRANSPARENT = {"font"}  # inline in theory, but old filings wrap whole divs in them
_NONE_DISPLAY = re.compile(r"display\s*:\s*none", re.IGNORECASE)
_WS = re.compile(r"\s+")
_BR_BREAK = re.compile(r"\x00[\s\x00]*\x00")  # two or more <br> in a row end a paragraph
_PAGE_NUMBER = re.compile(
    r"^(?:page\s+)?[\divxlc]{1,4}$|^page\s+\d+(?:\s+of\s+\d+)?$", re.IGNORECASE
)
_DIGITS = re.compile(r"\d+")
_BLOCK_SELECTOR = "div,p,table,li,ul,ol,h1,h2,h3,h4,h5,h6,tr"
REPEAT_MIN = 6
REPEAT_MAX_LEN = 120
# A repeated line that is an item heading WITH a title ("Item 2. Management's Discussion ..."): the
# first copy is the real heading, the rest are running page headers. A bare "Item 1" repeated on
# every page is a navigation label, not a heading, so a title (three letters) is required. Same
# shape as sections._ITEM, but kept here because `sections` imports this module.
_ITEM_HEADING = re.compile(
    r"^(?:part\s+(?:iv|iii|ii|i|[1-4])\b[\s.,:;|\-\u2013\u2014]*)?"
    r"item\s*\d{1,2}(?:\s*\.\s*(?=[a-c]\.))?\s*[a-c]?(?![a-z0-9])[\s.,:;|\-\u2013\u2014]*[a-z]{3}",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Block:
    kind: Literal["paragraph", "table"]
    text: str
    anchor: str | None = None
    is_link: bool = False
    rows: tuple[tuple[str, ...], ...] = ()
    header_rows: int = 0


def normalize(text: str) -> str:
    cleaned = text.replace("\xa0", " ").replace("\x00", " ").replace("\u200b", "")
    return _WS.sub(" ", cleaned).strip()


def decode_html(data: str | bytes) -> str:
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _hidden(node: LexborNode) -> bool:
    style = node.attributes.get("style")
    return bool(style) and bool(_NONE_DISPLAY.search(style))


class _Walker:
    def __init__(self, link_targets: set[str]) -> None:
        self.blocks: list[Block] = []
        self._links = link_targets
        self._text: list[str] = []
        self._linked_chars = 0
        self._ids: list[str] = []
        self._pending_ids: list[str] = []

    # -- inline buffer ---------------------------------------------------------------------

    def _note_id(self, node: LexborNode) -> None:
        node_id = node.attributes.get("id") or node.attributes.get("name")
        if node_id:
            self._ids.append(node_id)

    def _flush(self) -> None:
        raw = "".join(self._text)
        pieces = [p for p in (normalize(x) for x in _BR_BREAK.split(raw)) if p]
        if pieces:
            anchor = _pick_anchor([*self._ids, *self._pending_ids], self._links)
            is_link = self._linked_chars * 2 >= len(normalize(raw))
            for n, piece in enumerate(pieces):
                self.blocks.append(Block("paragraph", piece, anchor if n == 0 else None, is_link))
            self._pending_ids = []
        elif self._ids:
            self._pending_ids.extend(self._ids)  # an empty anchor labels the next block
        self._text, self._linked_chars, self._ids = [], 0, []

    def _inline(self, node: LexborNode, linked: bool) -> None:
        tag = node.tag
        if tag == "-text":
            chunk = node.text_content or ""
            self._text.append(chunk)
            if linked:
                self._linked_chars += len(normalize(chunk))
            return
        if tag in _SKIP or _hidden(node):
            return
        if tag == "br":
            self._text.append("\x00")
            return
        self._note_id(node)
        href = node.attributes.get("href") or ""
        is_internal_link = tag == "a" and href.startswith("#")
        spaced = tag in _BLOCK  # block children of a table cell must not run together
        if spaced:
            self._text.append(" ")
        child = node.child
        while child is not None:
            self._inline(child, linked or is_internal_link)
            child = child.next
        if spaced:
            self._text.append(" ")

    # -- structure -------------------------------------------------------------------------

    def _contains_block(self, node: LexborNode) -> bool:
        return node.css_first(_BLOCK_SELECTOR) is not None

    def walk_children(self, node: LexborNode) -> None:
        child = node.child
        while child is not None:
            self.walk(child)
            child = child.next
        self._flush()

    def walk(self, node: LexborNode) -> None:
        tag = node.tag
        if tag == "-text":
            self._inline(node, False)
            return
        if tag in _SKIP or _hidden(node):
            return
        if tag == "table":
            self._flush()
            self._table(node)
            return
        transparent = tag in _TRANSPARENT or tag.startswith("ix:") or tag == "a" or tag == "span"
        is_block = tag in _BLOCK
        if is_block or (transparent and self._contains_block(node)):
            self._flush()
            own_id = node.attributes.get("id")
            if own_id:
                self._pending_ids.append(own_id)
            if is_block and not self._contains_block(node):
                self._inline_children(node)
            else:
                self.walk_children(node)
            self._flush()
            return
        self._inline(node, False)

    def _inline_children(self, node: LexborNode) -> None:
        child = node.child
        while child is not None:
            self._inline(child, False)
            child = child.next
        self._flush()

    # -- tables ----------------------------------------------------------------------------

    def _table_rows(self, table: LexborNode) -> list[list[LexborNode]]:
        rows: list[list[LexborNode]] = []

        def collect(parent: LexborNode) -> None:
            child = parent.child
            while child is not None:
                if child.tag == "tr":
                    cells = []
                    cell = child.child
                    while cell is not None:
                        if cell.tag in ("td", "th"):
                            cells.append(cell)
                        cell = cell.next
                    rows.append(cells)
                elif child.tag in ("thead", "tbody", "tfoot"):
                    collect(child)
                child = child.next

        collect(table)
        return rows

    def _cell_text(self, cell: LexborNode) -> str:
        sub = _Walker(self._links)
        sub._inline(cell, False)
        return normalize("".join(sub._text))

    def _table(self, table: LexborNode) -> None:
        rows = self._table_rows(table)
        table_id = table.attributes.get("id")
        if table_id:
            self._pending_ids.append(table_id)
        if len(rows) == 1 and len(rows[0]) == 1:
            self.walk_children(rows[0][0])  # a one-cell layout table: read through to content
            return
        pairs = [(nodes, clean_cells([self._cell_text(c) for c in nodes])) for nodes in rows]
        pairs = [(nodes, cells) for nodes, cells in pairs if cells]  # keep nodes and cells aligned
        cleaned = [cells for _, cells in pairs]
        if not cleaned:
            return
        if is_data_table(cleaned):
            header = header_row_count(cleaned)
            text = "\n".join(render_row(r) for r in cleaned)
            anchor = _pick_anchor([*self._pending_ids, *_ids_inside(table)], self._links)
            self._pending_ids = []
            self.blocks.append(
                Block("table", text, anchor, False, tuple(cleaned), header_rows=header)
            )
            return
        for row_nodes, row in pairs:
            self._ids = [i for c in row_nodes for i in _ids_inside(c)]
            self._text = [" ".join(row)]
            self._linked_chars = (
                len(" ".join(row)) if any(_has_internal_link(c) for c in row_nodes) else 0
            )
            self._flush()


def _ids_inside(node: LexborNode) -> list[str]:
    ids = [node.attributes.get("id")] if node.attributes.get("id") else []
    for n in node.css("[id],a[name]"):
        value = n.attributes.get("id") or n.attributes.get("name")
        if value:
            ids.append(value)
    return ids


def _has_internal_link(node: LexborNode) -> bool:
    return any((a.attributes.get("href") or "").startswith("#") for a in node.css("a"))


def _pick_anchor(ids: list[str], link_targets: set[str]) -> str | None:
    for i in ids:
        if i in link_targets:
            return i
    return ids[0] if ids else None


def _drop_noise(blocks: list[Block]) -> list[Block]:
    shape = Counter(
        _DIGITS.sub("#", b.text)
        for b in blocks
        if b.kind == "paragraph" and len(b.text) <= REPEAT_MAX_LEN
    )
    out: list[Block] = []
    kept_headings: set[str] = set()
    for b in blocks:
        if b.kind == "paragraph":
            low = b.text.lower()
            if _PAGE_NUMBER.match(b.text) or low == "table of contents":
                continue
            key = _DIGITS.sub("#", b.text)
            if (
                len(b.text) <= REPEAT_MAX_LEN
                and _DIGITS.search(b.text)
                and shape[key] >= REPEAT_MIN
            ):
                if not _ITEM_HEADING.match(b.text) or key in kept_headings:
                    continue
                kept_headings.add(key)
        out.append(b)
    return out


def html_to_blocks(data: str | bytes) -> list[Block]:
    html = decode_html(data)
    if not html.strip():
        return []
    tree = LexborHTMLParser(html)
    body = tree.body
    if body is None:
        return []
    link_targets = {
        (a.attributes.get("href") or "")[1:]
        for a in body.css("a[href^='#']")
        if len(a.attributes.get("href") or "") > 1
    }
    walker = _Walker(link_targets)
    walker.walk_children(body)
    return _drop_noise(walker.blocks)
