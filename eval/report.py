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

from eval.stats import paired_bootstrap

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

    The run's own ``judge_kappa`` wins; when the harness stamped it from ``kappa.json`` for the same
    judge and rubric, the row says the labels were measured on another run and that answer identity
    is not checked here. Otherwise ``eval/judges/kappa.json`` applies only when it
    was measured on THIS run's answers (``results_run_id``) with this run's judge model and
    correctness rubric: a kappa from another run, model or rubric says nothing about this one.
    """
    fb = run["sets"].get("financebench")
    if not isinstance(fb, dict):
        return None
    cfg = run["config"]
    if fb.get("judge_kappa") is not None:
        if (
            fb.get("judge_kappa_source") == "eval/judges/kappa.json"
            and record
            and record.get("kappa") == fb["judge_kappa"]
            and record.get("judge_model") == cfg.get("judge")
            and record.get("rubric_version") == (cfg.get("judge_rubrics") or {}).get("correctness")
        ):
            # Stamped by the harness from kappa.json for this judge and rubric. Nothing here
            # checks that this run's answers are the ones the labels were made on (a replay of
            # the same cassettes is, a changed retrieval or prompt is not), and the labels cover
            # the labelled run's rows, not necessarily this run's split. Say so in the row.
            return {
                "kappa": record["kappa"],
                "n": record.get("n"),
                "source": (
                    "eval/judges/kappa.json; labels measured on run "
                    f"{record.get('results_run_id')}, not on this run's rows "
                    "(same judge and rubric; answer identity not checked by report.py)"
                ),
                "rubric": record.get("rubric_version"),
                "judge": record.get("judge_model"),
                "labels_sha256": record.get("labels_sha256"),
            }
        return {"kappa": fb["judge_kappa"], "n": None, "source": "results file"}
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


def _correct_by_id(run: dict[str, Any], set_name: str) -> dict[str, float]:
    """id -> 1.0/0.0 for the questions of ``set_name`` the run scored (unscored rows are absent)."""
    return {
        q["id"]: float(q["correct"])
        for q in run["per_question"]
        if q.get("set") == set_name and q.get("correct") is not None
    }


def render_comparison(runs: list[dict[str, Any]]) -> list[str]:
    """Engine vs baseline on the same questions: a paired bootstrap of the per-question
    difference in correctness (never two independent CIs compared by eye)."""
    engines = [r for r in runs if r["config"].get("system") == "engine"]
    baselines = [r for r in runs if r["config"].get("system") == "baseline"]
    rows: list[str] = []
    for e in engines:
        split = e["config"].get("split")
        cands = [b for b in baselines if b["config"].get("split") == split]
        for b in sorted(cands, key=lambda r: r["run_id"], reverse=True):
            for name in ("numeric", "financebench"):
                ce, cb = _correct_by_id(e, name), _correct_by_id(b, name)
                ids = sorted(set(ce) & set(cb))
                if not ids:
                    continue
                pr = paired_bootstrap([ce[i] for i in ids], [cb[i] for i in ids])
                lo, hi = pr.ci95
                verdict = (
                    "engine higher"
                    if lo > 0
                    else ("baseline higher" if hi < 0 else "no difference shown")
                )
                rows.append(
                    f"| `{e['run_id']}` | `{b['run_id']}` | {split} | {name} | {pr.n} "
                    f"| {_pct(sum(ce[i] for i in ids) / pr.n)} "
                    f"| {_pct(sum(cb[i] for i in ids) / pr.n)} "
                    f"| {100 * pr.mean_diff:+.1f} pp | {_ci(pr.ci95)} "
                    f"| {pr.p_two_sided:.4f} | {verdict} |"
                )
            break  # the newest matching baseline run only
    if not rows:
        return [
            "### Engine vs baseline (paired bootstrap)\n",
            f"{NM}: no engine run with a baseline run on the same split yet.",
        ]
    return [
        "### Engine vs baseline (paired bootstrap)\n",
        "| engine run | baseline run | split | set | paired n | engine | baseline | difference "
        "| 95% CI | p | reading |\n|---|---|---|---|---:|---:|---:|---:|---|---:|---|",
        *rows,
        "\nThe difference is engine minus baseline on the questions both runs scored; the CI is "
        "a paired bootstrap (10,000 resamples) and `reading` only says whether it excludes 0.",
    ]


def render_pareto(runs: list[dict[str, Any]]) -> list[str]:
    """One row per engine run: what each composer costs and gets (ADR-012's input)."""
    engines = sorted(
        (r for r in runs if r["config"].get("system") == "engine"),
        key=lambda r: (str(r["config"].get("split")), r["run_id"]),
    )
    if not engines:
        return ["### Composer Pareto\n", f"{NM}: no engine run yet."]
    out = [
        "### Composer Pareto (accuracy, cost, latency per composer)\n",
        "| run | split | composer | numeric accuracy | FinanceBench accuracy | narrative coverage "
        "| $ / question | p95 ms |\n|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in engines:
        sets, lat = r["sets"], r["latency_ms"]

        def acc(name: str, key: str = "accuracy", sets: dict[str, Any] = sets) -> str:
            v = sets.get(name)
            return _pct(v.get(key)) if isinstance(v, dict) else NM

        p95 = NM if lat.get("p95") is None else f"{lat['p95']:.0f}"
        out.append(
            f"| `{r['run_id']}` | {r['config'].get('split')} | {r['config'].get('composer')} "
            f"| {acc('numeric')} | {acc('financebench')} | {acc('narrative', 'coverage')} "
            f"| {float(r['cost']['per_question_usd']):.5f} | {p95} |"
        )
    return out


def render_ablation(docs: list[dict[str, Any]]) -> list[str]:
    """The newest retrieval ablation: recall@k per component, latency, and each row's paired
    difference from the row before it (recall@8)."""
    if not docs:
        return ["### Retrieval ablation\n", f"{NM}: no ablation run yet."]
    doc = sorted(docs, key=lambda d: d["run_id"])[-1]
    out = [
        f"### Retrieval ablation ({doc['config'].get('split', NM)} split)\n",
        f"Run `{doc['run_id']}`, corpus version {doc.get('corpus_version') or NM}, "
        f"{doc['config']['n_questions']} questions, plan: {doc['config']['plan_source']}; "
        f"reranker `{doc['config']['reranker']}`. Latency is {doc['config']['latency_note']}.\n",
    ]
    set_names = sorted({s for row in doc["rows"] for s in row["sets"]})
    for set_name in set_names:
        out.append(f"\n**{set_name}** (hit@k, 95% CI)\n")
        out.append(
            "| component | n | @1 | @3 | @5 | @8 | vs previous row (@8, paired) "
            "| p50 ms | p95 ms |\n|---|---:|---|---|---|---|---|---:|---:|"
        )
        prev: dict[str, float] | None = None
        for row in doc["rows"]:
            st = row["sets"].get(set_name)
            if st is None:
                continue
            cells = [
                f"{_pct(st[f'recall_at_{k}'])} {_ci(st[f'recall_at_{k}_ci95'])}"
                for k in (1, 3, 5, 8)
            ]
            cur = {q["id"]: q["hits"]["8"] for q in row["per_question"] if q["set"] == set_name}
            if prev is None:
                diff = "-"
            else:
                ids = sorted(set(cur) & set(prev))
                pr = paired_bootstrap([cur[i] for i in ids], [prev[i] for i in ids])
                diff = f"{100 * pr.mean_diff:+.1f} pp {_ci(pr.ci95)}"
            prev = cur
            lat = row["latency_ms"]
            out.append(
                f"| {row['name']} | {st['n']} | "
                + " | ".join(cells)
                + f" | {diff} | {lat['p50']:.0f} | {lat['p95']:.0f} |"
            )
    return out


def render(
    runs: list[dict[str, Any]],
    kappa_record: dict[str, Any] | None = None,
    ablation: list[dict[str, Any]] | None = None,
) -> str:
    if not runs and not ablation:
        return "_No results files yet: every metric is NOT MEASURED._"
    out: list[str] = []
    if not runs:
        return "\n".join(render_ablation(ablation or []))
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
            kappa_value = float(kd["kappa"])
            sha = str(kd.get("labels_sha256") or NM)[:12]
            out.append(
                f"| `{rid}` | {kappa_value:.3f} | {kd['n'] if kd['n'] is not None else NM} "
                f"| {'met' if kappa_value >= KAPPA_TARGET else 'below target'} "
                f"| {kd.get('rubric') or NM} "
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
    out.append("")
    out += render_comparison(runs)
    out.append("")
    out += render_pareto(runs)
    out.append("")
    out += render_ablation(ablation or [])
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
    ablation_dir = args.results / "ablation"
    ablation = (
        [json.loads(p.read_text()) for p in sorted(ablation_dir.glob("*.json"))]
        if ablation_dir.exists()
        else []
    )
    args.page.write_text(splice(args.page.read_text(), render(runs, kappa, ablation)))
    print(f"updated {args.page} from {len(runs)} run(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
