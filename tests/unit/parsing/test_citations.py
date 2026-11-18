"""Tests for ``pipelines/parsing/citations.py`` (M02 section 3.3 step 6)."""

from __future__ import annotations

from pipelines.parsing.citations import citation_url, fragment_source

DOC = "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"


def test_an_anchor_id_becomes_a_plain_fragment():
    assert citation_url(DOC, "i4f7_52", "Risk text") == DOC + "#i4f7_52"


def test_without_an_anchor_the_first_eight_words_become_a_text_fragment():
    url = citation_url(DOC, None, "The Company is exposed to risks from one two three four five")

    assert url == DOC + "#:~:text=The%20Company%20is%20exposed%20to%20risks%20from%20one"


def test_text_fragment_directive_characters_are_percent_encoded():
    url = citation_url(DOC, None, "Net sales, up 5% - year-over-year & more")

    assert url.endswith(
        "#:~:text=Net%20sales%2C%20up%205%25%20%2D%20year%2Dover%2Dyear%20%26%20more"
    )


def test_non_ascii_text_is_utf8_percent_encoded_and_nbsp_is_a_space():
    url = citation_url(DOC, None, "Café revenue")

    assert url.endswith("#:~:text=Caf%C3%A9%20revenue")


def test_empty_text_and_no_anchor_gives_the_bare_url():
    assert citation_url(DOC, None, "  ") == DOC


def test_table_chunks_quote_the_first_label_not_the_pipes():
    assert (
        fragment_source("Net sales | $383,285 | $394,328\nCost | 1 | 2", is_table=True)
        == "Net sales"
    )
    assert (
        fragment_source("In millions | 2023 | 2022\nNet sales | 1 | 2", is_table=True)
        == "In millions"
    )


def test_a_blank_anchor_is_treated_as_no_anchor():
    assert citation_url(DOC, "", "Some words here") == DOC + "#:~:text=Some%20words%20here"


def test_a_fragment_never_spans_two_blocks():
    assert fragment_source("Overview\nOur business is large and varied.") == "Overview"
    assert citation_url(DOC, None, "Overview\nOur business is large.") == DOC + "#:~:text=Overview"
