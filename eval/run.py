"""Eval harness: score one system on the gold sets and write ``eval/results/<run_id>.json``.

    uv run python -m eval.run --system baseline|engine|stub --sets numeric,financebench,... \\
        --split dev|test|all --mode replay|live|record|batch|stub --composer sonnet|haiku

Systems are called in-process through ``answer(question, cfg) -> Answer`` (eval.systems), so the
baseline and the engine are scored by the same metric functions (eval.metrics, Shekar's) and
the same judges (eval.judges). Cost comes from accounting: every LLM call's ``Usage`` is
recorded by ``AccountingLLM``, system calls and judge calls separately. Batch mode runs in
rounds: questions whose requests are queued are rerun after each ``run_pending_batch()``.

A metric function that still raises ``NotImplementedError`` leaves the question unscored
(``correct = null``) and its aggregate ``null``; nothing is guessed. Every aggregate carries
n and a 95% bootstrap CI (10,000 resamples, seed 13).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.engine.answer import Answer, ChunkProvenance, ExprProvenance
from app.llm.client import BatchGaveUp, BatchPending, LLMClient, LLMResponseError
from app.llm.types import LLMMode, Usage
from eval import metrics
from eval.judges.narrative import InvalidJudgeOutput
from eval.schemas import (
    SET_FILES,
    ClaimsResult,
    CostSummary,
    FinanceBenchRow,
    LatencySummary,
    NarrativeRow,
    NumericRow,
    PerQuestion,
    ResultsFile,
    RunConfig,
    SetResult,
    UnanswerableRow,
    load_set,
)
from eval.splits import check_coverage, ids_for
from eval.stats import bootstrap_ci, percentile
from eval.systems import (
    SYSTEMS,
    AccountingLLM,
    EvalCfg,
    compiled_facts_sink,
    reset_compiled_facts,
    reset_retrieval,
    reset_route,
    retrieval_sink,
    route_sink_set,
)

COMPOSERS = {"sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
RECALL_KS = (5, 10, 20)
MAX_BATCH_ROUNDS = 6
ANSWERABLE = ("numeric", "financebench", "narrative")


@dataclass
class Outcome:
    """Everything recorded for one question (becomes a PerQuestion row)."""

    row: BaseModel
    set_name: str
    split: str | None
    answer: Answer | None = None
    error: str | None = None
    latency_ms: int = 0
    retrieved: list[str] | None = field(default_factory=list)  # None: system doesn't retrieve
    system_usage: list[Usage] = field(default_factory=list)
    judge_usage: list[Usage] = field(default_factory=list)
    scores: dict[str, Any] = field(default_factory=dict)
    route: str = "n/a"
    # fact ids the system's compiler returned for this answer; checked, never written out
    compiled_facts: set[int] = field(default_factory=set)


def _unimplemented(fn: Callable[..., Any], *args: Any) -> Any:
    """Call a metric; None when Shekar hasn't written it yet."""
    try:
        return fn(*args)
    except NotImplementedError:
        return None


# --- scoring --------------------------------------------------------------------------------


