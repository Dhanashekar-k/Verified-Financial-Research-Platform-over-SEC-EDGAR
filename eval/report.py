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


KAPPA_TARGET = 0.70  # M04 DoD: judge-vs-human kappa >= 0.7, or the disagreements documented


def kappa_for(run: dict[str, Any], record: dict[str, Any] | None) -> dict[str, Any] | None:
    """The judge-vs-human kappa that applies to ``run``, with where it came from.

    The run's own ``judge_kappa`` wins. Otherwise ``eval/judges/kappa.json`` applies only when it
    was measured on THIS run's answers (``results_run_id``) with this run's judge model and
    correctness rubric: a kappa from another run, model or rubric says nothing about this one.
    """
    fb = run["sets"].get("financebench")
    if not isinstance(fb, dict):
        return None
    if fb.get("judge_kappa") is not None:
        return {"kappa": fb["judge_kappa"], "n": None, "source": "results file"}
    cfg = run["config"]
    if (
        record
        and record.get("kappa") is not None
        and record.get("results_run_id") == run["run_id"]
        and record.get("judge_model") == cfg.get("judge")
        and record.get("rubric_version") == (cfg.get("judge_rubrics") or {}).get("correctness")
    ):
        return {
            "kappa": record["kappa"],
            "n": record.get("n"),
            "source": "eval/judges/kappa.json",
            "rubric": record.get("rubric_version"),
            "judge": record.get("judge_model"),
            "labels_sha256": record.get("labels_sha256"),
        }
    return None


def render(runs: list[dict[str, Any]], kappa_record: dict[str, Any] | None = None) -> str:
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
            if s.get("scoring_errors"):
                errs += f" / {s['scoring_errors']} scoring"
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
                    kd = kappa_for(r, kappa_record)
                    judge = (
                        f"kappa {kd['kappa']:.2f} (answered rows only)"
                        if kd
                        else "kappa NOT MEASURED"
                    )
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
    cal = [(r["run_id"], kappa_for(r, kappa_record)) for r in runs]
    if any(kd for _, kd in cal):
        out.append("\n### Judge calibration (FinanceBench correctness judge vs human labels)\n")
        out.append(
            "| run | kappa | n labeled (answered rows) | vs 0.70 target | rubric | judge model "
            "| labels sha256 "
            "| source |\n|---|---:|---:|---|---|---|---|---|"
        )
        for rid, kd in cal:
            if not kd:
                continue
            k = float(kd["kappa"])
            sha = str(kd.get("labels_sha256") or NM)[:12]
            out.append(
                f"| `{rid}` | {k:.3f} | {kd['n'] if kd['n'] is not None else NM} "
                f"| {'met' if k >= KAPPA_TARGET else 'below target'} | {kd.get('rubric') or NM} "
                f"| {kd.get('judge') or NM} | `{sha}` | {kd['source']} |"
            )
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
    ap.add_argument("--kappa-file", type=Path, default=Path("eval/judges/kappa.json"))
    args = ap.parse_args(argv)
    runs = select_runs(args.results, args.runs) if args.results.exists() else []
    kappa = json.loads(args.kappa_file.read_text()) if args.kappa_file.exists() else None
    args.page.write_text(splice(args.page.read_text(), render(runs, kappa)))
    print(f"updated {args.page} from {len(runs)} run(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
