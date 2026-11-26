"""The one M6 ablation, dev only: the engine with and without the verifier (ADR-049).

    uv run python -m eval.verifier_ablation          # needs the cores; no DB, no LLM, $0

Both arms are the **same** composer outputs, taken from the accepted Gate 0 trace; the second is
the first put through ``verify_answer``. It is a post-processing replay, not a second engine run,
so the difference is the verifier's alone. Templated refusals (no composer) are not verified, as
in the engine. The unsupported rate of a kept claim is its Gate 0 verdict (no new judge call);
the verifier only drops claims, never rewrites them.

Not measured here: FinanceBench and narrative accuracy on the rebuilt answers (the judges would
have to run again) and added latency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.engine.answer import Answer
from app.engine.verify.models import VerificationReport
from app.engine.verify.pipeline import (
    DEFAULT_VERIFIER,
    Verifier,
    VerifierNotImplemented,
    require_implemented,
    verify_answer,
)
from eval import metrics
from eval.schemas import NumericRow, load_set
from eval.stats import bootstrap_ci, paired_bootstrap, wilson_ci
from eval.verifier_inputs import (
    TRACE_PATH,
    Reference,
    context_for,
    evidence_for,
    load_reference,
    load_trace,
)
from eval.verifier_trace import Trace, TraceRow

OUT_PATH = Path("eval/results/verifier/ablation_dev.json")
GATE0_PATH = Path("eval/results/verifier/gate0_dev_pre.json")
ANSWERABLE = ("numeric", "financebench", "narrative")
NOT_MEASURED = "NOT MEASURED"


@dataclass(frozen=True)
class Arm:
    """One system's answers, by question id, and (for the verified arm) the reports."""

    answers: dict[str, Answer]
    reports: dict[str, VerificationReport]


def verified_arm(trace: Trace, reference: Reference, verifier: Verifier = DEFAULT_VERIFIER) -> Arm:
    answers: dict[str, Answer] = {}
    reports: dict[str, VerificationReport] = {}
    for row in trace.rows:
        if row.answer is None:
            raise ValueError(f"{row.id}: the accepted trace has no answer")
        if not row.composed:
            answers[row.id] = row.answer
            continue
        answers[row.id], reports[row.id] = verify_answer(
            row.answer, evidence_for(trace, row), context_for(row, reference), verifier=verifier
        )
    return Arm(answers, reports)


def _rate(flags: Sequence[bool], *, wilson: bool = False) -> dict[str, Any]:
    if not flags:
        return {"n": 0, "rate": None, "ci95": None}
    k, n = sum(flags), len(flags)
    ci = wilson_ci(k, n) if wilson else bootstrap_ci([float(f) for f in flags])
    return {"n": n, "count": k, "rate": k / n, "ci95": list(ci)}