def score(
    outcome: Outcome,
    judge: AccountingLLM | None,
    gold_chunks: dict[str, list[str]],
) -> None:
    """Fill ``outcome.scores``. Judge calls may raise BatchPending (caller retries the round)."""
    ans, row, s = outcome.answer, outcome.row, outcome.scores
    if ans is None:
        if outcome.error:
            # A system failure counts against the system: incorrect on answerable sets, not a
            # refusal on unanswerable. Dropping it would make failing on hard questions pay.
            s["correct"] = None if isinstance(row, NarrativeRow) else False
            s["refused"] = False
            if isinstance(row, NarrativeRow):
                s["coverage"] = 0.0
        return
    refused = _unimplemented(metrics.refused, ans)
    s["refused"] = refused

    if isinstance(row, UnanswerableRow):
        s["correct"] = refused
        return
    if isinstance(row, NumericRow):
        s["correct"] = _unimplemented(metrics.numeric_correct, ans, row)
    elif isinstance(row, FinanceBenchRow):
        if judge is not None:
            from eval.judges.correctness import judge_correctness

            mark = len(judge.ledger)
            try:
                verdict, _ = judge_correctness(judge, row.question, row.gold_answer, ans)  # type: ignore[arg-type]
            finally:  # a billed judge failure still costs money
                outcome.judge_usage += judge.ledger[mark:]
            s["judge_label"], s["judge_reason"] = verdict.label, verdict.reason
            s["correct"] = _unimplemented(metrics.financebench_correct, verdict.label)
        else:
            s["correct"] = None
        _recall(outcome, gold_chunks.get(row.id, []))
    elif isinstance(row, NarrativeRow):
        if judge is not None:
            from eval.judges.narrative import judge_narrative

            mark = len(judge.ledger)
            try:
                nverdict, _ = judge_narrative(judge, row.question, row.gold_points, ans)  # type: ignore[arg-type]
            finally:
                outcome.judge_usage += judge.ledger[mark:]
            covered = [False] * len(row.gold_points)
            for p in nverdict.points:  # covered if any entry says so (as NarrativeVerdict does)
                if 0 <= p.index < len(covered):
                    covered[p.index] = covered[p.index] or p.covered
            s["coverage"] = _unimplemented(metrics.narrative_coverage, covered)
            s["contradiction"] = nverdict.contradiction
        s["correct"] = None  # narrative has no single correct flag; coverage is the metric
        _recall(outcome, row.gold_chunk_ids)


def _recall(outcome: Outcome, gold: list[str]) -> None:
    if not gold or outcome.retrieved is None:  # no gold, or a system that doesn't retrieve
        return
    for k in RECALL_KS:
        outcome.scores[f"recall_at_{k}"] = _unimplemented(
            metrics.recall_at_k, outcome.retrieved, gold, k
        )


# --- aggregation ----------------------------------------------------------------------------


def _with_ci(values: Sequence[Any]) -> tuple[float | None, list[float] | None, int]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None, None, 0
    lo, hi = bootstrap_ci(vals)
    return sum(vals) / len(vals), [lo, hi], len(vals)


def aggregate(outcomes: list[Outcome], kappa: float | None) -> dict[str, Any]:
    by_set: dict[str, list[Outcome]] = {}
    for o in outcomes:
        by_set.setdefault(o.set_name, []).append(o)
    sets: dict[str, Any] = {}
    for name, rows in by_set.items():
        res: dict[str, Any] = {"n": len(rows)}
        if name == "unanswerable":
            rate, ci, n_scored = _with_ci([o.scores.get("refused") for o in rows])
            res.update(refusal_rate=rate, refusal_rate_ci95=ci, n_scored=n_scored)
        elif name == "narrative":
            for key in ("coverage", "contradiction"):
                v, ci, n_scored = _with_ci([o.scores.get(key) for o in rows])
                res.update({key: v, f"{key}_ci95": ci, f"{key}_n": n_scored})
            res["judge"] = "uncalibrated"
        else:
            acc, ci, n_scored = _with_ci([o.scores.get("correct") for o in rows])
            res.update(accuracy=acc, ci95=ci, n_scored=n_scored)
            if name == "financebench":
                res["judge_kappa"] = kappa
                rr, rci, _ = _with_ci([o.scores.get("refused") for o in rows])
                res["judge_kappa_source"] = None if kappa is None else "eval/judges/kappa.json"
                res.update(refusal_rate=rr, refusal_rate_ci95=rci)
        for k in RECALL_KS:
            v, ci, n_scored = _with_ci([o.scores.get(f"recall_at_{k}") for o in rows])
            if n_scored:
                res.update(
                    {f"recall_at_{k}": v, f"recall_at_{k}_ci95": ci, f"recall_at_{k}_n": n_scored}
                )
        res["errors"] = sum(o.error is not None for o in rows)
        res["judge_errors"] = sum("judge_error" in o.scores for o in rows)
        res["scoring_errors"] = sum("scoring_error" in o.scores for o in rows)
        sets[name] = SetResult.model_validate(res)
    answerable = [o for o in outcomes if o.set_name in ANSWERABLE]
    fr, _, _ = _with_ci([o.scores.get("refused") for o in answerable])
    sets["answerable_false_refusal"] = fr
    # n is the number of claims asked about; the unsupported-claim rates need the support judge
    # with chunk text (M6) and stay NOT MEASURED.
    n_claims = sum(len(o.answer.claims) for o in outcomes if o.answer is not None)
    sets["claims"] = ClaimsResult(n=n_claims)
    return sets


