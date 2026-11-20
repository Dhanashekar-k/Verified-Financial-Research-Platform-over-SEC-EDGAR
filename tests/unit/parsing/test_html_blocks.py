"""Tests for ``pipelines/parsing/html_blocks.py``: filing HTML -> ordered paragraph/table blocks.

The HTML here is small and hand-written to isolate one behaviour each. It is synthetic and is NOT
a substitute for real filings: invariants over real filings live in ``test_real_fixtures.py``.
"""

from __future__ import annotations

import pytest

from pipelines.parsing.html_blocks import Block, html_to_blocks

WORDS = dict(
    enumerate(["", "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel"])
)


def doc(body: str) -> str:
    return f"<html><head><title>T</title><style>p{{x:y}}</style></head><body>{body}</body></html>"


def texts(blocks: list[Block]) -> list[str]:
    return [b.text for b in blocks]


def test_hidden_xbrl_header_and_display_none_are_dropped():
    html = doc(
        "<ix:header><ix:hidden><ix:nonnumeric>dei:EntityCentralIndexKey 0000320193</ix:nonnumeric>"
        '</ix:hidden></ix:header><div style="display: none">secret</div>'
        '<div style="DISPLAY:NONE;">also secret</div><div>Visible text.</div>'
    )

    assert texts(html_to_blocks(html)) == ["Visible text."]


def test_script_style_and_head_text_never_leak():
    html = (
        "<html><head><title>Cover</title></head>"
        "<body><script>var x=1</script><p>Body.</p></body></html>"
    )

    assert texts(html_to_blocks(html)) == ["Body."]


def test_nested_divs_become_separate_paragraphs_in_document_order():
    html = doc("<div><div>First.</div><div>Second <b>bold</b> part.</div></div><p>Third.</p>")

    assert texts(html_to_blocks(html)) == ["First.", "Second bold part.", "Third."]


def test_text_between_block_children_is_its_own_paragraph():
    html = doc("<div>before<div>middle</div>after</div>")

    assert texts(html_to_blocks(html)) == ["before", "middle", "after"]


def test_whitespace_nbsp_and_br_are_normalized():
    html = doc("<p>Item&nbsp;1A.<br>Risk\n   Factors</p>")

    assert texts(html_to_blocks(html)) == ["Item 1A. Risk Factors"]


def test_font_and_ix_wrappers_around_blocks_are_transparent():
    html = doc(
        "<font><div>Inside font.</div></font>"
        '<ix:nonnumeric name="n"><div>Note one.</div><div>Note two.</div></ix:nonnumeric>'
    )

    assert texts(html_to_blocks(html)) == ["Inside font.", "Note one.", "Note two."]


def test_inline_ix_tags_inside_a_paragraph_keep_the_paragraph_whole():
    html = doc('<p>Revenue was <ix:nonfraction name="r">383</ix:nonfraction> billion.</p>')

    assert texts(html_to_blocks(html)) == ["Revenue was 383 billion."]


def test_anchor_comes_from_the_block_own_id_a_nested_id_or_an_empty_preceding_anchor():
    html = doc(
        '<div id="own">One.</div>'
        '<div><span id="nested">Two.</span></div>'
        '<a id="empty"></a><div>Three.</div>'
        "<div>Four.</div>"
    )

    assert [(b.text, b.anchor) for b in html_to_blocks(html)] == [
        ("One.", "own"),
        ("Two.", "nested"),
        ("Three.", "empty"),
        ("Four.", None),
    ]


def test_an_id_on_a_container_attaches_to_its_first_block():
    html = doc('<div id="sec"><div>Heading</div><div>Body</div></div>')

    assert [(b.text, b.anchor) for b in html_to_blocks(html)] == [
        ("Heading", "sec"),
        ("Body", None),
    ]


def test_ids_that_internal_links_point_to_win_over_other_ids():
    html = doc(
        '<div><a href="#target">Item 1. Business</a></div>'
        '<div id="decor"><span id="target">Item 1. Business</span></div>'
    )

    assert html_to_blocks(html)[1].anchor == "target"