def _claim_rates(
    arm: Arm, rows: Sequence[TraceRow], labels: dict[tuple[str, str], str]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for group in ("all", "text", "numeric"):
        picked = [
            labels[(row.id, c.id)]
            for row in rows
            for c in arm.answers[row.id].claims
            if group == "all" or (c.kind == "text") == (group == "text")
        ]
        out[group] = {
            "unsupported": _rate([x == "unsupported" for x in picked], wilson=True),
            "partial": _rate([x == "partial" for x in picked], wilson=True),
        }
    return out


def arm_stats(
    arm: Arm,
    trace: Trace,
    labels: dict[tuple[str, str], str],
    numeric_gold: dict[str, NumericRow],
) -> dict[str, Any]:
    answers = arm.answers
    return {
        "claims": _claim_rates(arm, trace.rows, labels),
        "answerable_false_refusal": _rate(
            [metrics.refused(answers[r.id]) for r in trace.rows if r.set in ANSWERABLE]
        ),
        "unanswerable_refusal_rate": _rate(
            [metrics.refused(answers[r.id]) for r in trace.rows if r.set == "unanswerable"]
        ),
        "numeric_accuracy": _rate(
            [
                metrics.numeric_correct(answers[r.id], numeric_gold[r.id])
                for r in trace.rows
                if r.set == "numeric"
            ]
        ),
        "financebench_accuracy": NOT_MEASURED,
        "narrative_coverage": NOT_MEASURED,
        "added_latency_ms": NOT_MEASURED,
    }


def build_report(
    trace: Trace,
    verified: Arm,
    labels: dict[tuple[str, str], str],
    numeric_gold: dict[str, NumericRow],
) -> dict[str, Any]:
    engine = Arm({r.id: r.answer for r in trace.rows if r.answer is not None}, {})
    answerable = [r for r in trace.rows if r.set in ANSWERABLE]
    before = [float(metrics.refused(engine.answers[r.id])) for r in answerable]
    after = [float(metrics.refused(verified.answers[r.id])) for r in answerable]
    pair = paired_bootstrap(after, before)

    newly: list[dict[str, Any]] = []
    for row in trace.rows:
        new = verified.answers[row.id]
        if metrics.refused(new) and not metrics.refused(engine.answers[row.id]):
            assert new.refusal is not None
            newly.append(
                {
                    "id": row.id,
                    "set": row.set,
                    "reason": new.refusal.reason,
                    "missing": new.refusal.missing,
                    "dropped_by_check": verified.reports[row.id].dropped_by_check,
                }
            )

    dropped: list[dict[str, Any]] = []
    for row in trace.rows:
        report = verified.reports.get(row.id)
        if report is None or row.answer is None:
            continue
        for claim, result in zip(row.answer.claims, report.results, strict=True):
            if result.action == "drop":
                dropped.append(
                    {
                        "id": f"{row.id}/{claim.id}",
                        "kind": claim.kind,
                        "judge_label": labels[(row.id, claim.id)],
                        "failed_check": (result.checks.failed() or ["policy"])[0],
                        "reason": result.reason,
                        "claim_text": claim.text,
                    }
                )
    return {
        "arms": {
            "engine": arm_stats(engine, trace, labels, numeric_gold),
            "engine_plus_verifier": arm_stats(verified, trace, labels, numeric_gold),
        },
        "false_refusal_difference": {
            "verifier_minus_engine": pair.mean_diff,
            "paired_bootstrap_ci95": list(pair.ci95),
            "n": pair.n,
            "newly_refused": len(newly),
        },
        "dropped_claims_by_check": dict(Counter(d["failed_check"] for d in dropped)),
        "dropped_claims_by_judge_label": dict(Counter(d["judge_label"] for d in dropped)),
        "newly_refused": newly,
        "dropped_claims": dropped,
    }


def main(argv: list[str] | None = None) -> int:
    from eval.run import _git_sha

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trace", type=Path, default=TRACE_PATH)
    ap.add_argument("--gate0", type=Path, default=GATE0_PATH)
    ap.add_argument("--sets-dir", type=Path, default=Path("eval/sets"))
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args(argv)
    try:
        require_implemented()
    except VerifierNotImplemented as err:
        print(err)
        return 2
    trace = load_trace(args.trace)
    gate0 = json.loads(args.gate0.read_text())
    if hashlib.sha256(args.trace.read_bytes()).hexdigest() != gate0["trace_sha256"]:
        print("REFUSING: the trace is not the one the Gate 0 verdicts were given for")
        return 2
    labels = {(v["question_id"], v["claim_id"]): v["label"] for v in gate0["verdicts"]}
    numeric_gold = {r.id: r for r in load_set("numeric", args.sets_dir)}  # type: ignore[attr-defined]
    body = build_report(trace, verified_arm(trace, load_reference()), labels, numeric_gold)  # type: ignore[arg-type]
    recorded = gate0["dev_answerable_false_refusal_no_verifier"]["value"]
    measured = body["arms"]["engine"]["answerable_false_refusal"]["rate"]
    if abs(measured - recorded) > 1e-9:
        print(f"REFUSING: the engine arm's false refusal {measured} is not the run's {recorded}")
        return 2
    report = {
        "kind": "verifier_ablation_dev",
        "git_sha": _git_sha(),
        "source_run_id": trace.meta.source_run_id,
        "corpus_version": trace.meta.corpus_version,
        "trace_sha256": gate0["trace_sha256"],
        "gate0_file": str(args.gate0),
        "method": "the same composer outputs, with and without verify_answer (not a second run)",
        "labels": "Gate 0 support-judge verdicts (uncalibrated); no new judge call",
        "llm_calls": 0,
        **body,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    for name, stats in report["arms"].items():
        u = stats["claims"]["all"]["unsupported"]
        fr = stats["answerable_false_refusal"]
        print(f"{name:22} unsupported {u.get('count')}/{u['n']}  false refusal {fr['rate']:.3f}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