MAX_SCORING_ERROR_SHARE = 0.10


def provenance_gaps(
    answer: Answer, *, strict: bool = False, compiled: Collection[int] | None = None
) -> list[str]:
    """Claims whose provenance is present in shape but empty in substance: an `expr` claim with no
    fact ids, a `chunk` claim with no chunk id or a blank quote. 05 section 4.3 gives every claim
    exactly one provenance; DoD M5 asserts that none is missing. ``strict`` (the engine) also
    flags an answer that has prose, no claim and no refusal: nothing in it carries provenance.

    ``compiled``: the fact ids the compiler returned for this answer. When given, every `expr`
    claim may cite only those (an invented id is a gap), and a numeric claim must cite fact ids.
    It checks the ids, not that the expression is one the compiler built."""
    gaps: list[str] = []
    for c in answer.claims:
        p = c.provenance
        if compiled is not None:
            if isinstance(p, ExprProvenance):
                unknown = sorted(set(p.fact_ids) - set(compiled))
                if unknown:
                    gaps.append(f"{c.id}: cites fact ids the compiler did not return: {unknown}")
            elif c.kind != "text":
                gaps.append(f"{c.id}: a numeric claim without fact-id provenance")
        if isinstance(p, ExprProvenance):
            if not p.fact_ids:
                gaps.append(f"{c.id}: expr provenance without fact ids")
        elif isinstance(p, ChunkProvenance) and (not p.chunk_id.strip() or not p.quote.strip()):
            gaps.append(f"{c.id}: chunk provenance without a chunk id or quote")
    if strict and not answer.claims and answer.refusal is None and answer.answer_markdown.strip():
        gaps.append("answer has prose but no claim and no refusal")
    return gaps


def provenance_breaches(outcomes: list[Outcome], *, strict: bool = False) -> dict[str, list[str]]:
    """``{question id: gaps}`` over every answered question (the harness asserts this is empty).
    ``strict`` (the engine) also holds each answer to the fact ids compiled for it; an answer for
    which none were reported may cite none."""
    out: dict[str, list[str]] = {}
    for o in outcomes:
        if o.answer is None:
            continue
        compiled = getattr(o, "compiled_facts", set()) if strict else None
        if gaps := provenance_gaps(o.answer, strict=strict, compiled=compiled):
            out[o.row.id] = gaps  # type: ignore[attr-defined]
    return out


def scoring_error_breaches(
    outcomes: list[Outcome], max_share: float = MAX_SCORING_ERROR_SHARE
) -> dict[str, tuple[int, int]]:
    """Sets where more than ``max_share`` of the questions hit a scoring error (a metric raised
    ValueError): ``{set: (errors, n)}``. A few malformed rows are tolerated and reported; a
    metric that rejects most rows (say a new judge label) makes every aggregate meaningless."""
    n: dict[str, int] = {}
    bad: dict[str, int] = {}
    for o in outcomes:
        n[o.set_name] = n.get(o.set_name, 0) + 1
        bad[o.set_name] = bad.get(o.set_name, 0) + ("scoring_error" in o.scores)
    return {k: (bad[k], n[k]) for k in n if bad[k] > max_share * n[k]}


# --- running --------------------------------------------------------------------------------


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _load_rows(
    set_names: list[str], split: str, sets_dir: Path, splits_path: Path
) -> list[tuple[str, BaseModel, str | None]]:
    splits = json.loads(splits_path.read_text()) if splits_path.exists() else None
    out: list[tuple[str, BaseModel, str | None]] = []
    for name in set_names:
        keep = ids_for(split, name, splits)
        rows = load_set(name, sets_dir)
        if splits and name in splits["sets"]:
            check_coverage(name, {r.id for r in rows}, splits)  # type: ignore[attr-defined]
        for row in rows:
            rid = row.id  # type: ignore[attr-defined]
            if keep is None or rid in keep:
                which = None
                if splits and name in splits["sets"]:
                    which = "dev" if rid in set(splits["sets"][name]["dev"]) else "test"
                out.append((name, row, which))
    return out


