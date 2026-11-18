"""Tests for ``eval/label_sections.py``: the pick-not-type labeling loop. Sessions are scripted
through the `ask`/`say` hooks; the template and text files are real files in a temp directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eval.label_sections import (
    AFTER,
    PAGE,
    main,
    offset_of_line,
    order_conflicts,
    parse_position,
    set_label,
    snippet,
    verify_candidates,
)
from eval.labeling_harness import REQUIRED, find_hits, read_jsonl, template_rows, write_jsonl

BODY = "This paragraph is long enough to count as the body text of a section in the filing."
ACC = "0000000001-24-000001"
TEXT = "\n".join(
    [
        "Annual Report",  # a table of contents, then the body
        "Item 1.", "Business",
        "Item 1A.", "Risk Factors",
        "Item 7.", "MD&A",
        "Item 7A.", "Market Risk",
        "Item 8.", "Financial Statements",
        "PART I",
        "Item 1. Business", BODY,
        "Item 1A. Risk Factors", BODY,
        "Item 7. Management's Discussion", BODY,
        "Item 7A. Market Risk", BODY,
        "Item 8. Financial Statements", BODY,
    ]
)  # fmt: skip


class Script:
    """Scripted answers; records what the tool printed and asked."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.said: list[str] = []
        self.asked: list[str] = []

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def say(self, message: str) -> None:
        self.said.append(message)

    @property
    def out(self) -> str:
        return "\n".join(self.said)


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    out_dir = tmp_path / "labeling"
    out_dir.mkdir()
    (out_dir / "AAA_x.txt").write_text(TEXT, encoding="utf-8")
    template = tmp_path / "template.jsonl"
    write_jsonl(template, template_rows(ACC, "AAA_x.txt", find_hits(TEXT)))
    return template, out_dir


def run(workspace: tuple[Path, Path], script: Script, *flags: str) -> int:
    template, out_dir = workspace
    return main(
        ["--template", str(template), "--out-dir", str(out_dir), *flags],
        ask=script.ask,
        say=script.say,
    )


def saved(workspace: tuple[Path, Path]) -> dict[str, dict[str, Any]]:
    return {r["section"]: r for r in read_jsonl(workspace[0])}


def second_candidate(workspace: tuple[Path, Path], item: str) -> int:
    return saved(workspace)[item]["candidates"][1]["offset"]


def test_picking_the_body_candidate_for_every_item_writes_the_answer_as_you_go(workspace):
    script = Script("2", "2", "2", "2", "2")

    assert run(workspace, script) == 0

    rows = saved(workspace)
    for item in REQUIRED:
        assert rows[item]["present"] is True
        assert rows[item]["true_start_offset"] == second_candidate(workspace, item)
        assert rows[item]["labeled_by"] == "shekar"
        assert rows[item]["first_sentence"] == ""  # left to `labeling_harness fill`
    assert "labeling_harness fill" in script.out


def test_the_first_candidate_is_the_toc_line_so_the_pick_really_selects_by_number(workspace):
    run(workspace, Script("1", "q"))

    assert saved(workspace)["1"]["true_start_offset"] == TEXT.index("Item 1.\n")


def test_absent_records_no_offset(workspace):
    run(workspace, Script("absent", "q"))

    row = saved(workspace)["1"]
    assert (row["present"], row["true_start_offset"], row["labeled_by"]) == (False, None, "shekar")


def test_the_label_name_is_a_flag(workspace):
    run(workspace, Script("2", "q"), "--by", "someone")

    assert saved(workspace)["1"]["labeled_by"] == "someone"


def test_a_rerun_resumes_at_the_first_unlabeled_item(workspace):
    run(workspace, Script("2", "2", "q"))
    again = Script("q")

    run(workspace, again)

    assert "Item 7:" in again.out and "Item 1:" not in again.out and "Item 1A:" not in again.out
    assert "[2 of 5 labeled]" in again.out


def test_redo_walks_the_labeled_items_again_and_replaces_the_answer(workspace):
    run(workspace, Script("2", "q"))
    redo = Script("1", "q")

    run(workspace, redo, "--redo")

    assert "Item 1:" in redo.out
    assert saved(workspace)["1"]["true_start_offset"] == TEXT.index("Item 1.\n")


