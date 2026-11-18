"""Tests for ``eval/blast_radius.py``: the per-filing analysis, the summary and the sampling.

Synthetic HTML only (these test the measurement, not real filings): a complete 10-K, one missing
Item 7, one that never writes "Item 7A", one with a cross-reference only, a combined "Items 1 and
2" heading, and a two-registrant cover page.
"""

from __future__ import annotations

from datetime import date

import pytest

from eval.blast_radius import analyse_filing, choose_sample, sector_group, summarize
from pipelines.fetch.list_filings import FilingTarget
from pipelines.parsing.document import FilingMeta

NAMES = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()


def body(topic: str) -> str:
    return "".join(f"<p>{topic} {w} says revenue and margins moved with demand.</p>" for w in NAMES)


def heading(code: str, title: str) -> str:
    return f"<div>Item {code}. {title}</div>{body(title)}"


FULL = (
    heading("1", "Business") + heading("1A", "Risk Factors") + heading("7", "MD&amp;A")
    + heading("7A", "Market Risk") + heading("8", "Financial Statements")
)  # fmt: skip


def doc(inner: str, head: str = "") -> bytes:
    return f"<html><body>{head}{inner}<div>SIGNATURES</div></body></html>".encode()


def meta() -> FilingMeta:
    return FilingMeta(
        accession="0000000001-23-000001", cik=1, form="10-K", filed=date(2023, 11, 1),
        period_of_report=date(2023, 9, 30), primary_doc_url="https://x/a.htm",
    )  # fmt: skip


def cover(*ciks: str) -> str:
    tags = "".join(
        f'<ix:nonnumeric name="dei:EntityCentralIndexKey">{c}</ix:nonnumeric>' for c in ciks
    )
    return f"<div>{tags}</div>"


def test_a_complete_filing_misses_nothing():
    r = analyse_filing(doc(FULL), meta())

    assert r["missing_required"] == [] and r["missing_core"] == [] and r["parse_status"] == "ok"
    assert r["multi_registrant"] is False and r["combined_heading"] is False


def test_a_missing_core_item_is_reported_with_the_cause_no_mention():
    inner = (
        heading("1", "Business") + heading("1A", "Risk Factors")
        + heading("7A", "Market Risk") + heading("8", "Financial Statements")
    )  # fmt: skip

    r = analyse_filing(doc(inner), meta())

    assert r["missing_required"] == ["7"] and r["missing_core"] == ["7"]
    assert r["miss_causes"] == {"7": "no_mention"}
    assert r["parse_status"] == "partial"


def test_an_item_that_is_only_cross_referenced_is_mention_only():
    inner = (
        heading("1", "Business") + "<p>See Item 7A. for market risk.</p>"
        + heading("1A", "Risk Factors")
        + heading("7", "MD&amp;A") + heading("8", "Financial Statements")
    )  # fmt: skip

    r = analyse_filing(doc(inner), meta())

    assert r["missing_required"] == ["7A"] and r["missing_core"] == []
    assert r["miss_causes"] == {"7A": "mention_only"}


def test_two_cover_page_registrants_make_a_multi_registrant_filing():
    r = analyse_filing(doc(FULL, cover("0000753308", "0000037634")), meta())

    assert r["multi_registrant"] is True
    assert r["registrant_ciks"] == ["0000753308", "0000037634"]


def test_the_same_cik_repeated_is_one_registrant():
    assert (
        analyse_filing(doc(FULL, cover("0000320193", "0000320193")), meta())["multi_registrant"]
        is False
    )


def test_a_combined_items_heading_is_flagged():
    inner = "<div>Items 1 and 2. Business and Properties</div>" + body("x") + FULL

    assert analyse_filing(doc(inner), meta())["combined_heading"] is True


def row(group: str, missing: list[str], multi: bool = False, cause: str = "no_mention") -> dict:
    return {
        "group": group, "missing_required": missing,
        "missing_core": [m for m in missing if m != "7A"],
        "multi_registrant": multi, "combined_heading": False,
        "parse_status": "partial" if missing else "ok",
        "miss_causes": {m: cause for m in missing},
    }  # fmt: skip


def test_the_summary_counts_failures_by_item_group_and_registrant_type():
    rows = [
        row("utilities", ["7"], True),
        row("utilities", []),
        row("financials", ["7A"]),
        row("other", []),
    ]

    s = summarize(rows)

    assert (s["n"], s["n_missing_any_required"], s["n_missing_any_core"]) == (4, 2, 1)
    assert s["missing_by_item"] == {"1": 0, "1A": 0, "7": 1, "7A": 1, "8": 0}
    assert (
        s["n_multi_registrant"],
        s["multi_registrant_that_miss"],
        s["failing_that_are_multi_registrant"],
    ) == (1, 1, 1)
    assert s["by_group"]["utilities"] == {"n": 2, "missing_required": 1, "multi_registrant": 1}
    assert s["by_cause"] == {"no_mention": 2}


