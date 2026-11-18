"""Tests for ``eval/labeling_harness.py``: the parser-independent labeling aid for the gold set."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from eval.labeling_harness import (
    REQUIRED,
    check_rows,
    choose,
    fill_rows,
    find_hits,
    first_sentence,
    plain_text,
    read_jsonl,
    template_rows,
    toc_profile,
    write_jsonl,
)

HARNESS = Path(__file__).resolve().parents[2] / "eval" / "labeling_harness.py"
FIRST = "Our business designs and sells widgets to industrial customers worldwide."
LONG = f"{FIRST} More text follows."


def test_the_harness_imports_nothing_from_the_parser_it_helps_to_grade():
    tree = ast.parse(HARNESS.read_text())
    imported = [
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [None])
        if (n.module if isinstance(n, ast.ImportFrom) else a.name)
    ]

    forbidden = [
        m
        for m in imported
        if m.startswith(("pipelines.parsing", "eval.sections_eval", "eval.blast_radius"))
    ]
    assert forbidden == []


def test_plain_text_drops_scripts_styles_the_ixbrl_header_and_hidden_elements():
    html = (
        "<html><head><title>T</title></head><body><script>var x=1</script><style>p{}</style>"
        "<ix:header><ix:hidden>HIDDEN-FACT</ix:hidden></ix:header>"
        '<div style="display: none">secret</div>'
        "<div>Item&nbsp;1.</div><p>Business</p></body></html>"
    )

    assert plain_text(html) == "Item 1.\nBusiness"


def test_hits_are_lines_that_start_with_an_item_heading_with_their_character_offsets():
    text = (
        "Cover\nPART I\nItem 1A.\nRisk Factors\nWe see Item 7 elsewhere.\n"
        "ITEM 7A. Market Risk\nPart II Item 8. Statements"
    )

    hits = find_hits(text)

    assert [(h["code"], h["text"]) for h in hits] == [
        ("1A", "Item 1A."),
        ("7A", "ITEM 7A. Market Risk"),
        ("8", "Part II Item 8. Statements"),
    ]
    for h in hits:  # the offset is where the line starts in the text
        assert text[h["offset"] : h["offset"] + len(h["text"])] == h["text"]
        assert text.split("\n")[h["line"] - 1] == h["text"]
    assert hits[0]["context"] == "Risk Factors / We see Item 7 elsewhere. / ITEM 7A. Market Risk"


def test_a_combined_items_heading_and_an_item_code_inside_a_word_are_not_hits():
    assert find_hits("Items 1 and 2. Business\nItem 1ABC\nItem 12x") == []


def test_template_rows_cover_the_five_items_with_blank_fields_and_no_parser_output():
    hits = find_hits("Item 1A. Risk\nbody\nItem 7. MD&A\nItem 1A.\nmore")

    rows = template_rows("0000000001-23-000001", "x.txt", hits)

    assert [r["section"] for r in rows] == list(REQUIRED)
    assert set(rows[0]) == {
        "accession", "section", "first_sentence", "labeled_by", "text_file", "present",
        "true_start_offset", "candidates", "notes",
    }  # fmt: skip
    assert all(r["first_sentence"] == "" and r["labeled_by"] == "" for r in rows)
    assert all(r["present"] is None and r["true_start_offset"] is None for r in rows)
    one_a = next(r for r in rows if r["section"] == "1A")
    assert [c["line"] for c in one_a["candidates"]] == [1, 4]
    assert next(r for r in rows if r["section"] == "7A")["candidates"] == []


def test_first_sentence_skips_short_heading_lines_and_stops_at_the_first_full_stop():
    text = f"Item 1.\nBusiness\nGeneral\n{LONG}"

    assert (
        first_sentence(text, 0)
        == "Our business designs and sells widgets to industrial customers worldwide."
    )
    assert first_sentence("Item 1.\nshort", 0) is None


def test_fill_proposes_a_sentence_only_for_confirmed_rows_and_never_overwrites_a_typed_one():
    risks = "Risks are described here in considerable detail below."
    text = f"Item 1.\nBusiness\n{LONG}\nItem 1A.\nRisk Factors\n{risks}"
    rows = [
        {"text_file": "t", "present": True, "true_start_offset": 0, "first_sentence": ""},
        {
            "text_file": "t",
            "present": True,
            "true_start_offset": 0,
            "first_sentence": "typed by hand",
        },
        {"text_file": "t", "present": None, "true_start_offset": None, "first_sentence": ""},
        {"text_file": "t", "present": False, "true_start_offset": None, "first_sentence": ""},
    ]

    assert fill_rows(rows, {"t": text}) == 1
    assert rows[0]["first_sentence"].startswith("Our business designs")
    assert rows[1]["first_sentence"] == "typed by hand"
    assert rows[2]["first_sentence"] == rows[3]["first_sentence"] == ""


def test_check_flags_every_way_a_row_can_be_wrong_and_passes_a_good_one():
    text = f"Item 1.\nBusiness\n{LONG}"
    good = {
        "accession": "a", "section": "1", "text_file": "t", "present": True, "true_start_offset": 0,
        "first_sentence": FIRST,
        "labeled_by": "shekar",
    }  # fmt: skip
    bad = [
        {**good, "present": None},
        {**good, "present": False},
        {**good, "true_start_offset": None},
        {**good, "true_start_offset": 10**6},
        {**good, "first_sentence": ""},
        {**good, "first_sentence": "Something the filing never says anywhere in its text."},
        {**good, "labeled_by": " "},
    ]

    assert check_rows([good], {"t": text}) == []
    problems = check_rows(bad, {"t": text})
    assert len(problems) == len(bad)
    assert "present is not set" in problems[0] and "present is false" in problems[1]
    assert "does not appear" in problems[5] and "labeled_by is blank" in problems[6]


def row(acc: str, cik: int, size: int, style: str = "opaque", multi: bool = False, causes=None):
    return {
        "accession": acc, "cik": cik, "bytes": size, "toc_style": style,
        "multi_registrant": multi, "miss_causes": causes or {},
    }  # fmt: skip


def test_choose_is_stratified_deterministic_smallest_first_and_one_filing_per_company():
    rows = [
        row("o1", 1, 500), row("o2", 2, 100), row("o3", 3, 300),
        row("n1", 4, 400, "named"), row("n2", 5, 200, "named"),
        row("x1", 6, 150, "none"), row("x2", 7, 250, "none"),
        row("m1", 8, 120, multi=True), row("m2", 9, 130, multi=True),
        row("f1", 10, 900, causes={"1": "toc_or_index_only"}),
        row("f2", 11, 800, causes={"1A": "no_mention"}),
        row("all5", 12, 10, causes={i: "toc_or_index_only" for i in REQUIRED}),
        row("dup", 2, 50),  # same company as o2: skipped
    ]  # fmt: skip

    picked = choose(rows, {"multi_registrant": 1, "none": 1, "named": 1, "opaque": 1})

    assert [(p["accession"], p["stratum"]) for p in picked] == [
        ("f1", "missing_item:toc_or_index_only"),
        ("f2", "missing_item:no_mention"),
        ("m1", "multi_registrant"),
        ("x1", "toc_none"),
        ("n2", "toc_named"),
        ("dup", "toc_opaque"),  # the smaller of company 2's two filings; o2 is not also taken
    ]
    assert choose(rows, {"multi_registrant": 1, "none": 1, "named": 1, "opaque": 1}) == picked
    assert "all5" not in {
        p["accession"] for p in picked
    }  # a pure cross-reference index has no starts to label


def anchors(n: int, names: bool) -> bytes:
    def target(i: int) -> str:
        return f"item_{i}_x" if names else f"i{'0123456789abcdef' * 2}_{i}"

    links = "".join(f'<a href="#{target(i)}">Item {i}.</a>' for i in range(1, n + 1))
    divs = "".join(f'<div id="{target(i)}">x</div>' for i in range(1, n + 1))
    return f"<html><body>{links}{divs}</body></html>".encode()


def test_toc_profile_tells_opaque_named_and_no_anchor_tocs_apart():
    assert toc_profile(anchors(8, names=False))["style"] == "opaque"
    assert toc_profile(anchors(8, names=True))["style"] == "named"
    assert toc_profile(b"<html><body><div>Item 1.</div></body></html>")["style"] == "none"
    assert toc_profile(anchors(3, names=True))["style"] == "none"  # fewer than 5 TOC links


def test_jsonl_round_trips_with_non_ascii_text(tmp_path: Path):
    rows = [{"a": "café", "n": None}]

    write_jsonl(tmp_path / "x" / "t.jsonl", rows)

    assert read_jsonl(tmp_path / "x" / "t.jsonl") == rows
    assert "café" in (tmp_path / "x" / "t.jsonl").read_text(encoding="utf-8")


def test_the_committed_template_rows_have_only_the_neutral_fields():
    path = Path(__file__).resolve().parents[2] / "eval/sets/drafts/sections_gold_template.jsonl"
    if not path.is_file():
        pytest.skip("template not generated")

    for r in read_jsonl(path):
        assert set(r) == {
            "accession", "section", "first_sentence", "labeled_by", "text_file", "present",
            "true_start_offset", "candidates", "notes",
        }  # fmt: skip
        assert json.dumps(r["candidates"]).count("parser") == 0