def test_manual_takes_a_character_offset_snaps_to_the_line_start_and_asks_to_confirm(workspace):
    middle = TEXT.index("Item 1. Business") + 5
    run(workspace, Script(f"manual {middle}", "y", "q"))

    assert saved(workspace)["1"]["true_start_offset"] == TEXT.index("Item 1. Business")


def test_manual_takes_a_line_number_and_does_nothing_unless_confirmed(workspace):
    declined = Script("m", "line 3", "n", "q")
    run(workspace, declined)
    assert saved(workspace)["1"]["present"] is None
    assert "character offset, or `line N`" in declined.asked[1]

    run(workspace, Script("m line 13", "y", "q"))

    assert saved(workspace)["1"]["true_start_offset"] == TEXT.index("Item 1. Business")


def test_manual_rejects_a_position_outside_the_text_and_asks_again(workspace):
    script = Script("manual 99999999", "manual line 0", "manual abc", "q")

    run(workspace, script)

    assert script.out.count("not a position inside") == 3
    assert saved(workspace)["1"]["present"] is None


def test_skip_leaves_the_item_open_and_moves_on(workspace):
    script = Script("skip", "2", "q")

    run(workspace, script)

    rows = saved(workspace)
    assert rows["1"]["present"] is None and rows["1A"]["present"] is True
    resumed = Script("q")
    run(workspace, resumed)
    assert "Item 1:" in resumed.out  # the skipped item is asked again


def test_back_redoes_the_previous_item_then_continues_forward(workspace):
    script = Script("1", "back", "2", "2", "q")

    run(workspace, script)

    rows = saved(workspace)
    assert rows["1"]["true_start_offset"] == second_candidate(workspace, "1")  # redone
    assert rows["1A"]["true_start_offset"] == second_candidate(workspace, "1A")
    assert script.out.count("Item 1A:") == 2 and script.out.count("Item 1:") == 2


def test_back_on_the_first_item_stays_on_it(workspace):
    script = Script("back", "2", "q")

    run(workspace, script)

    assert saved(workspace)["1"]["true_start_offset"] == second_candidate(workspace, "1")


def test_picking_out_of_item_order_prints_a_warning_but_keeps_the_answer(workspace):
    script = Script("2", "1", "q")  # Item 1 body, then Item 1A's TOC line before it

    run(workspace, script)

    assert "out of order with Item 1" in script.out
    assert saved(workspace)["1A"]["present"] is True


def test_order_conflicts_checks_both_directions():
    rows = [
        {"accession": "a", "section": "1", "present": True, "true_start_offset": 500},
        {"accession": "a", "section": "7", "present": True, "true_start_offset": 100},
        {"accession": "b", "section": "1A", "present": True, "true_start_offset": 1},
        {"accession": "a", "section": "1A", "present": None, "true_start_offset": None},
    ]

    assert len(order_conflicts(rows, 3, 300)) == 2  # after Item 7 and before Item 1
    assert order_conflicts(rows, 3, 600) == ["Item 1A at 600 is out of order with Item 7 at 100"]


def test_the_screen_lists_each_candidate_with_its_context_and_nothing_from_the_parser(workspace):
    template, _ = workspace
    rows = read_jsonl(template)
    rows[0]["candidates"][0]["parser_section"] = "SECRET-PARSER-OUTPUT"
    write_jsonl(template, rows)
    script = Script("q")

    run(workspace, script)

    assert "[1] line 2" in script.out and "[2] line 13" in script.out
    assert "[3]" not in script.out
    assert (
        "▶Item 1. Business" in script.out
        and "Management's Discussion" not in script.out.split("Item 1A:")[0]
    )
    assert "SECRET-PARSER-OUTPUT" not in script.out
    assert "into the filing" in script.out and "next 'Item' line in" in script.out