def sample_items(
    items: list[tuple[str, BaseModel, str | None]], n: int, seed: int
) -> list[tuple[str, BaseModel, str | None]]:
    """A seeded random sample of ``n`` rows per set (all of them when a set has fewer), in the
    original order. The same seed and rows give the same questions on every run."""
    out: list[tuple[str, BaseModel, str | None]] = []
    for name in dict.fromkeys(i[0] for i in items):
        positions = [k for k, i in enumerate(items) if i[0] == name]
        chosen = set(random.Random(f"{seed}|{name}").sample(positions, min(n, len(positions))))
        out += [items[k] for k in sorted(chosen)]
    return out


def run_questions(
    system: Any,
    cfg: EvalCfg,
    judge: AccountingLLM | None,
    items: list[tuple[str, BaseModel, str | None]],
    gold_chunks: dict[str, list[str]],
    client: LLMClient,
    *,
    batch_rounds: int = MAX_BATCH_ROUNDS,
    log: Callable[[str], None] = print,
) -> tuple[list[Outcome], dict[str, Any]]:
    retrieves = getattr(system, "retrieves", True)
    outcomes = {
        row.id: Outcome(  # type: ignore[attr-defined]
            row=row, set_name=name, split=sp, retrieved=[] if retrieves else None
        )
        for name, row, sp in items
    }
    todo = list(outcomes)
    batches: list[dict[str, Any]] = []
    pending: list[str] = []
    for round_no in range(1, batch_rounds + 1):
        pending = []
        for qid in todo:
            o = outcomes[qid]
            if o.answer is None and o.error is None:
                sink, token = retrieval_sink()
                route_sink, route_token = route_sink_set()
                compiled, compiled_token = compiled_facts_sink()
                mark = len(cfg.llm.ledger)
                start = time.monotonic()
                try:
                    o.answer = system.answer(o.row.question, cfg)  # type: ignore[attr-defined]
                    o.latency_ms = int((time.monotonic() - start) * 1000)
                    o.retrieved = list(sink) if getattr(system, "retrieves", True) else None
                    o.compiled_facts = set(compiled)
                except BatchPending:
                    pending.append(qid)
                except Exception as err:  # noqa: BLE001
                    # Any system failure (LLM, DB, lookup) is recorded on the question and
                    # counted against the system, not allowed to abort the whole run.
                    o.error = f"{type(err).__name__}: {err}"
                finally:
                    o.system_usage += cfg.llm.ledger[mark:]
                    reset_retrieval(token)
                    reset_compiled_facts(compiled_token)
                    o.route = route_sink.get("route", "n/a")
                    reset_route(route_token)
                if qid in pending:
                    continue  # errored answers still go to score(), which marks them
            try:
                score(o, judge, gold_chunks)
            except BatchPending:
                o.scores.clear()
                pending.append(qid)
            except (LLMResponseError, BatchGaveUp, InvalidJudgeOutput) as err:
                # billed judge failures are already on the ledger (sliced in score's finally)
                o.scores["judge_error"] = f"{type(err).__name__}: {err}"
            except (ValueError, ArithmeticError) as err:
                # A metric rejected this row (e.g. narrative_coverage on a row with no gold
                # points). Unscored and recorded; one bad row must not abort a paid run.
                # ArithmeticError: decimal.Overflow on a huge model-emitted exponent, say.
                # After the judge errors above: InvalidJudgeOutput is a ValueError.
                o.scores["scoring_error"] = f"{type(err).__name__}: {err}"
        if not pending:
            break
        if client.mode is not LLMMode.batch:
            raise RuntimeError("BatchPending outside batch mode")
        if round_no == batch_rounds:
            break  # submitting now would pay for results no round is left to use
        log(f"round {round_no}: {len(pending)} questions waiting on {client.pending} requests")
        report = client.run_pending_batch()
        batches.append(
            {
                "batch_id": report.batch_id,
                "submitted": report.submitted,
                "succeeded": report.succeeded,
                "failed": len(report.failed),
            }
        )
        todo = pending
    for qid in pending:
        o = outcomes[qid]
        if o.answer is None:
            o.error = o.error or "batch rounds exhausted"
        else:
            o.scores["judge_error"] = "batch rounds exhausted"
    return list(outcomes.values()), {"batches": batches}


