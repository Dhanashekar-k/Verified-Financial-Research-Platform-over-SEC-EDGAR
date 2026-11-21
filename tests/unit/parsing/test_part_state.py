"""10-Q part state and the implied `P1-1` (ADR-031).

Two defects made 1,226 10-Qs in 78 companies miss both `P1-1` and `P1-2` on the full corpus:

1. A contents table that names `Part I` and `Part II` left the running part at II, so the body's
   `Item 1` / `Item 2` (which carry no Part of their own) were coded `P2-*`, or not at all.
2. Some 10-Qs print no `Item 1` heading in the body: the financial statements follow the contents
   table. Their first heading is `P1-2`, and everything before it was dropped.

The synthetic cases always run. The real-filing cases need filings that are cached under
`data/fixture_candidates/part_state` (gitignored) and skip without them.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from pipelines.parsing.html_blocks import Block, html_to_blocks
from pipelines.parsing.sections import (
    MIN_IMPLIED_CHARS,
    Section,
    detect_sections,
)

REPO = Path(__file__).resolve().parents[3]
CACHE = REPO / "data/fixture_candidates/part_state"


def P(text: str, link: bool = False, anchor: str | None = None) -> Block:
    return Block("paragraph", text, anchor=anchor, is_link=link)


def body(n_chars: int = 400) -> Block:
    return P(("Filler sentence about the business. " * (n_chars // 36 + 1))[:n_chars])


def contents_table(*, with_part_ii: bool = True) -> Block:
    """A table of contents in one table block, as Humana, Moderna and Corning print it."""
    rows = [
        ("Part I", "Financial Information", ""),
        ("Item 1.", "Financial Statements", "3"),
        ("Item 2.", "Management's Discussion and Analysis", "20"),
        ("Item 3.", "Quantitative and Qualitative Disclosures About Market Risk", "30"),
    ]
    if with_part_ii:
        rows += [
            ("Part II", "Other Information", ""),
            ("Item 1.", "Legal Proceedings", "33"),
            ("Item 1A.", "Risk Factors", "33"),
        ]
    return Block("table", "\n".join(" | ".join(r) for r in rows), rows=tuple(rows))


def codes(sections: list[Section]) -> list[str]:
    return [s.code for s in sections]


# --------------------------------------------------------------------------------------
# defect 1: a contents table must not move the part for the body
# --------------------------------------------------------------------------------------


def test_a_contents_table_naming_both_parts_does_not_leave_the_body_in_part_ii():
    blocks = [
        body(300),
        contents_table(),
        P("Item 1. Financial Statements"),
        body(8000),
        P("Item 2. Management's Discussion and Analysis"),
        body(3000),
        P("PART II. OTHER INFORMATION"),
        P("Item 1. Legal Proceedings"),
        body(500),
    ]

    assert codes(detect_sections(blocks, "10-Q")) == ["P1-1", "P1-2", "P2-1"]


def test_an_item_with_no_part_line_before_it_is_a_part_i_item():
    """Humana prints its body with no `Part I` line at all: Items 1 and 2 are Part I until a body
    `Part II` line says otherwise."""
    blocks = [
        body(300),
        P("Item 1. Financial Statements"),
        body(8000),
        P("Item 2. Management's Discussion and Analysis"),
        body(3000),
    ]

    assert codes(detect_sections(blocks, "10-Q")) == ["P1-1", "P1-2"]


def test_a_body_part_ii_line_still_moves_the_part():
    blocks = [
        P("Item 1. Financial Statements"),
        body(8000),
        P("PART II"),
        P("Item 1. Legal Proceedings"),
        body(500),
    ]

    assert codes(detect_sections(blocks, "10-Q")) == ["P1-1", "P2-1"]


def test_a_contents_table_naming_one_part_still_sets_the_part():
    """Only a table that names two Parts is a contents table; a one-row `PART II` heading table
    keeps moving the part as before."""
    part_ii = Block("table", "PART II", rows=(("PART II",),))
    blocks = [
        P("Item 1. Financial Statements"),
        body(8000),
        part_ii,
        P("Item 1. Legal Proceedings"),
        body(500),
    ]

    assert codes(detect_sections(blocks, "10-Q")) == ["P1-1", "P2-1"]


def test_a_10k_is_not_affected_by_a_contents_table_naming_two_parts():
    blocks = [
        contents_table(),
        P("Item 1. Business"),
        body(3000),
        P("Item 1A. Risk Factors"),
        body(3000),
    ]

    assert codes(detect_sections(blocks, "10-K")) == ["1", "1A"]


# --------------------------------------------------------------------------------------
# defect 2: the implied P1-1
# --------------------------------------------------------------------------------------


def corning_like(financials_chars: int) -> list[Block]:
    """Cover, contents table, financial statements WITHOUT an `Item 1` heading, then Item 2."""
    return [
        body(300),  # 0 cover
        contents_table(),  # 1
        P("Consolidated Statements of Income"),  # 2: first block of the financials
        body(financials_chars),  # 3
        P("Item 2. Management's Discussion and Analysis"),  # 4
        body(20000),  # heavier than the financials, as in a real filing, so the body chain wins
        P("Item 3. Quantitative and Qualitative Disclosures About Market Risk"),
        body(500),
        P("PART II. OTHER INFORMATION"),
        P("Item 1. Legal Proceedings"),
        body(500),
    ]


def test_a_10q_without_an_item_1_heading_indexes_its_financials_under_p1_1():
    blocks = corning_like(8000)

    sections = detect_sections(blocks, "10-Q")

    assert codes(sections) == ["P1-1", "P1-2", "P1-3", "P2-1"]
    p11 = sections[0]
    assert (p11.start, p11.end) == (2, 4)  # the block after the contents table, up to Item 2
    assert p11.heading_index == 1  # the contents table
    assert p11.title == "Financial Statements"
    assert p11.heading_text == ""  # there is no heading to quote
    assert p11.anchor is None  # nothing to link to: the table of contents is not P1-1's heading
    assert blocks[p11.start].text == "Consolidated Statements of Income"


def test_the_implied_p1_1_ends_where_the_first_real_heading_starts():
    blocks = corning_like(8000)

    p11, p12 = detect_sections(blocks, "10-Q")[:2]

    assert p11.end == p12.heading_index


def test_the_implied_p1_1_needs_at_least_the_minimum_amount_of_text():
    """The guard: a few hundred characters between the contents table and `Item 2` are a stray
    paragraph, not financial statements, so no P1-1 is claimed."""
    too_small = corning_like(MIN_IMPLIED_CHARS // 4)

    assert codes(detect_sections(too_small, "10-Q"))[0] == "P1-2"


def test_the_minimum_is_inclusive_of_text_just_above_it():
    """Just above the minimum is enough (the guard is a size floor, not a ratio)."""
    just_enough = corning_like(MIN_IMPLIED_CHARS)  # plus the 33-character caption block

    assert codes(detect_sections(just_enough, "10-Q"))[0] == "P1-1"


def test_no_contents_table_means_nothing_is_implied():
    blocks = [
        body(8000),
        P("Item 2. Management's Discussion and Analysis"),
        body(3000),
    ]

    assert codes(detect_sections(blocks, "10-Q")) == ["P1-2"]


def test_a_filing_with_its_own_item_1_heading_is_unchanged_and_nothing_is_implied():
    blocks = [
        body(300),
        contents_table(),
        P("Item 1. Financial Statements", anchor="fs"),
        body(8000),
        P("Item 2. Management's Discussion and Analysis"),
        body(3000),
    ]

    sections = detect_sections(blocks, "10-Q")

    assert codes(sections) == ["P1-1", "P1-2"]
    assert sections[0].heading_text == "Item 1. Financial Statements"
    assert sections[0].heading_index == 2
    assert sections[0].anchor == "fs"


def test_a_10q_that_starts_in_part_ii_gets_no_implied_p1_1():
    """Only a chain that starts inside Part I is a Part I filing with a missing Item 1."""
    blocks = [
        body(8000),
        P("PART II. OTHER INFORMATION"),
        P("Item 1. Legal Proceedings"),
        body(500),
        P("Item 1A. Risk Factors"),
        body(500),
    ]

    assert codes(detect_sections(blocks, "10-Q")) == ["P2-1", "P2-1A"]


def test_a_10k_never_gets_an_implied_section():
    blocks = [
        contents_table(),
        P("Item 7. Management's Discussion and Analysis"),
        body(3000),
        P("Item 8. Financial Statements"),
        body(3000),
    ]

    sections = detect_sections(blocks, "10-K")

    assert codes(sections) == ["7", "8"]
    assert all(s.heading_text for s in sections)


# --------------------------------------------------------------------------------------
# real filings (cached, gitignored)
# --------------------------------------------------------------------------------------


def _sections(name: str) -> tuple[list[Block], dict[str, Section]]:
    path = CACHE / name
    if not path.exists():
        pytest.skip(f"{name} is not in {CACHE}")
    blocks = html_to_blocks(gzip.decompress(path.read_bytes()))
    return blocks, {s.code: s for s in detect_sections(blocks, "10-Q")}


def test_corning_10q_indexes_its_financial_statements_under_p1_1():
    """Corning 10-Q 0000024741-26-000279: no `Item 1` heading in the body. Before ADR-031 it had
    P2-2..P2-6 only and its financial statements were not under any Part I section."""
    blocks, sections = _sections("GLW_0000024741-26-000279.htm.gz")

    assert {"P1-1", "P1-2", "P1-3", "P1-4"} <= set(sections)
    p11 = sections["P1-1"]
    assert p11.heading_text == "" and p11.anchor is None
    assert blocks[p11.start].text.startswith("Consolidated Statements of Income")
    assert sum(len(b.text) for b in blocks[p11.start : p11.end]) > 50_000
    assert p11.end == sections["P1-2"].heading_index


def test_moderna_10q_item_1_is_p1_1_not_p2_1():
    """Moderna 10-Q 0001682852-26-000197: its contents table has a `Part II` row. Before ADR-031
    the body's `Item 1. Financial Statements` was coded P2-1 and its financials sat under Legal
    Proceedings."""
    blocks, sections = _sections("MRNA_0001682852-26-000197.htm.gz")

    assert sections["P1-1"].heading_text == "Item 1. Financial Statements"
    assert sections["P2-1"].title == "Legal Proceedings"
    assert sum(len(b.text) for b in blocks[sections["P2-1"].start : sections["P2-1"].end]) < 10_000
