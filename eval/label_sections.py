"""Pick-not-type labeling loop for the section gold template.

    uv run python -m eval.label_sections [--by shekar] [--redo] [--filing TICKER_or_ACCESSION]
        [--template eval/sets/drafts/sections_gold_template.jsonl]
        [--out-dir data/fixture_candidates/labeling]

For each filing in the template it walks the five target items (1, 1A, 7, 7A, 8) one at a time and
lists that item's candidate lines as numbered options, each with the text around it, so you can
tell the real section start from a table-of-contents line or a cross-reference. Per item:

    <number>      the candidate that is the true start of the section
    absent        the filing has no such section
    manual        enter the position yourself (a character offset, or `line N`) when no candidate is
                  the real start; it snaps to the start of that line and asks you to confirm
    show <n>      a longer look at candidate n          n / p   next / previous page of candidates
    skip          leave it for later                    back    redo the previous item
    quit          stop (every answer is already saved)  ?       help

Each answer is written to the template immediately (`present`, `true_start_offset`,
`labeled_by`), so you can quit at any time and a rerun resumes at the first unlabeled item
(`--redo` walks the labeled ones again).

INDEPENDENCE. The gold must not be derived from the parser it grades. This module builds on
`eval.labeling_harness` only (neutral regex hits over plain text) and imports nothing from
`pipelines` (a test enforces it). The candidates come from the template; the surrounding text
comes from the text file. Nothing here knows how the parser divides a filing, and no parser output
is displayed. `first_sentence` is left alone: run `labeling_harness fill`, then `check`, afterwards.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval.labeling_harness import (
    DEFAULT_OUT,
    DEFAULT_TEMPLATE,
    REQUIRED,
    find_hits,
    load_texts,
    read_jsonl,
    write_jsonl,
)

DEFAULT_BY = "shekar"
BEFORE = 60  # characters shown before a candidate
AFTER = 200  # characters shown after it
SHOW_BEFORE, SHOW_AFTER = 200, 900  # the `show` command
PAGE = 8  # candidates per page
WIDTH = 100
ITEM_NAMES = {
    "1": "Business",
    "1A": "Risk Factors",
    "7": "Management's Discussion and Analysis",
    "7A": "Quantitative and Qualitative Disclosures About Market Risk",
    "8": "Financial Statements and Supplementary Data",
}
HELP = """\
  <number>   pick that candidate as the true start of the section
  absent     the filing has no such section
  manual     enter a character offset or `line N` yourself (also: `manual 4237`, `manual line 103`)
  show <n>   longer look at candidate n
  n / p      next / previous page of candidates
  skip       leave this item for later
  back       go back to the previous item and redo it
  quit       stop; everything answered so far is saved"""

Ask = Callable[[str], str]
Say = Callable[[str], None]


# -- text helpers -------------------------------------------------------------------------


def snippet(text: str, offset: int, before: int = BEFORE, after: int = AFTER) -> str:
    """The text around an offset on one line: `…before▶at and after…`, line breaks as ⏎."""
    lo = max(0, offset - before)
    head = text[lo:offset].replace("\n", " ⏎ ")
    tail = text[offset : offset + after].replace("\n", " ⏎ ")
    return f"{'…' if lo else ''}{head}▶{tail}{'…' if offset + after < len(text) else ''}"


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def snap_to_line_start(text: str, offset: int) -> int:
    return text.rfind("\n", 0, offset) + 1


def offset_of_line(text: str, number: int) -> int | None:
    if number < 1:
        return None
    pos = 0
    for _ in range(number - 1):
        pos = text.find("\n", pos)
        if pos < 0:
            return None
        pos += 1
    return pos if pos < len(text) else None


def parse_position(text: str, raw: str) -> int | None:
    """`4237` is a character offset, `line 103` / `l 103` a line number; None if unusable."""
    words = raw.lower().replace(",", "").split()
    try:
        if len(words) == 2 and words[0] in ("line", "l"):
            return offset_of_line(text, int(words[1]))
        if len(words) == 1:
            n = int(words[0])
            return snap_to_line_start(text, n) if 0 <= n < len(text) else None
    except ValueError:
        return None
    return None


def describe(text: str, cand: dict[str, Any], number: int, hit_offsets: list[int]) -> str:
    off = cand["offset"]
    later = next((o for o in hit_offsets if o > off), None)
    gap = f"next 'Item' line in {later - off:,} chars" if later is not None else "last 'Item' line"
    head = (
        f"  [{number}] line {cand.get('line', line_number(text, off))} · "
        f"{100 * off / len(text):.1f}% into the filing · {gap}"
    )
    body = textwrap.fill(
        snippet(text, off), width=WIDTH, initial_indent="      ", subsequent_indent="      "
    )
    return f"{head}\n{body}"


# -- template state -----------------------------------------------------------------------


def verify_candidates(rows: list[dict[str, Any]], texts: dict[str, str]) -> list[str]:
    """Candidates must sit at their offsets and saved starts on a line start, else it is stale."""
    problems = []
    for n, row in enumerate(rows, start=1):
        text = texts[row["text_file"]]
        for c in row["candidates"]:
            if text[c["offset"] : c["offset"] + len(c["text"])] != c["text"]:
                problems.append(
                    f"row {n} ({row['accession']} {row['section']}): candidate at offset "
                    f"{c['offset']} does not match {row['text_file']}"
                )
        offset = row.get("true_start_offset")
        if row.get("present") and offset is not None:
            if not 0 <= offset < len(text) or (offset and text[offset - 1] != "\n"):
                problems.append(
                    f"row {n} ({row['accession']} {row['section']}): saved offset {offset} is not "
                    f"the start of a line in {row['text_file']}"
                )
    return problems


def set_label(row: dict[str, Any], *, present: bool, offset: int | None, by: str) -> None:
    if (
        row.get("true_start_offset") != offset
    ):  # a first_sentence taken from another offset is stale
        row["first_sentence"] = ""
    row["present"] = present
    row["true_start_offset"] = offset
    row["labeled_by"] = by


def order_conflicts(rows: list[dict[str, Any]], index: int, offset: int) -> list[str]:
    """Sections of one filing run in item order; flag labeled items that contradict `offset`."""
    me = rows[index]
    mine = REQUIRED.index(me["section"])
    out = []
    for other in rows:
        if other is me or other["accession"] != me["accession"]:
            continue
        if not other.get("present") or other.get("true_start_offset") is None:
            continue
        theirs = REQUIRED.index(other["section"])
        o = other["true_start_offset"]
        if (theirs < mine and o >= offset) or (theirs > mine and o <= offset):
            out.append(
                f"Item {me['section']} at {offset:,} is out of order with Item "
                f"{other['section']} at {o:,}"
            )
    return out


def atomic_save(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    write_jsonl(tmp, rows)
    os.replace(tmp, path)


# -- the loop -----------------------------------------------------------------------------


@dataclass
class Result:
    labeled_now: int
    unlabeled: int
    quit: bool


def label_row(
    rows: list[dict[str, Any]],
    index: int,
    text: str,
    hit_offsets: list[int],
    *,
    by: str,
    ask: Ask,
    say: Say,
    save: Callable[[], None],
) -> str:
    """Returns 'done', 'skip', 'back' or 'quit'."""
    row = rows[index]
    cands = row["candidates"]
    pages = max(1, math.ceil(len(cands) / PAGE))
    page, redraw = 0, True

    def answer(present: bool, offset: int | None) -> str:
        set_label(row, present=present, offset=offset, by=by)
        save()
        if offset is not None:
            for message in order_conflicts(rows, index, offset):
                say(f"  ! {message}. A table-of-contents pick? `back` redoes the previous item.")
        return "done"

    while True:
        if redraw:
            if cands:
                lo = page * PAGE
                say(
                    f"  {len(cands)} candidate line(s), "
                    f"showing {lo + 1}-{min(lo + PAGE, len(cands))}:"
                )
                for n, cand in enumerate(cands[lo : lo + PAGE], start=lo + 1):
                    say(describe(text, cand, n, hit_offsets))
            else:
                say(
                    "  No candidate lines start with this item. "
                    "`absent`, or `manual` if you can find it."
                )
            redraw = False
        try:
            raw = ask("  pick #, absent, manual, show #, n/p, skip, back, quit, ? > ").strip()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        command, _, arg = raw.partition(" ")
        command, arg = command.lower(), arg.strip()
        if raw.isascii() and raw.isdigit():
            number = int(raw)
            if 1 <= number <= len(cands):
                return answer(True, cands[number - 1]["offset"])
            say(f"  There is no candidate {number}.")
        elif command in ("absent", "a"):
            return answer(False, None)
        elif command in ("manual", "m"):
            try:
                value = arg or ask("  character offset, or `line N`: ").strip()
            except (EOFError, KeyboardInterrupt):
                return "quit"
            offset = parse_position(text, value)
            if offset is None:
                say("  That is not a position inside this filing's text.")
                continue
            say(textwrap.fill(snippet(text, offset), width=WIDTH, initial_indent="    ",
                              subsequent_indent="    "))  # fmt: skip
            try:
                sure = ask(f"  true start at line {line_number(text, offset)}? [y/N] ").strip()
            except (EOFError, KeyboardInterrupt):
                return "quit"
            if sure.lower() in ("y", "yes"):
                return answer(True, offset)
        elif (
            command in ("show", "v")
            and arg.isascii()
            and arg.isdigit()
            and 1 <= int(arg) <= len(cands)
        ):
            offset = cands[int(arg) - 1]["offset"]
            say(textwrap.fill(snippet(text, offset, SHOW_BEFORE, SHOW_AFTER), width=WIDTH,
                              initial_indent="    ", subsequent_indent="    "))  # fmt: skip
        elif command in ("n", "next"):
            if page + 1 < pages:
                page, redraw = page + 1, True
            else:
                say("  That was the last page.")
        elif command in ("p", "prev"):
            if page > 0:
                page, redraw = page - 1, True
            else:
                say("  That is the first page.")
        elif command in ("skip", "s"):
            return "skip"
        elif command in ("back", "b"):
            return "back"
        elif command in ("quit", "q"):
            return "quit"
        elif command in ("?", "h", "help"):
            say(HELP)
        else:
            say("  I did not understand that. `?` lists the commands.")


def selected(rows: list[dict[str, Any]], only: str | None) -> list[int]:
    keep = [
        i
        for i, r in enumerate(rows)
        if only is None
        or only.lower() in (r["accession"].lower(), r["text_file"].split("_")[0].lower())
    ]
    if not keep:
        raise ValueError(f"no filing in the template matches {only!r}")
    return keep


def run_session(
    rows: list[dict[str, Any]],
    texts: dict[str, str],
    *,
    by: str,
    save: Callable[[], None],
    ask: Ask,
    say: Say,
    redo: bool = False,
    only: str | None = None,
) -> Result:
    order = selected(rows, only)
    accessions = list(dict.fromkeys(r["accession"] for r in rows))
    hits = {f: [h["offset"] for h in find_hits(t)] for f, t in texts.items()}
    labeled_now, cursor, last_filing = 0, 0, None
    frontier: int | None = None  # where `back` left from: items before it are all revisited
    quit_now = False
    while 0 <= cursor < len(order):
        index = order[cursor]
        row = rows[index]
        if frontier is not None and cursor >= frontier:
            frontier = None
        if not redo and frontier is None and row["present"] is not None:
            cursor += 1
            continue
        if row["accession"] != last_filing:
            last_filing = row["accession"]
            say(
                f"\n=== {row['text_file'].split('_')[0]}  {row['accession']}  "
                f"(filing {accessions.index(last_filing) + 1} of {len(accessions)}; "
                f"text: {row['text_file']})"
            )
        done = sum(r["present"] is not None for r in rows)
        say(
            f"\n--- Item {row['section']}: {ITEM_NAMES[row['section']]}   "
            f"[{done} of {len(rows)} labeled]"
        )
        if row["present"] is not None:
            where = f"starts at offset {row['true_start_offset']:,}" if row["present"] else "absent"
            say(f"  (currently answered: {where}; `skip` keeps it)")
        result = label_row(
            rows, index, texts[row["text_file"]], hits[row["text_file"]],
            by=by, ask=ask, say=say, save=save,
        )  # fmt: skip
        if result == "quit":
            quit_now = True
            break
        if result == "back":
            if cursor > 0:
                frontier = cursor if frontier is None else frontier
                cursor -= 1
            continue
        labeled_now += result == "done"
        cursor += 1
    return Result(labeled_now, sum(rows[i]["present"] is None for i in order), quit_now)


# -- command line -------------------------------------------------------------------------


def main(argv: list[str] | None = None, *, ask: Ask = input, say: Say = print) -> int:
    parser = argparse.ArgumentParser(description="Label section starts by picking candidates.")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--by", default=DEFAULT_BY, help="written to labeled_by")
    parser.add_argument("--redo", action="store_true", help="walk already-labeled items again")
    parser.add_argument("--filing", help="only this ticker or accession number")
    args = parser.parse_args(argv)

    if not args.template.is_file():
        say(
            f"No template at {args.template}. "
            "Create it with: python -m eval.labeling_harness prepare"
        )
        return 2
    rows = read_jsonl(args.template)
    try:
        texts = load_texts(rows, args.out_dir)
    except FileNotFoundError as err:
        say(
            f"Missing text file {err.filename}. They are scratch files made by `prepare` from the "
            "cached filings. Recreate them WITHOUT touching your labels:\n"
            "  python -m eval.labeling_harness prepare --template /tmp/throwaway.jsonl"
        )
        return 2
    problems = verify_candidates(rows, texts)
    if problems:
        say("The text files do not match the template, so offsets would be wrong:")
        for p in problems[:5]:
            say(f"  {p}")
        return 2
    try:
        result = run_session(
            rows, texts, by=args.by, save=lambda: atomic_save(args.template, rows),
            ask=ask, say=say, redo=args.redo, only=args.filing,
        )  # fmt: skip
    except ValueError as err:
        say(str(err))
        return 2
    total = sum(r["present"] is not None for r in rows)
    say(f"\nLabeled {result.labeled_now} this session; {total} of {len(rows)} rows have an answer.")
    if result.unlabeled:
        say(f"{result.unlabeled} left. Run the same command to resume.")
    else:
        say(
            "All done. Next: python -m eval.labeling_harness fill   (proposes first_sentence)\n"
            "      then:      python -m eval.labeling_harness check"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