def build_results(
    outcomes: list[Outcome],
    config: RunConfig,
    corpus_version: int | None,
    kappa: float | None,
    failed_usage_usd: Decimal,
) -> ResultsFile:
    per_q: list[PerQuestion] = []
    for o in outcomes:
        cost = sum((u.cost_usd for u in o.system_usage + o.judge_usage), Decimal(0))
        ans = o.answer
        extra: dict[str, Any] = {
            "split": o.split,
            "question": o.row.question,  # type: ignore[attr-defined]
            "system_cost_usd": str(sum((u.cost_usd for u in o.system_usage), Decimal(0))),
            "judge_cost_usd": str(sum((u.cost_usd for u in o.judge_usage), Decimal(0))),
            "input_tokens": sum(u.input_tokens for u in o.system_usage),
            "output_tokens": sum(u.output_tokens for u in o.system_usage),
            "retrieved_top20": None if o.retrieved is None else o.retrieved[:20],
            "error": o.error,
            **{k: v for k, v in o.scores.items() if k != "correct"},
        }
        if isinstance(o.row, FinanceBenchRow):
            extra["gold_answer"] = o.row.gold_answer
        if ans is not None:
            extra["answer_markdown"] = ans.answer_markdown
            extra["refusal"] = ans.refusal.model_dump() if ans.refusal else None
        per_q.append(
            PerQuestion(
                id=o.row.id,  # type: ignore[attr-defined]
                set=o.set_name,
                correct=o.scores.get("correct"),
                route=o.route,
                cost_usd=cost,
                latency_ms=o.latency_ms,
                **extra,
            )
        )
    total = sum((p.cost_usd for p in per_q), Decimal(0)) + failed_usage_usd
    lat = [o.latency_ms for o in outcomes if o.answer is not None]
    measured = config.mode in ("live", "record") and lat
    utc = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    short = {v: k for k, v in COMPOSERS.items()}.get(config.composer, config.composer)
    return ResultsFile(
        run_id=f"{utc}_{config.system}-{config.system_version}_{short}",
        git_sha=_git_sha(),
        corpus_version=corpus_version,
        config=config,
        sets=aggregate(outcomes, kappa),
        latency_ms=LatencySummary(
            p50=percentile(lat, 50) if measured else None,
            p95=percentile(lat, 95) if measured else None,
        ),
        cost=CostSummary(
            total_usd=total, per_question_usd=total / len(per_q) if per_q else Decimal(0)
        ),
        per_question=per_q,
    )


def _read_live_corpus_version(dsn: str) -> int | None:
    """``corpus_state.live_version`` from the serving DB (read-only session). Retrieval goes
    through the ``chunks`` view, which follows this value, so it is the version a run used."""
    import psycopg

    with psycopg.connect(
        dsn, autocommit=True, options="-c default_transaction_read_only=on"
    ) as conn:
        # the table does not exist before the first swap
        if conn.execute("SELECT to_regclass('corpus_state')").fetchone() == (None,):
            return None
        row = conn.execute("SELECT live_version FROM corpus_state").fetchone()
    return None if row is None else int(row[0])


