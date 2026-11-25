"""Judge calibration: blind label template, Cohen's kappa, disagreement report (M04 3.8).

Workflow:
1. ``template``: sample 30 FinanceBench dev answers from a baseline results file and write
   them WITHOUT any judge output to ``eval/judges/drafts/calibration_template.jsonl``, with an
   empty ``human_label``. Rows that errored (``error`` set or no ``answer_markdown``) and
   refused answers are excluded before sampling: a refusal is labelled by code, not by the
   judge, so including it would inflate kappa with trivial agreement. Shekar labels the rows
   correct/incorrect blind and saves the result as ``eval/judges/calibration.jsonl``
   (protected gold; this module refuses to write it and never overwrites a template without
   ``--force``). ``template --all`` writes every answered row of a run (any split) to
   ``eval/judges/drafts/calibration_worksheet.jsonl`` instead; ``kappa --allow-n`` then scores
   that n.
2. ``kappa``: read Shekar's labels and the judge's labels, taken from the same results file
   (``judge_label`` / ``judge_reason`` per question, ``--results``) or from a JSONL of
   ``{"id", "label", "reason"}`` (``--judge``). It prints Cohen's kappa, writes
   ``eval/judges/kappa.json`` (kappa, n, judge model, rubric version, labels sha, run id) for
   ``eval/run.py`` to report next to FinanceBench accuracy, and can write the disagreement
   report. It fails on duplicate ids, and on n != 30 unless ``--allow-n`` is given.

All 30 labels go to the correctness judge (the headline FinanceBench metric); the support and
narrative judges are reported as uncalibrated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LABELS: tuple[str, ...] = ("correct", "incorrect", "refused")
LABELS_PATH = Path("eval/judges/calibration.jsonl")
TEMPLATE_PATH = Path("eval/judges/drafts/calibration_template.jsonl")
WORKSHEET_PATH = Path("eval/judges/drafts/calibration_worksheet.jsonl")
KAPPA_PATH = Path("eval/judges/kappa.json")
TARGET_N = 30
# Only these fields reach the labeler: nothing the judge produced.
TEMPLATE_FIELDS: tuple[str, ...] = ("id", "question", "gold_answer", "answer_markdown", "refusal")


def cohen_kappa(a: Sequence[str], b: Sequence[str], labels: Sequence[str] = LABELS) -> float | None:
    """Cohen's kappa between two raters over the same items.

    kappa = (p_o - p_e) / (1 - p_e), with p_e from each rater's marginal label frequencies.
    Returns ``None`` when p_e == 1: both raters used one and the same label for every item, so
    agreement is certain by chance and kappa is undefined (0/0). Reporting a number there
    would be invented; the caller prints "undefined (no label variation)".
    A label outside ``labels`` raises, so a typo in the human file fails loudly.
    """
    if len(a) != len(b):
        raise ValueError(f"raters cover different item counts: {len(a)} vs {len(b)}")
    if not a:
        raise ValueError("kappa needs at least one item")
    allowed = set(labels)
    bad = sorted({x for x in (*a, *b) if x not in allowed})
    if bad:
        raise ValueError(f"labels outside {sorted(allowed)}: {bad}")
    n = len(a)
    p_o = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    p_e = sum((ca[label] / n) * (cb[label] / n) for label in labels)
    if p_e >= 1.0:
        return None
    return (p_o - p_e) / (1.0 - p_e)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def make_label_template(
    results_json: Path,
    split: str | None = "dev",
    n: int | None = 30,
    seed: int = 13,
    out: Path = TEMPLATE_PATH,
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Sample ``n`` FinanceBench answers of ``split`` and write a blind labeling template.

    ``split=None`` takes every split and ``n=None`` takes every answered row (a worksheet over
    all of a run's answers, shuffled by ``seed`` so the order carries no hint).
    Errored rows and refused answers are excluded before sampling (see module docstring).
    Rows are sorted by id before seeded sampling, so the sample depends only on the results
    file's contents and the seed, not on row order. A row's split is its own ``split`` field
    when present, else the run's ``config.split``. Refuses to write the protected labels file,
    and refuses to overwrite an existing template (it may hold labels) unless ``force``.
    """
    if out.resolve() == LABELS_PATH.resolve():
        raise ValueError(f"refusing to write {LABELS_PATH}: it holds Shekar's labels")
    if out.exists() and not force:
        raise FileExistsError(f"{out} exists (it may already hold labels); pass force to replace")
    results = json.loads(Path(results_json).read_text())
    run_split = results.get("config", {}).get("split")
    pool = [
        row
        for row in results["per_question"]
        if row.get("set") == "financebench"
        and (split is None or row.get("split", run_split) == split)
        and not row.get("error")
        and row.get("answer_markdown") is not None
        and row.get("refusal") is None
    ]
    pool.sort(key=lambda row: str(row["id"]))
    if n is None:
        n = len(pool)
    if n < 1 or len(pool) < n:
        raise ValueError(f"only {len(pool)} FinanceBench {split or 'all'} answers, need {n}")
    missing = [f for f in TEMPLATE_FIELDS if f != "refusal" and any(f not in r for r in pool)]
    if missing:
        raise ValueError(f"results rows lack fields needed for labeling: {missing}")
    sample = random.Random(seed).sample(pool, n)
    rows = [{**{f: row.get(f) for f in TEMPLATE_FIELDS}, "human_label": ""} for row in sample]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return rows


