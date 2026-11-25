"""Blind labeling session for the judge-calibration worksheet (M04 3.8).

``make label-calibration`` steps through ``eval/judges/drafts/calibration_worksheet.jsonl`` one
row at a time and shows only the question, the gold answer and the system's answer. Nothing the
judge produced is read or shown: the worksheet has none, and this module never opens a results
file. Keys: ``c`` correct, ``i`` incorrect, ``r`` refused (a reply that declines to answer
although it is not a formal refusal), ``s`` skip (come back to it), ``b`` back one row, ``q`` quit.

Progress is saved after every keystroke to ``eval/judges/drafts/calibration_labeled.jsonl`` (same
rows as the worksheet plus ``human_label``), so a quit or a crash loses nothing and the next run
resumes. ``eval/judges/calibration.jsonl`` is protected gold: this module refuses to write it.
When every row is labeled it prints the ``cp`` command Shekar runs to promote the file, and the
``make kappa`` command that follows.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from eval.judges.calibrate import LABELS, LABELS_PATH, WORKSHEET_PATH

DRAFTS_DIR = Path("eval/judges/drafts")
LABELED_PATH = DRAFTS_DIR / "calibration_labeled.jsonl"
KEYS = {"c": "correct", "i": "incorrect", "r": "refused"}
# Only these reach the screen. A row carrying anything else (a judge field, say) never shows it.
SHOWN: tuple[tuple[str, str], ...] = (
    ("question", "QUESTION"),
    ("gold_answer", "GOLD ANSWER"),
    ("answer_markdown", "SYSTEM ANSWER"),
)
PROMPT = "[c]orrect  [i]ncorrect  [r]efused  [s]kip  [b]ack  [q]uit > "
CONFIRM = "All rows labeled. [y] finish   [b] back to the last row   [v] review from row 1 > "


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    ids = [str(r["id"]) for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate ids in {path}")
    return rows


def load_labels(rows: Sequence[dict[str, Any]], labeled: Path) -> dict[str, str]:
    """Labels from an earlier session. Rows must be the worksheet's: same ids and answers."""
    if not labeled.exists():
        return {}
    prior = read_rows(labeled)
    by_id = {str(r["id"]): r for r in rows}
    labels: dict[str, str] = {}
    for p in prior:
        pid = str(p["id"])
        w = by_id.get(pid)
        if w is None or w.get("answer_markdown") != p.get("answer_markdown"):
            raise ValueError(
                f"{labeled} was made from a different worksheet ({pid} differs); move it aside "
                "or delete it to start over"
            )
        label = str(p.get("human_label", "")).strip()
        if label and label not in LABELS:
            raise ValueError(f"{labeled}: {pid} has label {label!r}, expected one of {LABELS}")
        if label:
            labels[pid] = label
    return labels


def check_out(out: Path, worksheet: Path) -> None:
    """``out`` must be a file under the drafts directory and not the worksheet itself: that
    rules out the protected labels file and every other gold path, however it is spelled."""
    resolved = out.resolve()
    if DRAFTS_DIR.resolve() not in resolved.parents:
        raise ValueError(f"--out must be a file under {DRAFTS_DIR}/ (got {out})")
    if resolved == worksheet.resolve():
        raise ValueError("--out must not be the worksheet itself")
    if resolved == LABELS_PATH.resolve():
        raise ValueError(f"refusing to write {LABELS_PATH}: it is Shekar's to promote")


def save(rows: Sequence[dict[str, Any]], labels: dict[str, str], out: Path) -> None:
    if out.resolve() == LABELS_PATH.resolve():
        raise ValueError(f"refusing to write {LABELS_PATH}: it is Shekar's to promote")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(
        "".join(
            json.dumps({**r, "human_label": labels.get(str(r["id"]), "")}, ensure_ascii=False)
            + "\n"
            for r in rows
        )
    )
    tmp.replace(out)  # a crash mid-write never leaves a half-written progress file