def resolve_corpus_version(system: object, dsn: str | None, explicit: int | None) -> int | None:
    """The corpus version to record in the results file.

    A system that reads the corpus (``reads_corpus``) is stamped with the live version from the
    DB. An explicit ``--corpus-version`` is accepted only when it equals the live one: retrieval
    cannot be pointed at another version, so a different value would mislabel the run. A system
    that reads no corpus (long-context, stub) records ``explicit`` as given, usually None.
    """
    if not getattr(system, "reads_corpus", False):
        return explicit
    if not dsn:
        raise RuntimeError("cannot record corpus_version: no serving DSN (EDGARQA_SERVING_DSN)")
    live = _read_live_corpus_version(dsn)
    if live is None:
        raise RuntimeError("corpus_state is empty: no live corpus version to record")
    if explicit is not None and explicit != live:
        raise SystemExit(
            f"--corpus-version {explicit} but the serving DB's live version is {live}; "
            "retrieval reads the live version, so the run would be mislabeled"
        )
    return live


def corpus_version_drifted(system: object, dsn: str | None, started: int | None) -> int | None:
    """The live version now, when it differs from the one the run started under (a swap or
    rollback mid-run: later questions retrieved from another corpus); else None."""
    if not getattr(system, "reads_corpus", False) or not dsn or started is None:
        return None
    now = _read_live_corpus_version(dsn)
    return None if now == started else now