def test_a_paragraph_that_is_internal_link_text_is_marked_is_link():
    html = doc(
        '<div><a href="#x">Item 1A. Risk Factors</a></div>'
        '<div><a href="https://example.com/">External</a></div>'
        "<div>Plain.</div>"
    )

    assert [b.is_link for b in html_to_blocks(html)] == [True, False, False]


def test_data_tables_are_serialised_with_cleaned_cells_and_header_rows():
    html = doc(
        "<table>"
        "<tr><td>In millions</td><td>2023</td><td>2022</td></tr>"
        "<tr><td>Net sales</td><td>$</td><td>383,285</td><td></td><td>$</td><td>394,328</td></tr>"
        "<tr><td>Net loss</td><td>(</td><td>1,200</td><td>)</td><td>5</td><td>%</td></tr>"
        "</table>"
    )

    [table] = html_to_blocks(html)

    assert table.kind == "table"
    assert table.rows == (
        ("In millions", "2023", "2022"),
        ("Net sales", "$383,285", "$394,328"),
        ("Net loss", "(1,200)", "5%"),
    )
    assert table.header_rows == 1
    assert table.text.splitlines()[1] == "Net sales | $383,285 | $394,328"


def test_a_heading_table_without_amounts_becomes_one_paragraph_per_row():
    html = doc(
        "<table><tr><td>Item 1A.</td><td>Risk Factors</td></tr>"
        "<tr><td>Item 1B.</td><td>Unresolved Staff Comments</td></tr></table>"
    )

    blocks = html_to_blocks(html)

    assert [(b.kind, b.text) for b in blocks] == [
        ("paragraph", "Item 1A. Risk Factors"),
        ("paragraph", "Item 1B. Unresolved Staff Comments"),
    ]


def test_a_one_cell_layout_table_is_read_through_to_its_paragraphs():
    html = doc("<table><tr><td><div>Para one.</div><div>Para two.</div></td></tr></table>")

    assert texts(html_to_blocks(html)) == ["Para one.", "Para two."]


def test_a_table_of_contents_with_page_numbers_stays_a_table_block_with_rows():
    html = doc(
        "<table>"
        "<tr><td><a href='#a'>Item 1.</a></td><td>Business</td><td>1</td></tr>"
        "<tr><td><a href='#b'>Item 1A.</a></td><td>Risk Factors</td><td>5</td></tr>"
        "<tr><td><a href='#c'>Item 2.</a></td><td>Properties</td><td>17</td></tr>"
        "</table>"
    )

    [table] = html_to_blocks(html)

    assert table.kind == "table"
    assert table.rows[1] == ("Item 1A.", "Risk Factors", "5")


def test_bare_page_numbers_and_table_of_contents_backlinks_are_dropped():
    html = doc("<div>Real text.</div><div>12</div><div>Page 13</div><div>Table of Contents</div>")

    assert texts(html_to_blocks(html)) == ["Real text."]


def test_a_running_page_header_repeated_on_every_page_is_dropped_even_with_changing_numbers():
    pages = "".join(
        f"<div>Apple Inc. | 2023 Form 10-K | {n}</div><div>{WORDS[n]} sentence.</div>"
        for n in range(1, 9)
    )

    assert texts(html_to_blocks(doc(pages))) == [f"{WORDS[n]} sentence." for n in range(1, 9)]


MDA = (
    "Item 2. Management\u2019s Discussion and Analysis "
    "of Financial Condition and Results of Operations"
)


def test_an_item_heading_repeated_as_a_page_header_keeps_its_first_copy():
    """Berkshire's 10-Q repeats the Item 2 heading on every MD&A page (18 copies in the real
    filing): the first is the heading, the rest are page headers. Dropping all of them left the
    filing without an Item 2 (so `parse_status` was `partial`)."""
    pages = "".join(f"<div>{MDA}</div><div>{WORDS[n]} sentence.</div>" for n in range(1, 9))

    assert texts(html_to_blocks(doc(pages))) == [
        MDA,
        *[f"{WORDS[n]} sentence." for n in range(1, 9)],
    ]


