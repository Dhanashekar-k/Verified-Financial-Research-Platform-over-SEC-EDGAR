"""Planner accuracy on the eval sets (M05 section 3.3): intent, and for numeric rows also the
metric, company and period the plan names.

    uv run python -m eval.intent_accuracy --split dev --mode replay          # $0, cassettes only
    uv run python -m eval.intent_accuracy --split dev --mode batch           # PAID: Haiku calls

Labels exist only where the set defines them: numeric rows are `numeric`, narrative rows are
`narrative`. FinanceBench and unanswerable rows have no intent label (that is what the hand-labeled
router_test.jsonl is for, M7), so only their predicted-intent distribution is reported. Numeric rows
also carry the gold metric, CIK, fiscal year and period, so plan quality is measured directly:
a wrong metric or company would sink the numeric score before the compiler ever runs.

Results go to `eval/results/intent/<run_id>.json` (a subdirectory: `report.py` reads only the
top level). Every rate carries n and a 95% bootstrap CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.engine.entities.resolve_company import CompanyIndex
from app.engine.plan.planner import attach_ciks, build_system_prompt, plan_question
from app.engine.plan.schema import QueryPlan
from app.llm.client import BatchGaveUp, BatchPending, LLMClient
from app.llm.types import LLMMode
from eval.run import _load_rows
from eval.schemas import NumericRow
from eval.stats import bootstrap_ci
from eval.systems import AccountingLLM

MAX_ROUNDS = 5
LABELS = {"numeric": "numeric", "narrative": "narrative"}


class Rate(BaseModel):
    n: int
    rate: float | None
    ci95: tuple[float, float] | None


def rate(flags: list[bool]) -> Rate:
    if not flags:
        return Rate(n=0, rate=None, ci95=None)
    lo, hi = bootstrap_ci([float(f) for f in flags])
    return Rate(n=len(flags), rate=sum(flags) / len(flags), ci95=(lo, hi))


EXPECTED_OPERATIONS: dict[str, set[str]] = {
    "single_value": {"value"},
    "ratio": {"value", "ratio"},  # a ratio metric is asked for as a value (05 4.5)
    "growth": {"growth"},
    "compare": {"compare"},
}


def numeric_plan_checks(row: NumericRow, plan: QueryPlan) -> dict[str, bool]:
    """Did the plan name the gold metric, every gold company, the gold period and the operation
    the question type asks for? Growth rows list two periods and the gold period is the later one;
    compare rows name two companies (``cik`` and ``compare_cik``)."""
    ciks = {c.cik for c in plan.companies if c.cik is not None}
    want_ciks = {row.cik} | ({row.compare_cik} if row.compare_cik is not None else set())
    periods = {(p.fiscal_year, p.fiscal_period) for p in plan.periods}
    return {
        "metric": plan.metrics == [row.metric],
        "company": want_ciks <= ciks,
        "period": (row.fiscal_year, row.fiscal_period) in periods,
        "operation": plan.operation in EXPECTED_OPERATIONS[row.qtype],
    }


def run_plans(
    items: list[tuple[str, BaseModel, str | None]],
    llm: AccountingLLM,
    client: LLMClient,
    index: CompanyIndex,
) -> dict[str, QueryPlan]:
    """Plan every question, in batch rounds when the client is in batch mode."""
    system = build_system_prompt()
    plans: dict[str, QueryPlan] = {}
    failed: dict[str, str] = {}
    todo = [(row.id, row.question) for _, row, _ in items]  # type: ignore[attr-defined]
    for _ in range(MAX_ROUNDS):
        pending: list[tuple[str, str]] = []
        for qid, question in todo:
            try:
                plan, _ = plan_question(question, llm, system=system)
                plans[qid] = attach_ciks(plan, index)
            except BatchPending:
                pending.append((qid, question))
            except BatchGaveUp as err:
                failed[qid] = str(err)
        if not pending:
            break
        client.run_pending_batch()
        todo = pending
    else:
        raise RuntimeError(f"{len(todo)} questions still pending after {MAX_ROUNDS} batch rounds")
    if failed:
        raise RuntimeError(f"{len(failed)} planner requests gave up: {sorted(failed)[:5]}")
    return plans


def score_plans(
    items: list[tuple[str, BaseModel, str | None]], plans: dict[str, QueryPlan]
) -> dict[str, Any]:
    intent: dict[str, list[bool]] = {}
    fields: dict[str, list[bool]] = {k: [] for k in ("metric", "company", "period", "operation")}
    clarified = Counter[str]()
    distribution: dict[str, Counter[str]] = {}
    per_question: list[dict[str, Any]] = []
    confusion: dict[str, Counter[str]] = {}
    for set_name, row, _ in items:
        qid = row.id  # type: ignore[attr-defined]
        plan = plans[qid]
        label = LABELS.get(set_name)
        record: dict[str, Any] = {"id": qid, "set": set_name, "predicted": plan.intent}
        distribution.setdefault(set_name, Counter())[plan.intent] += 1
        if plan.needs_clarification:
            clarified[set_name] += 1
        if label is not None:
            record["label"] = label
            record["correct"] = plan.intent == label
            intent.setdefault(set_name, []).append(plan.intent == label)
            confusion.setdefault(label, Counter())[plan.intent] += 1
        if isinstance(row, NumericRow):
            checks = numeric_plan_checks(row, plan)
            record["plan_checks"] = checks
            for k, v in checks.items():
                fields[k].append(v)
        per_question.append(record)
    return {
        "intent_accuracy": {k: rate(v).model_dump() for k, v in intent.items()},
        "numeric_plan_fields": {k: rate(v).model_dump() for k, v in fields.items() if v},
        "confusion": {k: dict(v) for k, v in confusion.items()},
        "predicted_intent_distribution": {k: dict(v) for k, v in distribution.items()},
        "needs_clarification": dict(clarified),
        "per_question": per_question,
    }


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Planner intent/plan accuracy on the eval sets.")
    ap.add_argument("--split", default="dev", choices=["dev", "test", "all"])
    ap.add_argument("--sets", default="numeric,narrative,financebench,unanswerable")
    ap.add_argument("--mode", default="replay", choices=[m.value for m in LLMMode])
    ap.add_argument("--out", type=Path, default=Path("eval/results/intent"))
    ap.add_argument("--sets-dir", type=Path, default=Path("eval/sets"))
    ap.add_argument("--splits", type=Path, default=Path("eval/sets/splits.json"))
    ap.add_argument("--cassettes", type=Path, default=Path("eval/cassettes"))
    ap.add_argument(
        "--companies",
        type=Path,
        default=Path("tests/fixtures/entities/companies.csv"),
        help="company snapshot for the resolver (cik,name,ticker,aliases)",
    )
    args = ap.parse_args(argv)
    if args.mode == "stub":
        raise SystemExit("--mode stub plans nothing; no results file is written")
    if args.split == "test":
        print("NOTE: test split. Iterate on dev; run test only at milestones (rules/eval.md).")
    items = _load_rows(
        [s for s in args.sets.split(",") if s], args.split, args.sets_dir, args.splits
    )
    client = LLMClient(mode=LLMMode(args.mode), cassette_dir=args.cassettes)
    llm = AccountingLLM(client)
    plans = run_plans(items, llm, client, CompanyIndex.from_csv(args.companies))
    scored = score_plans(items, plans)
    cost = sum((u.cost_usd for u in llm.ledger), Decimal(0))
    system = build_system_prompt()
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    run_id = f"{stamp}_intent_planner-{client.resolve('planner')}"
    results = {
        "run_id": run_id,
        "git_sha": _git_sha(),
        "config": {
            "mode": args.mode,
            "split": args.split,
            "sets": args.sets.split(","),
            "planner": client.resolve("planner"),
            "system_prompt_sha256": hashlib.sha256(system.encode()).hexdigest(),
            "n_questions": len(items),
        },
        "cost_usd": float(cost),
        **scored,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{run_id}.json"
    path.write_text(json.dumps(results, indent=2, default=str) + "\n")
    print(f"wrote {path} ({len(items)} questions, ${float(cost):.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