def answered_financebench_rows(results: dict[str, Any]) -> list[dict[str, Any]]:
    """The FinanceBench rows a worksheet covers: answered, not errored, not refused."""
    return [
        r
        for r in results["per_question"]
        if r.get("set") == "financebench"
        and not r.get("error")
        and r.get("answer_markdown") is not None
        and r.get("refusal") is None
    ]


def check_labels_match_results(
    human_rows: list[dict[str, Any]], results_json: Path, *, cover_all: bool
) -> None:
    """Fail unless the labelled answers ARE this results file's answers.

    Joining by id alone would pair labels written against one run's answers with another
    run's judge labels (a rerun keeps the ids) and print a plausible kappa. Each labelled
    row's ``answer_markdown`` must equal the results row's; with ``cover_all`` the labels must
    also cover exactly the answered FinanceBench rows of the run (a worksheet, not a subset).
    """
    results = json.loads(Path(results_json).read_text())
    by_id = {str(r["id"]): r for r in results["per_question"]}
    stale = sorted(
        str(h["id"])
        for h in human_rows
        if str(h["id"]) not in by_id
        or by_id[str(h["id"])].get("answer_markdown") != h.get("answer_markdown")
    )
    if stale:
        raise ValueError(
            f"labels do not match run {results.get('run_id')}: answers differ or ids missing "
            f"for {stale[:5]}{' ...' if len(stale) > 5 else ''} (label the worksheet made from "
            "THIS results file)"
        )
    if cover_all:
        want = {str(r["id"]) for r in answered_financebench_rows(results)}
        have = {str(h["id"]) for h in human_rows}
        if want != have:
            raise ValueError(
                f"labels cover {len(have)} rows but the run has {len(want)} answered "
                f"FinanceBench rows (missing {len(want - have)}, extra {len(have - want)})"
            )