def render(row: dict[str, Any], pos: int, total: int, done: int, current: str | None) -> str:
    lines = ["", "=" * 78, f"row {pos + 1}/{total}   labeled {done}/{total}   id {row['id']}"]
    if current:
        lines.append(f"(your current label: {current})")
    for key, title in SHOWN:
        lines += ["", f"--- {title} ---"]
        for para in str(row.get(key, "")).split("\n"):
            lines.append(textwrap.fill(para, width=100) if para.strip() else "")
    lines.append("")
    return "\n".join(lines)


def run_session(
    rows: Sequence[dict[str, Any]],
    labels: dict[str, str],
    out: Path,
    *,
    ask: Callable[[str], str] | None = None,
    show: Callable[[str], None] = print,
) -> bool:
    """Label until every row has a label (True) or the user quits (False).

    Starts at the first unlabeled row. Skipped rows stay unlabeled and come round again after
    the last row; ``b`` steps back (and shows the label you gave, which you may change).
    """
    ask = ask or input  # looked up at call time
    total = len(rows)
    ids = [str(r["id"]) for r in rows]

    def next_unlabeled(after: int) -> int | None:
        """The next unlabeled row after ``after``, wrapping round; ``after`` itself comes last."""
        order: list[int] = [*range(after + 1, total), *range(0, after + 1)]
        for i in order:
            if ids[i] not in labels:
                return i
        return None

    pos = next_unlabeled(-1)
    last = total - 1 if pos is None else pos  # the row a "back" from the confirm prompt reopens
    while True:
        if pos is None:  # everything is labeled: confirm, so the last keystroke can be undone
            show(f"\nlabeled {len(labels)}/{total}: {dict(Counter(labels.values()))}")
            try:
                key = ask(CONFIRM).strip().lower()
            except EOFError:
                save(rows, labels, out)
                return False  # not confirmed: the next session asks again
            if key == "y":
                save(rows, labels, out)
                return True
            if key == "b":
                pos = last
            elif key == "v":
                pos = 0
            else:
                show("press y, b or v")
            continue
        show(render(rows[pos], pos, total, len(labels), labels.get(ids[pos])))
        try:
            key = ask(PROMPT).strip().lower()
        except EOFError:
            key = "q"
        if key in KEYS:
            labels[ids[pos]] = KEYS[key]
            save(rows, labels, out)  # after every label: a quit or crash loses nothing
            last = pos
            pos = next_unlabeled(pos)
        elif key == "s":
            nxt = next_unlabeled(pos)
            if nxt == pos:
                show("(this is the only unlabeled row left: label it, or quit)")
            else:
                pos = nxt
        elif key == "b":
            pos = max(pos - 1, 0)
        elif key == "q":
            save(rows, labels, out)
            return False
        else:
            show("press c, i, r, s, b or q")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.judges.label")
    parser.add_argument("--worksheet", type=Path, default=WORKSHEET_PATH)
    parser.add_argument("--out", type=Path, default=LABELED_PATH)
    args = parser.parse_args(argv)
    try:
        check_out(args.out, args.worksheet)
    except ValueError as err:
        print(f"refusing: {err}")
        return 2
    if not args.worksheet.exists():
        print(
            f"{args.worksheet} not found: make calibration-worksheet RESULTS=<baseline run> first"
        )
        return 2
    rows = read_rows(args.worksheet)
    labels = load_labels(rows, args.out)
    if labels:
        print(f"resuming: {len(labels)}/{len(rows)} already labeled in {args.out}")
    complete = run_session(rows, labels, args.out)
    counts = Counter(labels.values())
    print(f"\nlabeled {len(labels)}/{len(rows)}: {dict(counts)}   saved to {args.out}")
    if not complete:  # a deliberate quit is not an error: `make` should not print one
        print("not confirmed; run `make label-calibration` again to resume or finish")
        return 0
    print(
        "\nAll rows labeled. To promote (protected gold, so you run it):\n"
        f"  cp {args.out} {LABELS_PATH}\n"
        "then, with the baseline run the worksheet was made from:\n"
        "  make kappa RESULTS=eval/results/<that run>.json"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