def _kappa(path: Path, judge_model: str | None, rubric: str) -> float | None:
    """Judge-vs-human kappa written by ``eval.judges.calibrate kappa``, used only when it was
    measured for this run's judge model and correctness rubric version (else None)."""
    if judge_model is None or not path.exists():
        return None
    data = json.loads(path.read_text())
    if data.get("judge_model") != judge_model or data.get("rubric_version") != rubric:
        return None
    value = data.get("kappa")
    return float(value) if value is not None else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score a system on the eval sets.")
    ap.add_argument("--system", required=True, choices=sorted(SYSTEMS))
    ap.add_argument("--sets", default=",".join(SET_FILES))
    ap.add_argument("--split", default="dev", choices=["dev", "test", "all"])
    ap.add_argument("--mode", default="replay", choices=[m.value for m in LLMMode])
    ap.add_argument("--composer", default="sonnet", choices=sorted(COMPOSERS))
    ap.add_argument("--out", type=Path, default=Path("eval/results"))
    ap.add_argument("--sets-dir", type=Path, default=Path("eval/sets"))
    ap.add_argument("--splits", type=Path, default=Path("eval/sets/splits.json"))
    ap.add_argument(
        "--gold-chunks",
        type=Path,
        default=Path("eval/sets/drafts/financebench_evidence_chunks.json"),
        help="FinanceBench row id -> gold chunk ids (evidence pages) for recall@k",
    )
    ap.add_argument("--ef-search", type=int, default=800, help="HNSW ef_search, pinned (ADR-040)")
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--corpus-version", type=int, default=None)
    ap.add_argument(
        "--dsn",
        default=os.environ.get("EDGARQA_SERVING_DSN"),
        help="serving Postgres DSN (default: $EDGARQA_SERVING_DSN; keeps it out of make's echo)",
    )
    ap.add_argument(
        "--allow-stub-results",
        action="store_true",
        help="write a results file for --mode stub (tests only; report.py never shows them)",
    )
    ap.add_argument(
        "--no-judge",
        action="store_true",
        help="skip LLM judges (FinanceBench and narrative are then unscored)",
    )
    ap.add_argument(
        "--sample",
        type=int,
        default=None,
        help="a seeded random sample of N questions per set (e.g. 30 for long-context)",
    )
    ap.add_argument("--sample-seed", type=int, default=13)
    ap.add_argument(
        "--request-timeout",
        type=float,
        default=None,
        help="seconds per API call (default 120); raise it for ~150K-token long-context prompts",
    )
    ap.add_argument("--kappa-file", type=Path, default=Path("eval/judges/kappa.json"))
    ap.add_argument("--cassettes", type=Path, default=Path("eval/cassettes"))
    args = ap.parse_args(argv)
    if args.sample is not None and args.sample < 1:
        ap.error("--sample must be at least 1")
    if args.request_timeout is not None and args.request_timeout <= 0:
        ap.error("--request-timeout must be positive")

    if args.mode == "stub" and not args.allow_stub_results:
        raise SystemExit("--mode stub produces fake answers; no results file is written")
    if args.split == "test":
        print("NOTE: test split. Iterate on dev; run test only at milestones (rules/eval.md).")
    set_names = [s.strip() for s in args.sets.split(",") if s.strip()]
    composer = COMPOSERS[args.composer]
    timeout = {} if args.request_timeout is None else {"request_timeout_s": args.request_timeout}
    client = LLMClient(
        mode=LLMMode(args.mode),
        cassette_dir=args.cassettes,
        registry={"composer": composer},
        **timeout,
    )
    system_llm, judge_llm = AccountingLLM(client), AccountingLLM(client)
    cfg = EvalCfg(
        llm=system_llm,
        composer="composer",
        ef_search=args.ef_search,
        top_k=args.top_k,
        corpus_version=args.corpus_version,
        dsn=args.dsn,
    )
    system = SYSTEMS[args.system]()
    gold_chunks: dict[str, list[str]] = {}
    if args.gold_chunks.exists():
        gold_chunks = json.loads(args.gold_chunks.read_text()).get("gold_chunk_ids", {})
    items = _load_rows(set_names, args.split, args.sets_dir, args.splits)
    corpus_version = resolve_corpus_version(system, args.dsn, args.corpus_version)
    cfg.corpus_version = corpus_version
    if args.sample is not None:
        items = sample_items(items, args.sample, args.sample_seed)
    try:
        outcomes, batch_info = run_questions(
            system, cfg, None if args.no_judge else judge_llm, items, gold_chunks, client
        )
    finally:
        close = getattr(system, "close", None)
        if callable(close):
            close()
    config = RunConfig(
        system=args.system,
        system_version=str(getattr(system, "version", "0")),
        composer=composer,
        mode=args.mode,
        split=args.split,
        sets=set_names,
        embedder=getattr(system, "embedder_name", None),
        ef_search=args.ef_search,
        top_k=args.top_k,
        judge=None if args.no_judge else client.resolve("judge"),
        judge_rubrics=_rubric_versions(),
        batches=batch_info["batches"],
        failed_usage_usd=str(client.failed_usage_usd),
        sample=args.sample,
        sample_seed=None if args.sample is None else args.sample_seed,
        request_timeout_s=args.request_timeout,
        latency_note=None
        if args.mode in ("live", "record")
        else f"{args.mode} mode: latency is not a system measurement",
    )
    results = build_results(
        outcomes,
        config,
        corpus_version,
        _kappa(
            args.kappa_file,
            None if args.no_judge else client.resolve("judge"),
            _rubric_versions()["correctness"],
        ),
        client.failed_usage_usd,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{results.run_id}.json"
    path.write_text(results.model_dump_json(indent=2) + "\n")
    print(f"wrote {path} ({len(outcomes)} questions, ${results.cost.total_usd:.4f})")
    drifted = corpus_version_drifted(system, args.dsn, corpus_version)
    if drifted is not None:  # the file is written first: cost and answers are not lost
        print(
            f"FAIL: the live corpus version changed from {corpus_version} to {drifted} during "
            "the run; later questions may have retrieved from another corpus. Rerun."
        )
        return 2
    # The gate is the engine's contract (DoD M5). The baselines' claims carry a model-written
    # quote and are not held to it: one blank quote must not abort a paid baseline run.
    gaps = provenance_breaches(outcomes, strict=True) if args.system == "engine" else {}
    if gaps:  # the file is written first; a claim without provenance breaks the contract
        for qid, why in list(gaps.items())[:10]:
            print(f"FAIL: {qid} breaks the provenance contract: {why}")
        return 2
    breaches = scoring_error_breaches(outcomes)
    if breaches:  # the file is written first: the run's cost and answers are not lost
        for name, (bad, n) in breaches.items():
            print(f"FAIL: {bad}/{n} {name} questions hit a scoring error (> 10%); see per_question")
        return 2
    return 0


def _rubric_versions() -> dict[str, str]:
    from eval.judges import correctness, narrative, support

    return {
        "correctness": correctness.RUBRIC_VERSION,
        "narrative": narrative.RUBRIC_VERSION,
        "support": support.RUBRIC_VERSION,
    }


if __name__ == "__main__":
    raise SystemExit(main())
