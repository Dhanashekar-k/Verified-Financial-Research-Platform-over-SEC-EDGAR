"""Item-heading detection and the TOC-skipping chain (M02 section 3.3 step 3-4).

A *candidate* is a paragraph, or a row of a table block, whose text starts with
`[Part <roman>] Item <n>[A-C]` followed by a short title (cross-references such as "see Item 1A"
or "Item 7 of this report ..." do not start that way, or continue in lower case, and are ignored).

Choosing the real headings (the heuristic posted to the owner before it was relied on):

1. Each candidate's weight is the number of characters between it and the NEXT candidate of any
   code (for the last one: to a SIGNATURES block, else the end). A table-of-contents entry is
   followed at once by the next entry, so its weight is about 0. A candidate whose block is an
   internal link (`is_link`) is weighted x0.01, since TOC entries are link text.
2. One occurrence per code is chosen so that codes strictly follow the form's canonical order and
   positions strictly increase, maximising the total weight (a DP over candidates). The body
   chain weighs the filing; the TOC chain weighs nothing; mixing them breaks the order.
3. A section runs from its heading to the next chosen heading (the last: to SIGNATURES or the
   end). Unchosen candidates stay inside the text.
4. 10-Q codes are `P<part>-<item>`; the part comes from the heading or the nearest preceding
   `PART I/II` block.

Known failure cases: combined headings ("Items 1 and 2"), a filing with no body headings at all
(nothing to find), a title split across more than the next block, and a repeated body heading
with more text than the real one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pipelines.parsing.html_blocks import Block

CODES_10K: tuple[str, ...] = (
    "1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8", "9", "9A", "9B", "9C",
    "10", "11", "12", "13", "14", "15", "16",
)  # fmt: skip
CODES_10Q: tuple[str, ...] = (
    "P1-1", "P1-2", "P1-3", "P1-4", "P2-1", "P2-1A", "P2-2", "P2-3", "P2-4", "P2-5", "P2-6",
)  # fmt: skip

CANONICAL_TITLES_10K = {
    "1": "Business",
    "1A": "Risk Factors",
    "1B": "Unresolved Staff Comments",
    "1C": "Cybersecurity",
    "2": "Properties",
    "3": "Legal Proceedings",
    "4": "Mine Safety Disclosures",
    "5": "Market for Registrant's Common Equity, Related Stockholder Matters and Issuer Purchases "
    "of Equity Securities",
    "6": "Selected Financial Data",
    "7": "Management's Discussion and Analysis of Financial Condition and Results of Operations",
    "7A": "Quantitative and Qualitative Disclosures About Market Risk",
    "8": "Financial Statements and Supplementary Data",
    "9": "Changes in and Disagreements with Accountants on Accounting and Financial Disclosure",
    "9A": "Controls and Procedures",
    "9B": "Other Information",
    "9C": "Disclosure Regarding Foreign Jurisdictions that Prevent Inspections",
    "10": "Directors, Executive Officers and Corporate Governance",
    "11": "Executive Compensation",
    "12": "Security Ownership of Certain Beneficial Owners and Management",
    "13": "Certain Relationships and Related Transactions, and Director Independence",
    "14": "Principal Accountant Fees and Services",
    "15": "Exhibits and Financial Statement Schedules",
    "16": "Form 10-K Summary",
}  # fmt: skip
CANONICAL_TITLES_10Q = {
    "P1-1": "Financial Statements",
    "P1-2": "Management's Discussion and Analysis of Financial Condition and Results of Operations",
    "P1-3": "Quantitative and Qualitative Disclosures About Market Risk",
    "P1-4": "Controls and Procedures",
    "P2-1": "Legal Proceedings",
    "P2-1A": "Risk Factors",
    "P2-2": "Unregistered Sales of Equity Securities and Use of Proceeds",
    "P2-3": "Defaults Upon Senior Securities",
    "P2-4": "Mine Safety Disclosures",
    "P2-5": "Other Information",
    "P2-6": "Exhibits",
}

MAX_HEADING_CHARS = 200
MAX_TITLE_CHARS = 150
MAX_FOLLOWING_TITLE_CHARS = 100
LINK_WEIGHT = 0.01
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "1": 1, "2": 2, "3": 3, "4": 4}
_SEP = r"[\s.,:;|\-–—]*"
_ITEM = re.compile(
    rf"^(?:part\s+(?P<part>iv|iii|ii|i|[1-4])\b{_SEP})?"
    rf"item\s*(?P<num>\d{{1,2}})\s*(?P<let>[a-c])?(?![a-z0-9]){_SEP}(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_PART = re.compile(rf"^part\s+(?P<part>iv|iii|ii|i|[1-4])\b{_SEP}(?P<rest>.*)$", re.IGNORECASE)
_SIGNATURES = re.compile(r"^signatures?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ItemHeading:
    item: str  # '1A'
    part: str | None  # roman numeral as written, upper-cased, or None
    title: str


@dataclass(frozen=True, slots=True)
class Section:
    code: str
    title: str
    heading_text: str
    heading_index: int  # block holding the heading (a table block when it came from a row)
    start: int  # first body block
    end: int  # exclusive
    anchor: str | None


@dataclass(frozen=True, slots=True)
class _Candidate:
    code: str
    heading: ItemHeading
    text: str
    block: int
    row: int
    is_link: bool


def parse_item_heading(text: str) -> ItemHeading | None:
    text = text.strip()
    if not text or len(text) > MAX_HEADING_CHARS:
        return None
    m = _ITEM.match(text)
    if not m:
        return None
    rest = m.group("rest").strip()
    if len(rest) > MAX_TITLE_CHARS or (rest and rest[0].islower()):
        return None
    item = m.group("num") + (m.group("let") or "").upper()
    if item not in CANONICAL_TITLES_10K:
        return None
    part = m.group("part")
    return ItemHeading(item, part.upper() if part else None, rest.rstrip(" .:;|-–—"))


def _form_kind(form: str) -> str:
    if form.startswith("10-K"):
        return "10-K"
    if form.startswith("10-Q"):
        return "10-Q"
    raise ValueError(f"unsupported form {form!r}: expected a 10-K or 10-Q family form")


def _code(kind: str, heading: ItemHeading, current_part: int | None) -> str | None:
    if kind == "10-K":
        return heading.item
    part = _ROMAN[heading.part.lower()] if heading.part else current_part
    if part is None:
        return None
    code = f"P{part}-{heading.item}"
    return code if code in CODES_10Q else None


def _candidates(blocks: Sequence[Block], kind: str) -> list[_Candidate]:
    out: list[_Candidate] = []
    part: int | None = None
    for i, block in enumerate(blocks):
        lines = (
            [(0, " ".join(block.text.split()), block.is_link)]
            if block.kind == "paragraph"
            else [(r, " ".join(row), False) for r, row in enumerate(block.rows)]
        )
        for r, text, is_link in lines:
            heading = parse_item_heading(text)
            if heading is None:
                pm = _PART.match(text) if len(text) <= 120 else None
                if pm:
                    part = _ROMAN[pm.group("part").lower()]
                continue
            code = _code(kind, heading, part)
            if code is not None:
                if heading.part:  # only a heading with a valid code may move the current part
                    part = _ROMAN[heading.part.lower()]
                out.append(_Candidate(code, heading, text, i, r, is_link))
    return out


def _end_marker(blocks: Sequence[Block], after: int) -> int:
    for i in range(after, len(blocks)):
        b = blocks[i]
        if b.kind == "paragraph" and len(b.text) <= 20 and _SIGNATURES.match(b.text.strip()):
            return i
    return len(blocks)


def detect_sections(blocks: Sequence[Block], form: str) -> list[Section]:
    kind = _form_kind(form)
    cands = _candidates(blocks, kind)
    if not cands:
        return []
    order = CODES_10K if kind == "10-K" else CODES_10Q
    rank = {c: i for i, c in enumerate(order)}

    cum = [0]
    for b in blocks:
        cum.append(cum[-1] + len(b.text))
    stop = _end_marker(blocks, cands[-1].block)

    weights: list[float] = []
    for n, c in enumerate(cands):
        nxt = cands[n + 1].block if n + 1 < len(cands) else stop
        chars = max(0, cum[nxt] - cum[c.block + 1]) if nxt > c.block else 0
        weights.append(chars * (LINK_WEIGHT if c.is_link else 1.0))

    # best[i]: max total weight of a valid chain ending at candidate i
    best = [0.0] * len(cands)
    prev = [-1] * len(cands)
    for i, c in enumerate(cands):
        top, arg = 0.0, -1
        for j in range(i):
            if rank[cands[j].code] < rank[c.code] and best[j] > top:
                top, arg = best[j], j
        best[i], prev[i] = weights[i] + top, arg
    tail = max(range(len(cands)), key=lambda i: (best[i], -i))
    chain: list[int] = []
    while tail != -1:
        chain.append(tail)
        tail = prev[tail]
    chain.reverse()

    titles = CANONICAL_TITLES_10K if kind == "10-K" else CANONICAL_TITLES_10Q
    chosen = [cands[i] for i in chain]
    sections: list[Section] = []
    for n, c in enumerate(chosen):
        end = chosen[n + 1].block if n + 1 < len(chosen) else stop
        start = c.block + 1
        title = c.heading.title
        if not title and start < end:
            nxt = blocks[start]
            if (
                nxt.kind == "paragraph"
                and len(nxt.text) <= MAX_FOLLOWING_TITLE_CHARS
                and nxt.text[:1].isupper()
                and parse_item_heading(nxt.text) is None
            ):
                title, start = nxt.text.rstrip(" .:"), start + 1
        sections.append(
            Section(
                code=c.code,
                title=title or titles[c.heading.item if kind == "10-K" else c.code],
                heading_text=c.text,
                heading_index=c.block,
                start=min(start, end),
                end=end,
                anchor=blocks[c.block].anchor,
            )
        )
    return sections