def judge_rows_from_results(results_json: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Judge labels stored per question by ``eval/run.py`` as ``{"id", "label", "reason"}``,
    plus the run's id and config."""
    results = json.loads(Path(results_json).read_text())
    rows = [
        {"id": r["id"], "label": r["judge_label"], "reason": r.get("judge_reason", "")}
        for r in results["per_question"]
        if r.get("judge_label") is not None
    ]
    return rows, {"run_id": results.get("run_id"), **results.get("config", {})}


def _check_unique(rows: list[dict[str, Any]], what: str) -> None:
    counts = Counter(str(r["id"]) for r in rows)
    dupes = sorted(i for i, c in counts.items() if c > 1)
    if dupes:
        raise ValueError(f"duplicate ids in {what}: {dupes}")


def paired_labels(
    human_rows: list[dict[str, Any]], judge_rows: list[dict[str, Any]]
) -> tuple[list[str], list[str], list[str]]:
    """Align human and judge labels by id. Returns (ids, human, judge) for ids in both.

    Unlabeled human rows (empty ``human_label``) or ids the judge lacks raise: kappa over a
    silently smaller set would overstate what was measured.
    """
    _check_unique(human_rows, "human labels")
    _check_unique(judge_rows, "judge output")
    judge_by_id = {str(r["id"]): r for r in judge_rows}
    ids, human, judge = [], [], []
    for row in human_rows:
        rid = str(row["id"])
        label = str(row.get("human_label", "")).strip()
        if not label:
            raise ValueError(f"{rid}: human_label is empty")
        if rid not in judge_by_id:
            raise ValueError(f"{rid}: no judge output for this id")
        ids.append(rid)
        human.append(label)
        judge.append(str(judge_by_id[rid]["label"]))
    return ids, human, judge


def disagreement_report(human_rows: list[dict[str, Any]], judge_rows: list[dict[str, Any]]) -> str:
    """Markdown listing every item where the judge and Shekar disagree."""
    ids, human, judge = paired_labels(human_rows, judge_rows)
    by_id = {str(r["id"]): r for r in human_rows}
    judge_by_id = {str(r["id"]): r for r in judge_rows}
    kappa = cohen_kappa(human, judge)
    kappa_txt = "undefined (no label variation)" if kappa is None else f"{kappa:.3f}"
    agree = sum(h == j for h, j in zip(human, judge, strict=True))
    lines = [
        "# Judge calibration: disagreements",
        "",
        f"n = {len(ids)}, agreement = {agree}/{len(ids)}, Cohen's kappa = {kappa_txt}",
        "",
    ]
    for rid, h, j in zip(ids, human, judge, strict=True):
        if h == j:
            continue
        row = by_id[rid]
        lines += [
            f"## {rid}: human `{h}`, judge `{j}`",
            "",
            f"- **Question:** {row.get('question', '')}",
            f"- **Gold:** {row.get('gold_answer', '')}",
            f"- **Answer:** {row.get('answer_markdown', '')}",
            f"- **Refusal:** {json.dumps(row.get('refusal'))}",
            f"- **Judge reason:** {judge_by_id[rid].get('reason', '')}",
            "",
        ]
    if agree == len(ids):
        lines.append("No disagreements.")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.judges.calibrate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("template", help="write the blind 30-answer labeling template")
    t.add_argument("--results", type=Path, required=True)
    t.add_argument("--split", default="dev")
    t.add_argument("--n", type=int, default=TARGET_N)
    t.add_argument("--seed", type=int, default=13)
    t.add_argument("--out", type=Path, default=None, help="default: template, or worksheet --all")
    t.add_argument(
        "--all",
        action="store_true",
        help="every answered FinanceBench row of any split (the worksheet), not a sample",
    )
    t.add_argument("--force", action="store_true", help="replace an existing template")
    k = sub.add_parser("kappa", help="Cohen's kappa of the judge against Shekar's labels")
    src = k.add_mutually_exclusive_group(required=True)
    src.add_argument("--results", type=Path, help="results JSON with judge_label per question")
    src.add_argument("--judge", type=Path, help="judge output JSONL (id, label, reason)")
    k.add_argument("--labels", type=Path, default=LABELS_PATH)
    k.add_argument("--report", type=Path, default=None, help="write the disagreement report")
    k.add_argument("--kappa-out", type=Path, default=KAPPA_PATH)
    k.add_argument("--allow-n", action="store_true", help=f"accept n != {TARGET_N}")
    k.add_argument(
        "--all",
        action="store_true",
        help="labels are the full worksheet (template --all): they must match the results "
        "file's answers and cover every answered FinanceBench row; n is then not limited to 30",
    )
    args = parser.parse_args(argv)

    if args.cmd == "template":
        out = args.out or (WORKSHEET_PATH if args.all else TEMPLATE_PATH)
        rows = make_label_template(
            args.results,
            None if args.all else args.split,
            None if args.all else args.n,
            args.seed,
            out,
            force=args.force,
        )
        print(f"wrote {len(rows)} blind rows to {out}; label them, save as {LABELS_PATH}")
        return 0

    if not args.labels.exists():
        print(
            f"{args.labels} not found: kappa needs Shekar's labels. Label the template "
            f"({TEMPLATE_PATH}) or the worksheet ({WORKSHEET_PATH}) and save it as "
            f"{args.labels} first.",
            file=sys.stderr,
        )
        return 2
    human_rows = _read_jsonl(args.labels)
    run_info: dict[str, Any] = {}
    if args.results is not None:
        try:
            check_labels_match_results(human_rows, args.results, cover_all=args.all)
        except ValueError as err:
            print(f"refusing: {err}", file=sys.stderr)
            return 4
        judge_rows, run_info = judge_rows_from_results(args.results)
        judge_rows = [r for r in judge_rows if str(r["id"]) in {str(h["id"]) for h in human_rows}]
    else:
        judge_rows = _read_jsonl(args.judge)
    _, human, judge = paired_labels(human_rows, judge_rows)
    if len(human) != TARGET_N and not (args.allow_n or args.all):
        print(
            f"n={len(human)} but the calibration target is {TARGET_N}; refusing to report a "
            "kappa from a different sample size (pass --allow-n to override)",
            file=sys.stderr,
        )
        return 3
    kappa = cohen_kappa(human, judge)
    print(f"n={len(human)} kappa={'undefined' if kappa is None else f'{kappa:.4f}'}")
    from eval.judges.correctness import RUBRIC_VERSION

    record = {
        "kappa": kappa,
        "n": len(human),
        "judge_model": run_info.get("judge"),
        "rubric_version": (run_info.get("judge_rubrics") or {}).get("correctness", RUBRIC_VERSION),
        "labels_sha256": hashlib.sha256(args.labels.read_bytes()).hexdigest(),
        "computed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "results_run_id": run_info.get("run_id"),
    }
    args.kappa_out.parent.mkdir(parents=True, exist_ok=True)
    args.kappa_out.write_text(json.dumps(record, indent=2) + "\n")
    print(f"wrote {args.kappa_out}")
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(disagreement_report(human_rows, judge_rows))
        print(f"wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