def test_the_kept_copy_is_the_first_one_in_the_document():
    html = doc(
        "<div>Before.</div>"
        + "".join(f"<div>{MDA}</div><div>{WORDS[n]} sentence.</div>" for n in range(1, 8))
    )

    blocks = texts(html_to_blocks(html))

    assert blocks[:2] == ["Before.", MDA]
    assert blocks.count(MDA) == 1


def test_a_bare_item_label_repeated_on_every_page_is_still_dropped():
    """Microsoft's pages carry a bare "Item 1" navigation label (46+ copies per filing). It has no
    title, so it is not a heading and must not become one."""
    pages = "".join(f"<div>Item 1</div><div>{WORDS[n]} sentence.</div>" for n in range(1, 9))

    assert texts(html_to_blocks(doc(pages))) == [f"{WORDS[n]} sentence." for n in range(1, 9)]


def test_a_repeated_item_heading_below_the_threshold_is_untouched():
    html = doc(f"<div>{MDA}</div><div>x.</div><div>{MDA}</div><div>y.</div>")

    assert texts(html_to_blocks(html)) == [MDA, "x.", MDA, "y."]


def test_short_repeated_paragraphs_below_the_threshold_are_kept():
    html = doc("<div>None.</div><div>None.</div><div>None.</div>")

    assert texts(html_to_blocks(html)) == ["None.", "None.", "None."]


def test_bytes_are_decoded_as_utf8_then_cp1252():
    assert texts(html_to_blocks(doc("<p>café</p>").encode("utf-8"))) == ["café"]
    assert texts(html_to_blocks(b"<html><body><p>\x93quoted\x94</p></body></html>")) == ["“quoted”"]


@pytest.mark.parametrize("html", ["", "<html></html>", doc("")])
def test_an_empty_document_has_no_blocks(html: str):
    assert html_to_blocks(html) == []


def test_output_is_deterministic():
    html = doc(
        "<div id='a'>One.</div>"
        "<table><tr><td>a</td><td>1,000</td></tr>"
        "<tr><td>b</td><td>2,000</td><td>3</td></tr></table>"
    )

    assert html_to_blocks(html) == html_to_blocks(html)


def test_block_children_inside_a_table_cell_are_separated_by_spaces():
    html = doc(
        "<table>"
        "<tr><td><div>Three Months Ended</div><div>June 30,</div></td>"
        "<td>2023</td><td>2022</td></tr>"
        "<tr><td><p>Revenue</p><p>net</p></td><td>1,000</td><td>900</td></tr>"
        "<tr><td>Cost</td><td>500</td><td>450</td></tr>"
        "</table>"
    )

    [table] = html_to_blocks(html)

    assert table.rows[0][0] == "Three Months Ended June 30,"
    assert table.rows[1][0] == "Revenue net"


def test_spacer_rows_in_a_heading_table_do_not_shift_anchors_or_link_flags_onto_other_rows():
    html = doc(
        "<table>"
        "<tr><td>&nbsp;</td></tr>"
        '<tr><td><a href="#a1">Item 1. Business</a></td></tr>'
        "<tr><td>&nbsp;</td></tr>"
        '<tr><td id="body1">Item 2. Properties</td></tr>'
        "</table>"
    )

    blocks = html_to_blocks(html)

    assert [(b.text, b.is_link, b.anchor) for b in blocks] == [
        ("Item 1. Business", True, None),
        ("Item 2. Properties", False, "body1"),
    ]


def test_two_line_breaks_end_a_paragraph_but_one_is_just_a_space():
    html = doc("<p>Item 1.<br>Business<br><br>Our business is described below.</p>")

    assert texts(html_to_blocks(html)) == ["Item 1. Business", "Our business is described below."]


def test_the_first_piece_of_a_br_split_paragraph_keeps_the_anchor():
    html = doc('<p id="x">Item 1A.<br><br>Risk text.</p>')

    assert [(b.text, b.anchor) for b in html_to_blocks(html)] == [
        ("Item 1A.", "x"),
        ("Risk text.", None),
    ]
