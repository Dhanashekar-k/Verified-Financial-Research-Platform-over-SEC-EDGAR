"""Regenerate the tables in ``docs/results.md`` from ``eval/results/*.json``.

Only the text between ``<!-- AUTO:START -->`` and ``<!-- AUTO:END -->`` is rewritten; the rest
of the page is prose Shekar owns. Hand-typed numbers inside the markers are not allowed
(.claude/rules/eval.md), and this module is the only writer there. Every number printed comes
from a results file named in the table, with n and its 95% bootstrap CI; anything a run did not
measure prints as NOT MEASURED.

Run selection: the latest results file per (system, composer, split), or explicit ``--runs``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

START, END = "<!-- AUTO:START -->", "<!-- AUTO:END -->"
NM = "NOT MEASURED"


def _pct(v: Any) -> str:
    return NM if v is None else f"{100 * float(v):.1f}%"


def _ci(ci: Any) -> str:
    if not ci:
        return NM
    return f"[{100 * float(ci[0]):.1f}, {100 * float(ci[1]):.1f}]"


def select_runs(results_dir: Path, explicit: list[str] | None = None) -> list[dict[str, Any]]:
    files = sorted(results_dir.glob("*.json"))
    runs = [json.loads(p.read_text()) | {"_file": p.name} for p in files]
    runs = [r for r in runs if "run_id" in r and "sets" in r and "config" in r]
    # Stub-mode answers are fake; they never reach the published tables.
    runs = [r for r in runs if r["config"].get("mode") != "stub"]
    if explicit:
        wanted = set(explicit)
        return [r for r in runs if r["run_id"] in wanted]
    # Latest per configuration: a partial (--sets) or unjudged run never replaces a full one.
    latest: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in sorted(runs, key=lambda r: r["run_id"]):
        c = r["config"]
        key = (
            c.get("system"),
            c.get("composer"),
            c.get("split"),
            tuple(sorted(c.get("sets", []))),
            c.get("judge") is not None,
        )
        latest[key] = r
    return list(latest.values())


def render(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "_No results files yet: every metric is NOT MEASURED._"
    out: list[str] = []
    out.append("### Accuracy by set\n")
    out.append(
        "| run | split | set | n | n scored | errors | metric | value | 95% CI | judge |\n"
        "|---|---|---|---:|---:|---:|---|---:|---|---|"
    )
    for r in runs:
        rid, split = r["run_id"], r["config"].get("split")
        sets = r["sets"]
        for name in ("numeric", "financebench", "narrative", "unanswerable"):
            s = sets.get(name)
            if not isinstance(s, dict):
                continue
            n = s.get("n")
            errs = f"{s.get('errors', 0)} sys / {s.get('judge_errors', 0)} judge"
            if name == "unanswerable":
                rows = [
                    (
                        "refusal rate",
                        s.get("refusal_rate"),
                        s.get("refusal_rate_ci95"),
                        "-",
                        s.get("n_scored"),
                    )
                ]
            elif name == "narrative":
                rows = [
                    (
                        "gold-point coverage",
                        s.get("coverage"),
                        s.get("coverage_ci95"),
                        "uncalibrated",
                        s.get("coverage_n"),
                    ),
                    (
                        "contradiction rate",
                        s.get("contradiction"),
                        s.get("contradiction_ci95"),
                        "uncalibrated",
                        s.get("contradiction_n"),
                    ),
                ]
            else:
                judge = "-"
                if name == "financebench":
                    k = s.get("judge_kappa")
                    judge = f"kappa {k:.2f}" if k is not None else "kappa NOT MEASURED"
                rows = [("accuracy", s.get("accuracy"), s.get("ci95"), judge, s.get("n_scored"))]
                if name == "financebench":
                    rows.append(
                        (
                            "refusal rate",
                            s.get("refusal_rate"),
                            s.get("refusal_rate_ci95"),
                            "-",
                            s.get("n_scored"),
                        )
                    )
            for k in (5, 10, 20):
                if f"recall_at_{k}" in s:
                    rows.append(
                        (
                            f"recall@{k} (eval/metrics.py)",
                            s[f"recall_at_{k}"],
                            s.get(f"recall_at_{k}_ci95"),
                            "-",
                            s.get(f"recall_at_{k}_n"),
                        )
                    )
            for metric, value, ci, judge, n_scored in rows:
                scored = NM if n_scored is None else n_scored
                cells = [
                    f"`{rid}`",
                    split,
                    name,
                    n,
                    scored,
                    errs,
                    metric,
                    _pct(value),
                    _ci(ci),
                    judge,
                ]
                out.append("| " + " | ".join(str(c) for c in cells) + " |")
    out.append("\n### Cost and latency\n")
    out.append(
        "| run | mode | composer | questions | total $ | $ / question "
        "| p50 ms | p95 ms | ef_search |\n|---|---|---|---:|---:|---:|---:|---:|---:|"
    )
    for r in runs:
        c, cost, lat = r["config"], r["cost"], r["latency_ms"]
        p50 = NM if lat.get("p50") is None else f"{lat['p50']:.0f}"
        p95 = NM if lat.get("p95") is None else f"{lat['p95']:.0f}"
        out.append(
            f"| `{r['run_id']}` | {c.get('mode')} | {c.get('composer')} | {len(r['per_question'])} "
            f"| {float(cost['total_usd']):.4f} | {float(cost['per_question_usd']):.5f} "
            f"| {p50} | {p95} | {c.get('ef_search') if c.get('ef_search') is not None else NM} |"
        )
    fr = [(r["run_id"], r["sets"].get("answerable_false_refusal")) for r in runs]
    out.append("\n### False refusal on answerable sets\n")
    out.append("| run | false-refusal rate |\n|---|---:|")
    out += [f"| `{rid}` | {_pct(v)} |" for rid, v in fr]
    out.append(
        "\nSource: `eval/results/<run_id>.json`. Claims / unsupported-claim rate: NOT MEASURED "
        "until the support judge has chunk text to read (M6)."
    )
    return "\n".join(out)


def splice(page: str, body: str) -> str:
    if page.count(START) != 1 or page.count(END) != 1 or page.index(START) > page.index(END):
        raise ValueError("docs/results.md must contain exactly one AUTO:START ... AUTO:END block")
    head, rest = page.split(START, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{START}\n{body}\n{END}{tail}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Regenerate docs/results.md auto tables.")
    ap.add_argument("--results", type=Path, default=Path("eval/results"))
    ap.add_argument("--page", type=Path, default=Path("docs/results.md"))
    ap.add_argument("--runs", nargs="*", default=None, help="explicit run_ids to include")
    args = ap.parse_args(argv)
    runs = select_runs(args.results, args.runs) if args.results.exists() else []
    args.page.write_text(splice(args.page.read_text(), render(runs)))
    print(f"updated {args.page} from {len(runs)} run(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