def test_a_long_candidate_list_is_paged_and_any_number_can_be_picked(workspace):
    template, out_dir = workspace
    lines = ["intro"] + ["Item 8. Financial Statements", BODY] * 20
    text = "\n".join(lines)
    (out_dir / "AAA_x.txt").write_text(text, encoding="utf-8")
    write_jsonl(template, template_rows(ACC, "AAA_x.txt", find_hits(text)))
    script = Script("skip", "skip", "skip", "skip", "n", "p", "p", "n", "n", "n", "21", "13", "q")

    run(workspace, script)

    assert f"showing 1-{PAGE}" in script.out and f"showing {PAGE + 1}-{2 * PAGE}" in script.out
    assert "That is the first page." in script.out and "That was the last page." in script.out
    assert "There is no candidate 21." in script.out
    assert saved(workspace)["8"]["true_start_offset"] == find_hits(text)[12]["offset"]


def test_show_prints_a_longer_look_at_one_candidate(workspace):
    script = Script("show 2", "show 9", "q")

    run(workspace, script)

    long_look = next(m for m in script.said if "Management's" in m or "Item 1. Business" in m)
    assert len(long_look) > AFTER and "I did not understand" in script.out


def test_an_item_with_no_candidates_says_so_and_a_number_is_refused(workspace):
    template, out_dir = workspace
    text = "intro\n" + "\n".join(["Item 1. Business", BODY])
    (out_dir / "AAA_x.txt").write_text(text, encoding="utf-8")
    write_jsonl(template, template_rows(ACC, "AAA_x.txt", find_hits(text)))
    script = Script("2", "absent", "1", "q")  # Item 1 has one candidate; Item 1A has none

    run(workspace, script)

    rows = saved(workspace)
    assert "There is no candidate 2." in script.out
    assert rows["1"]["present"] is False
    assert "No candidate lines start with this item" in script.out
    assert rows["1A"]["present"] is None  # "1" is not a candidate of an item that has none


def test_end_of_input_quits_cleanly_and_keeps_what_was_answered(workspace):
    script = Script("2")  # then EOF

    assert run(workspace, script) == 0

    assert saved(workspace)["1"]["present"] is True
    assert "Run the same command to resume." in script.out


def test_an_unknown_command_and_help_do_not_change_anything(workspace):
    script = Script("banana", "?", "q")

    run(workspace, script)

    assert "I did not understand" in script.out and "pick that candidate" in script.out
    assert saved(workspace)["1"]["present"] is None


def test_filing_filter_selects_by_ticker_or_accession_and_an_unknown_one_is_an_error(workspace):
    assert run(workspace, Script("q"), "--filing", "AAA") == 0
    assert run(workspace, Script("q"), "--filing", ACC) == 0
    script = Script("q")

    assert run(workspace, script, "--filing", "ZZZ") == 2
    assert "no filing in the template matches 'ZZZ'" in script.out


def test_a_changed_offset_clears_a_first_sentence_taken_from_the_old_one():
    row: dict[str, Any] = {
        "true_start_offset": 10,
        "first_sentence": "old sentence",
        "present": True,
    }

    set_label(row, present=True, offset=10, by="s")
    assert row["first_sentence"] == "old sentence"  # same offset: keep what was typed
    set_label(row, present=True, offset=99, by="s")
    assert row["first_sentence"] == ""


def test_stale_text_files_are_refused_before_anything_is_asked(workspace):
    _, out_dir = workspace
    (out_dir / "AAA_x.txt").write_text("totally different text " * 50, encoding="utf-8")
    script = Script("2")

    assert run(workspace, script) == 2

    assert "do not match the template" in script.out and script.asked == []
    assert saved(workspace)["1"]["present"] is None


def test_missing_text_files_are_reported_with_a_command_that_keeps_the_labels(workspace):
    _, out_dir = workspace
    (out_dir / "AAA_x.txt").unlink()
    script = Script()

    assert run(workspace, script) == 2

    assert "WITHOUT touching your labels" in script.out


def test_verify_candidates_compares_the_text_at_each_offset():
    rows = template_rows(ACC, "t", find_hits(TEXT))

    assert verify_candidates(rows, {"t": TEXT}) == []
    assert len(verify_candidates(rows, {"t": "x" * len(TEXT)})) > 0