def test_sector_groups_follow_the_sic_ranges():
    assert [sector_group(s) for s in (4911, 4999, 6021, 6799, 3571, None)] == [
        "utilities", "utilities", "financials", "financials", "other", "unknown"
    ]  # fmt: skip


def test_the_sample_keeps_the_prior_filings_and_tops_up_utilities_and_financials():
    def t(cik: int, n: int = 1) -> FilingTarget:
        return FilingTarget(
            cik=cik, accession=f"{cik:010d}-23-{n:06d}", form="10-K", filed=date(2023, 3, 1),
            report_date=date(2022, 12, 31), primary_doc="a.htm", url=f"https://x/{cik}/{n}.htm",
        )  # fmt: skip

    targets = [t(c) for c in range(1, 41)] + [t(1, 2)]
    sic = {c: 4911 if c <= 10 else 6021 if c <= 20 else 3571 for c in range(1, 41)}
    prior = [t(30).accession]

    chosen = choose_sample(
        targets, sic, prior, n=20, seed=1, want={"utilities": 4, "financials": 4}
    )

    assert chosen[0].accession == t(30).accession
    groups = [sector_group(sic[c.cik]) for c in chosen]
    assert groups.count("utilities") >= 4 and groups.count("financials") >= 4
    assert len({c.cik for c in chosen}) == len(chosen) == 20
    assert choose_sample(targets, sic, prior, 20, 1, {"utilities": 4, "financials": 4}) == chosen


def test_an_item_listed_only_in_a_table_of_contents_table_is_toc_or_index_only():
    toc = (
        "<table><tr><td>Item 1.</td><td>Business</td><td>3</td></tr>"
        "<tr><td>Item 7.</td><td>MD&amp;A</td><td>40</td></tr>"
        "<tr><td>Item 8.</td><td>Financial Statements</td><td>90</td></tr></table>"
    )
    inner = (
        toc + heading("1A", "Risk Factors") + "<div>Business</div>" + body("b")
        + heading("7A", "Market Risk") + heading("8", "Financial Statements")
    )  # fmt: skip

    r = analyse_filing(doc(inner), meta())

    assert r["miss_causes"]["1"] == "toc_or_index_only"
    assert r["paragraph_candidates_per_missing"]["1"] == 0


def test_an_unlinked_heading_paragraph_the_chain_skipped_is_heading_paragraph_not_chosen():
    """Item 7 written after Item 8: the chain keeps the heavier in-order one and drops the other."""
    inner = (
        heading("1", "Business") + heading("1A", "Risk Factors")
        + heading("8", "Financial Statements")
        + body("big") * 5 + "<div>Item 7. MD&amp;A</div>" + body("small")
    )  # fmt: skip

    r = analyse_filing(doc(inner), meta())

    assert "7" in r["missing_required"]
    assert r["miss_causes"]["7"] == "heading_paragraph_not_chosen"
    assert r["paragraph_candidates_per_missing"]["7"] == 1


def test_text_before_the_first_heading_is_reported_as_a_share_of_the_document():
    cover = "<p>" + "Cover page and table of contents text. " * 30 + "</p>"
    complete = analyse_filing(doc(FULL, cover), meta())
    no_item_1 = (
        heading("1A", "Risk Factors") + heading("7", "MD&amp;A") + heading("7A", "Market Risk")
        + heading("8", "Financial Statements")
    )  # fmt: skip
    lost = analyse_filing(
        doc(heading("1", "Business") + no_item_1.replace("Item 1A", "Item 1A"), cover), meta()
    )

    assert 0 < complete["share_before_first_heading"] < 0.5
    assert complete["text_chars"] > 0
    nothing = analyse_filing(doc("<p>no headings at all</p>"), meta())
    assert nothing["share_before_first_heading"] == 1.0  # nothing is indexed
    assert lost["share_before_first_heading"] <= complete["share_before_first_heading"] + 0.05


def test_the_summary_reports_the_typical_share_for_complete_filings():
    rows = [
        {**row("other", []), "share_before_first_heading": 0.02},
        {**row("other", []), "share_before_first_heading": 0.04},
        {**row("other", ["1"]), "share_before_first_heading": 0.30},
    ]

    s = summarize(rows)

    assert s["median_share_before_first_heading_complete"] == pytest.approx(0.03)
    assert s["max_share_before_first_heading_complete"] == 0.04
