"""Tests for ``pipelines/parsing/sections.py``: item headings, TOC skipping, section extents.

Blocks are built directly (a heading, a filler paragraph of a given length, a TOC table) so each
test states the structure it is about. The synthetic structures are NOT a substitute for real
filings; ``test_real_fixtures.py`` checks the same invariants on the owner's fixture filings.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pipelines.parsing.html_blocks import Block
from pipelines.parsing.sections import (
    CODES_10K,
    CODES_10Q,
    Section,
    detect_sections,
    parse_item_heading,
)


def P(text: str, link: bool = False, anchor: str | None = None) -> Block:
    return Block("paragraph", text, anchor=anchor, is_link=link)


def body(n_chars: int = 400) -> Block:
    return P(("Filler sentence about the business. " * (n_chars // 36 + 1))[:n_chars])


def toc_table(*labels: tuple[str, str]) -> Block:
    rows = tuple((code, title, str(i + 1)) for i, (code, title) in enumerate(labels))
    return Block("table", "\n".join(" | ".join(r) for r in rows), rows=rows)


def codes(sections: list[Section]) -> list[str]:
    return [s.code for s in sections]


# --------------------------------------------------------------------------------------
# parse_item_heading
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code", "title"),
    [
        ("Item 1A. Risk Factors", ("1A", None), "Risk Factors"),
        (
            "ITEM 7.  MANAGEMENT'S DISCUSSION AND ANALYSIS",
            ("7", None),
            "MANAGEMENT'S DISCUSSION AND ANALYSIS",
        ),
        ("Item 7A — Quantitative and Qualitative", ("7A", None), "Quantitative and Qualitative"),
        ("Item 1B: Unresolved Staff Comments", ("1B", None), "Unresolved Staff Comments"),
        ("Item 1. Business", ("1", None), "Business"),
        ("Item 8", ("8", None), ""),
        ("PART II - Item 5. Market for Registrant", ("5", "II"), "Market for Registrant"),
        ("Part I, Item 2. Management's Discussion", ("2", "I"), "Management's Discussion"),
        ("Item 9A. Controls and Procedures.", ("9A", None), "Controls and Procedures"),
    ],
)
def test_item_headings_are_recognised_with_their_variants(text: str, code, title: str):
    parsed = parse_item_heading(text)

    assert parsed is not None
    assert (parsed.item, parsed.part) == code
    assert parsed.title == title


@pytest.mark.parametrize(
    "text",
    [
        "See Item 1A. Risk Factors for more information.",
        "Item 7 of this report discusses liquidity in more detail.",
        "Items discussed elsewhere",
        "Item 99. Unknown",
        "The item 1A heading",
        "",
        "Item 1A. " + "x" * 400,
    ],
)
def test_prose_and_unknown_items_are_not_headings(text: str):
    assert parse_item_heading(text) is None


# --------------------------------------------------------------------------------------
# detect_sections: TOC skipping
# --------------------------------------------------------------------------------------


def test_a_hyperlinked_toc_is_skipped_in_favour_of_the_body():
    blocks = [
        P("Table of contents text"),
        P("Item 1. Business", link=True),
        P("Item 1A. Risk Factors", link=True),
        P("Item 7. Management's Discussion", link=True),
        body(),
        P("Item 1. Business", anchor="b1"),
        body(2000),
        P("Item 1A. Risk Factors", anchor="b1a"),
        body(5000),
        P("Item 7. Management's Discussion", anchor="b7"),
        body(4000),
        P("SIGNATURES"),
        body(300),
    ]

    sections = detect_sections(blocks, "10-K")

    assert codes(sections) == ["1", "1A", "7"]
    assert [s.heading_index for s in sections] == [5, 7, 9]
    assert [s.anchor for s in sections] == ["b1", "b1a", "b7"]
    assert sections[-1].end == 11  # stops at SIGNATURES


def test_a_toc_table_without_links_is_skipped():
    blocks = [
        toc_table(("Item 1.", "Business"), ("Item 1A.", "Risk Factors"), ("Item 7.", "MD&A")),
        P("Item 1. Business"),
        body(3000),
        P("Item 1A. Risk Factors"),
        body(3000),
        P("Item 7. MD&A"),
        body(3000),
    ]

    assert [s.heading_index for s in detect_sections(blocks, "10-K")] == [1, 3, 5]


def test_an_unlinked_paragraph_toc_is_skipped_by_text_weight_alone():
    blocks = [
        P("Item 1. Business"),
        P("Item 1A. Risk Factors"),
        P("Item 7. Management's Discussion"),
        P("Item 8. Financial Statements"),
        P("Item 1. Business"),
        body(3000),
        P("Item 1A. Risk Factors"),
        body(3000),
        P("Item 7. Management's Discussion"),
        body(3000),
        P("Item 8. Financial Statements"),
        body(3000),
    ]

    assert [s.heading_index for s in detect_sections(blocks, "10-K")] == [4, 6, 8, 10]


def test_short_real_sections_come_from_the_body_not_the_toc():
    blocks = [
        P("Item 1A. Risk Factors", link=True),
        P("Item 1B. Unresolved Staff Comments", link=True),
        P("Item 2. Properties", link=True),
        P("Item 1A. Risk Factors"),
        body(5000),
        P("Item 1B. Unresolved Staff Comments"),
        P("None."),
        P("Item 2. Properties"),
        body(800),
    ]

    sections = detect_sections(blocks, "10-K")

    assert [(s.code, s.heading_index) for s in sections] == [("1A", 3), ("1B", 5), ("2", 7)]


def test_the_toc_twin_of_a_short_section_is_never_mixed_into_the_chain():
    """TOC '1B' sits before body '1A'; taking it would break the order, so the body wins."""
    blocks = [
        P("Item 1A. Risk Factors"),
        P("Item 1B. Unresolved Staff Comments"),
        P("Item 2. Properties"),
        P("Item 1A. Risk Factors"),
        body(4000),
        P("Item 1B. Unresolved Staff Comments"),
        P("None."),
        P("Item 2. Properties"),
        body(900),
    ]

    assert [s.heading_index for s in detect_sections(blocks, "10-K")] == [3, 5, 7]


def test_cross_references_do_not_become_sections():
    blocks = [
        P("Item 1A. Risk Factors"),
        body(3000),
        P("See Item 7. Management's Discussion for details."),
        P("Item 7 of this report describes our liquidity."),
        body(500),
        P("Item 7. Management's Discussion"),
        body(3000),
    ]

    assert [s.heading_index for s in detect_sections(blocks, "10-K")] == [0, 5]


def test_a_heading_with_the_title_in_the_next_block_takes_that_title_and_skips_it():
    blocks = [P("Item 1A."), P("Risk Factors"), body(3000), P("Item 7."), P("MD&A"), body(2000)]

    sections = detect_sections(blocks, "10-K")

    assert [(s.title, s.start) for s in sections] == [("Risk Factors", 2), ("MD&A", 5)]


def test_a_heading_with_no_title_anywhere_gets_the_canonical_title():
    blocks = [P("Item 1A."), body(3000), P("Item 7."), body(3000)]

    assert [s.title for s in detect_sections(blocks, "10-K")] == [
        "Risk Factors",
        "Management's Discussion and Analysis of Financial Condition and Results of Operations",
    ]


def test_the_last_section_runs_to_the_end_when_there_is_no_signatures_block():
    blocks = [P("Item 15. Exhibits"), body(1000)]

    assert detect_sections(blocks, "10-K")[0].end == 2


def test_a_repeated_item_heading_in_the_body_picks_the_one_with_more_text():
    blocks = [
        P("Item 7. MD&A"),
        body(5000),
        P("Item 7. MD&A (continued)"),
        body(100),
        P("Item 8. Financial Statements"),
        body(3000),
    ]

    assert [s.heading_index for s in detect_sections(blocks, "10-K")] == [0, 4]


def test_missing_items_are_simply_absent():
    blocks = [P("Item 1. Business"), body(2000), P("Item 8. Financial Statements"), body(2000)]

    assert codes(detect_sections(blocks, "10-K")) == ["1", "8"]


def test_10k_amendment_forms_use_the_10k_codes():
    blocks = [P("Item 1. Business"), body(2000)]

    assert codes(detect_sections(blocks, "10-K/A")) == ["1"]


def test_no_blocks_and_unknown_forms():
    assert detect_sections([], "10-K") == []
    with pytest.raises(ValueError, match="form"):
        detect_sections([P("Item 1. Business")], "8-K")


# --------------------------------------------------------------------------------------
# 10-Q: parts
# --------------------------------------------------------------------------------------


def test_10q_codes_include_the_part_and_disambiguate_item_numbers():
    blocks = [
        P("PART I - FINANCIAL INFORMATION"),
        P("Item 1. Financial Statements"),
        body(3000),
        P("Item 2. Management's Discussion and Analysis"),
        body(3000),
        P("Item 4. Controls and Procedures"),
        body(500),
        P("PART II - OTHER INFORMATION"),
        P("Item 1. Legal Proceedings"),
        body(500),
        P("Item 1A. Risk Factors"),
        body(2000),
        P("Item 2. Unregistered Sales of Equity Securities"),
        body(500),
    ]

    assert codes(detect_sections(blocks, "10-Q")) == [
        "P1-1",
        "P1-2",
        "P1-4",
        "P2-1",
        "P2-1A",
        "P2-2",
    ]


def test_10q_part_can_be_part_of_the_heading_itself():
    blocks = [P("Part II, Item 1A. Risk Factors"), body(2000)]

    assert codes(detect_sections(blocks, "10-Q")) == ["P2-1A"]


def test_10q_toc_with_parts_is_skipped():
    blocks = [
        P("PART I"),
        P("Item 1. Financial Statements", link=True),
        P("Item 2. Management's Discussion", link=True),
        P("PART II"),
        P("Item 1A. Risk Factors", link=True),
        P("PART I - FINANCIAL INFORMATION"),
        P("Item 1. Financial Statements"),
        body(3000),
        P("Item 2. Management's Discussion"),
        body(3000),
        P("PART II - OTHER INFORMATION"),
        P("Item 1A. Risk Factors"),
        body(3000),
    ]

    assert [(s.code, s.heading_index) for s in detect_sections(blocks, "10-Q")] == [
        ("P1-1", 6),
        ("P1-2", 8),
        ("P2-1A", 11),
    ]


# --------------------------------------------------------------------------------------
# invariants (property)
# --------------------------------------------------------------------------------------

_ITEM_TEXTS = [f"Item {c}. Heading" for c in CODES_10K]
_block = st.one_of(
    st.integers(min_value=1, max_value=3000).map(body),
    st.tuples(st.sampled_from(_ITEM_TEXTS), st.booleans()).map(lambda t: P(t[0], link=t[1])),
    st.just(P("SIGNATURES")),
)


@settings(max_examples=150, deadline=None)
@given(st.lists(_block, max_size=60))
def test_sections_are_ordered_non_overlapping_and_in_canonical_order(blocks: list[Block]):
    sections = detect_sections(blocks, "10-K")

    heads = [s.heading_index for s in sections]
    assert heads == sorted(set(heads))
    ranks = [CODES_10K.index(s.code) for s in sections]
    assert ranks == sorted(set(ranks))
    for a, b in zip(sections, sections[1:], strict=False):
        assert a.heading_index < a.start <= a.end <= b.heading_index
    for s in sections:
        assert 0 <= s.heading_index < len(blocks) and s.end <= len(blocks)


def test_the_10q_code_table_covers_both_parts():
    assert CODES_10Q[0] == "P1-1" and "P2-1A" in CODES_10Q and len(set(CODES_10Q)) == len(CODES_10Q)


def test_a_cross_reference_to_a_part_one_item_inside_part_two_does_not_flip_the_part():
    blocks = [
        P("PART I"),
        P("Item 1. Financial Statements"),
        body(3000),
        P("Item 2. Management's Discussion"),
        body(3000),
        P("PART II"),
        P("Item 1A. Risk Factors"),
        P(
            "Part I, Item 1A, \u201cRisk Factors\u201d in our 2023 Annual Report "
            "describes the risks."
        ),
        body(800),
        P("Item 2. Unregistered Sales of Equity Securities"),
        body(500),
        P("Item 6. Exhibits"),
        body(300),
    ]

    assert codes(detect_sections(blocks, "10-Q")) == [
        "P1-1",
        "P1-2",
        "P2-1A",
        "P2-2",
        "P2-6",
    ]


# --------------------------------------------------------------------------------------
# two narrow heading cases found by the 150-filing blast-radius survey
# --------------------------------------------------------------------------------------

FUSED = (
    "Part I Item 1. Business The Allstate Corporation was incorporated under the laws of the State "
    "of Delaware in 1992 and is a holding company for a number of insurance companies worldwide."
)


def test_a_heading_fused_with_the_first_sentence_is_a_heading_and_keeps_the_sentence():
    parsed = parse_item_heading(FUSED)

    assert parsed is not None
    assert (parsed.item, parsed.part, parsed.title) == ("1", "I", "Business")
    assert parsed.body.startswith("The Allstate Corporation was incorporated")


@pytest.mark.parametrize(
    "text",
    [
        "Item 7 of this report discusses our liquidity in more detail, including the matters "
        "set out below and the risks that could affect our results of operations in the future.",
        "Item 1. Business and other matters are discussed in this long sentence which is plainly "
        "prose and not a heading because the title is followed by lowercase words only.",
        "Item 2. Something else entirely that is far too long to be a heading and is not a known "
        "title for that item, so it must not be mistaken for one by the fused-heading rule at all.",
    ],
)
def test_long_prose_that_starts_like_an_item_reference_is_not_a_fused_heading(text: str):
    assert parse_item_heading(text) is None


def test_the_fused_sentence_becomes_the_first_text_of_the_section_not_lost():
    blocks = [
        P("PART I"),
        P(FUSED, anchor="b1"),
        body(2000),
        P("Item 1A. Risk Factors"),
        body(3000),
    ]

    [one, one_a] = detect_sections(blocks, "10-K")

    assert (one.code, one.title, one.heading_index, one.anchor) == ("1", "Business", 1, "b1")
    assert one.body_prefix.startswith("The Allstate Corporation")
    assert one_a.body_prefix == ""


@pytest.mark.parametrize("text", ["ITEM 8,", "Item 8,", "ITEM 7A,"])
def test_a_heading_that_ends_in_a_comma_is_the_start_of_a_cross_reference_not_a_heading(text: str):
    assert parse_item_heading(text) is None


@pytest.mark.parametrize(
    ("text", "item"),
    [
        ("ITEM 5. MARKET FOR THE REGISTRANT'S COMMON EQUITY,", "5"),
        ("Item 13. Certain Relationships and Related Transactions,", "13"),
    ],
)
def test_a_title_that_wraps_onto_the_next_line_still_ends_in_a_comma_and_is_a_heading(
    text: str, item: str
):
    """Found by diffing the 150-filing survey before and after the comma rule: PPL and Nasdaq."""
    parsed = parse_item_heading(text)

    assert parsed is not None and parsed.item == item


def test_a_comma_inside_a_heading_is_still_a_heading():
    parsed = parse_item_heading("Item 7, Management's Discussion and Analysis")

    assert parsed is not None and parsed.item == "7"


def test_a_cross_reference_fragment_does_not_displace_the_real_item_7a():
    """Everest: 'ITEM 8,' inside Item 7 sits before the real Item 7A, and used to win the order."""
    blocks = [
        P("Item 1A. Risk Factors"),
        body(3000),
        P("ITEM 8,"),
        P("\u201cFinancial Statements"),
        P("and"),
        P("Supplementary Data\u201d - Notes 1 and 3"),
        body(2000),
        P("ITEM 7A."),
        P("QUANTITATIVE"),
        body(1500),
        P("Item 9A. Controls and Procedures"),
        body(500),
    ]

    assert codes(detect_sections(blocks, "10-K")) == ["1A", "7A", "9A"]


# --------------------------------------------------------------------------------------
# "Item 1.A." (a dot before the letter): ADR-029
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "item", "title"),
    [
        ("Item 1.A. Risk Factors", "1A", "Risk Factors"),
        ("Item 1.B. Unresolved Staff Comments", "1B", "Unresolved Staff Comments"),
        (
            "Item 7.A. Quantitative and Qualitative Disclosures about Market Risk",
            "7A",
            "Quantitative and Qualitative Disclosures about Market Risk",
        ),
        ("ITEM 9.A. CONTROLS AND PROCEDURES", "9A", "CONTROLS AND PROCEDURES"),
        ("Item 1 . A. Risk Factors", "1A", "Risk Factors"),
        ("Item 1.A.", "1A", ""),
    ],
)
def test_a_dot_before_the_item_letter_is_the_lettered_item(text: str, item: str, title: str):
    parsed = parse_item_heading(text)

    assert parsed is not None
    assert (parsed.item, parsed.title) == (item, title)


@pytest.mark.parametrize(
    ("text", "item"),
    [
        (
            "Item 1. A new risk factor summary",
            "1",
        ),  # the article "A" after "Item 1." is not a letter
        ("Item 1.A Risk Factors", "1"),  # no dot after the letter: not the dotted form (ADR-029)
        ("Item 1. Business", "1"),
        ("Item 2. B2B services overview", "2"),
    ],
)
def test_a_letter_after_item_n_dot_that_is_not_dotted_stays_the_plain_item(text: str, item: str):
    parsed = parse_item_heading(text)

    assert parsed is not None
    assert parsed.item == item


def test_a_dotted_toc_is_skipped_and_the_dotted_body_headings_are_the_sections():
    """Rollins' layout: a stacked TOC of `Item 1.A.` rows, then the same headings in the body."""
    blocks = [
        P("Item 1."),
        P("Business."),
        P("Item 1.A."),
        P("Risk Factors."),
        P("Item 1.B."),
        P("Unresolved Staff Comments."),
        P("Item 7."),
        P("Management's Discussion and Analysis."),
        P("Item 7.A."),
        P("Quantitative and Qualitative Disclosures about Market Risk."),
        P("Item 8."),
        P("Financial Statements and Supplementary Data."),
        P("Item 1."),
        P("Business"),
        body(3000),
        P("Item 1.A."),
        P("Risk"),
        P("Factors"),
        body(3000),
        P("Item 1.B."),
        P("Unresolved Staff Comments"),
        P("None."),
        P("Item 7."),
        P("Management's Discussion and Analysis of Financial Condition and Results of Operations."),
        body(3000),
        P("Item 7.A."),
        P("Quantitative and Qualitative Disclosures about Market Risk"),
        body(3000),
        P("Item 8."),
        P("Financial Statements and Supplementary Data"),
        body(3000),
    ]

    sections = detect_sections(blocks, "10-K")

    assert codes(sections) == ["1", "1A", "1B", "7", "7A", "8"]
    by_code = {s.code: s for s in sections}
    assert by_code["1"].heading_index == 12  # the body heading, not the TOC row at 0
    assert by_code["1A"].heading_index == 15  # item 1A is no longer folded into item 1
    assert by_code["7A"].heading_index == 25