def test_position_helpers():
    assert offset_of_line("a\nbb\nccc", 3) == 5
    assert offset_of_line("a\nbb\nccc", 4) is None and offset_of_line("a", 0) is None
    assert parse_position("a\nbb\nccc", "line 2") == 2 and parse_position("a\nbb\nccc", "l 3") == 5
    assert parse_position("a\nbb\nccc", "4") == 2  # inside line 2: snaps to its start
    assert parse_position("a\nbb\nccc", "1,000") is None
    assert snippet("abc\ndef", 4, before=10, after=10) == "abc ⏎ ▶def"


def test_no_temporary_file_is_left_beside_the_template(workspace):
    run(workspace, Script("2", "q"))

    assert [p.name for p in workspace[0].parent.iterdir() if p.name.startswith(".")] == []


def test_superscript_digits_are_not_numbers_and_do_not_crash(workspace):
    script = Script("²", "show ²", "q")

    assert run(workspace, script) == 0

    assert script.out.count("I did not understand") == 2


def test_two_backs_in_a_row_revisit_every_item_in_between(workspace):
    # 1, 1A and 7 answered; on 7A: back (to 7), back (to 1A), redo 1A, then 7 is shown again
    script = Script("2", "2", "2", "back", "back", "2", "skip", "q")

    run(workspace, script)

    items = [
        m.split(":")[0].replace("--- Item ", "").strip() for m in script.said if "--- Item" in m
    ]
    assert items == ["1", "1A", "7", "7A", "7", "1A", "7", "7A"]
    assert "currently answered: starts at offset" in script.out
    assert saved(workspace)["7"]["present"] is True  # `skip` on an answered item keeps its answer


def test_back_then_skip_keeps_the_revisited_answer_and_moves_forward(workspace):
    script = Script("2", "back", "skip", "q")

    run(workspace, script)

    assert saved(workspace)["1"]["true_start_offset"] == second_candidate(workspace, "1")
    assert script.out.count("Item 1A:") == 2


def test_back_then_quit_saves_and_a_rerun_resumes_at_the_first_unlabeled_item(workspace):
    run(workspace, Script("2", "back", "q"))
    again = Script("q")

    run(workspace, again)

    assert "Item 1A:" in again.out and "Item 1:" not in again.out


def test_back_across_a_filing_boundary_reprints_the_filing_header(workspace):
    template, out_dir = workspace
    (out_dir / "BBB_y.txt").write_text(TEXT, encoding="utf-8")
    other = "0000000002-24-000002"
    write_jsonl(
        template,
        template_rows(ACC, "AAA_x.txt", find_hits(TEXT))
        + template_rows(other, "BBB_y.txt", find_hits(TEXT)),
    )
    script = Script("2", "2", "2", "2", "2", "back", "2", "q")  # back from BBB item 1, redo AAA 8

    run(workspace, script)

    assert script.out.count("=== AAA") == 2 and script.out.count("=== BBB") == 2
    assert "filing 2 of 2" in script.out


def test_back_works_together_with_the_filing_filter_and_with_redo(workspace):
    script = Script("2", "back", "2", "q")

    run(workspace, script, "--filing", "aaa")

    assert saved(workspace)["1"]["present"] is True
    redo = Script("back", "skip", "back", "skip", "q")
    run(workspace, redo, "--redo")
    assert redo.out.count("Item 1:") >= 2


def test_the_filing_filter_ignores_case(workspace):
    assert run(workspace, Script("q"), "--filing", "aaa") == 0


def test_a_saved_offset_that_is_not_a_line_start_is_reported_as_a_stale_file():
    rows = template_rows(ACC, "t", find_hits(TEXT))
    rows[0].update(present=True, true_start_offset=TEXT.index("Item 1. Business") + 3)
    rows[1].update(present=True, true_start_offset=10**9)
    rows[2].update(present=True, true_start_offset=TEXT.index("Item 1. Business"))

    problems = verify_candidates(rows, {"t": TEXT})

    assert [p.split(" (")[0] for p in problems] == ["row 1", "row 2"]
