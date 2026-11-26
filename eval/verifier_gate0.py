"""M6 Gate 0: how many claims are unsupported before any verifier? (ADR-049)

    uv run python -m eval.verifier_gate0 --max-usd 2

Every claim of an accepted trace (``eval.verifier_trace``) is judged once by the support judge
in batch mode. **Paid.** The headline is the text-claim unsupported rate with its Wilson 95%
interval; numeric claims are reported apart. The Phase B branch follows from the rule below,
fixed before any verdict was seen. If any claim is left unjudged, no rate is published.

Spending cap: requests go out in chunks sized so that what is already spent (including billed
failures) plus the chunk's **worst case** stays within ``--max-usd``. The worst case takes the
full ``max_tokens`` as output and over-counts input (2 characters a token, billed as a cache
write), so the cap holds whatever the model replies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.engine.answer import ChunkProvenance
from app.llm.client import BatchGaveUp, BatchPending, LLMClient
from app.llm.prices import cost_usd
from app.llm.types import LLMMode, Usage
from eval import metrics
from eval.judges import support
from eval.judges.evidence import EVIDENCE_VERSION, chunk_evidence, expr_evidence
from eval.stats import bootstrap_ci, wilson_ci
from eval.verifier_trace import DEFAULT_OUT as DEFAULT_TRACE
from eval.verifier_trace import WRITER, Trace, compare_with_source

OUT_DIR = Path("eval/results/verifier")
CHUNK_SIZE = 20
SPEND_STATE = "gate0_spend_state.json"
INPUT_OVERHEAD_TOKENS = 500  # message framing and the structured-output instructions
THRESHOLD_PERCENT = 5  # the pre-registered rule: at or below, ship without entailment
BRANCH_DETERMINISTIC = "deterministic"
BRANCH_ENTAILMENT = "entailment"
ANSWERABLE = ("numeric", "financebench", "narrative")
NUMERIC_LABEL = (
    "LLM-judged arithmetic; the verifier re-checks numeric claims deterministically, so this "
    "does not decide headroom"
)


class UnresolvedBatch(RuntimeError):
    """A saved batch that cannot be resumed without risking a second payment."""


@dataclass(frozen=True)
class Item:
    """One claim to judge. ``evidence`` is None when the cited passage does not exist."""

    question_id: str
    set_name: str
    claim_id: str
    kind: str
    claim_text: str
    evidence: str | None
    refused: bool
    cited: str  # chunk id or expr
    shown: bool = True  # a cited chunk the composer was given


class Verdict(BaseModel):
    question_id: str
    set: str
    claim_id: str
    kind: str
    label: str
    reason: str
    source: str  # "judge" | "missing_chunk"
    refused: bool
    shown: bool
    cited: str
    claim_text: str


def build_items(trace: Trace) -> list[Item]:
    out: list[Item] = []
    for row in trace.rows:
        if row.answer is None:
            continue
        for claim in row.answer.claims:
            p = claim.provenance
            shown = True
            if isinstance(p, ChunkProvenance):
                chunk = trace.chunks.get(p.chunk_id)
                evidence = None if chunk is None else chunk_evidence(chunk, trace.company_names)
                shown = p.chunk_id in row.shown_chunk_ids
                cited = p.chunk_id
            else:
                evidence = expr_evidence(p.expr, p.fact_ids, trace.facts, trace.company_names)
                cited = p.expr
            out.append(
                Item(
                    question_id=row.id,
                    set_name=row.set,
                    claim_id=claim.id,
                    kind=claim.kind,
                    claim_text=claim.text,
                    evidence=evidence,
                    refused=row.answer.refusal is not None,
                    cited=cited,
                    shown=shown,
                )
            )
    return out


# --- judging under a cap ---------------------------------------------------------------------


def _messages(claim_text: str, evidence: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": support.build_user_message(claim_text, evidence)}]


def _key(client: LLMClient, claim_text: str, evidence: str) -> str:
    return client.request_key(
        "judge",
        support.SYSTEM,
        _messages(claim_text, evidence),
        support.SupportVerdict,
        max_tokens=support.MAX_TOKENS,
    )


def worst_case_usd(model: str, claim_text: str, evidence: str) -> Decimal:
    chars = len(support.SYSTEM) + len(support.build_user_message(claim_text, evidence))
    chars += len(json.dumps(support.SupportVerdict.model_json_schema()))
    tokens = chars // 2 + INPUT_OVERHEAD_TOKENS
    return cost_usd(model, 0, tokens, 0, support.MAX_TOKENS, batch=True)


def _billed(usage: Usage) -> Decimal:
    """What a verdict cost when it was bought: from its token counts, so a cassette served on a
    rerun still counts against the cap."""
    return cost_usd(
        usage.model,
        usage.input_tokens,
        usage.cache_write_tokens,
        usage.cache_read_tokens,
        usage.output_tokens,
        batch=True,
    )


@dataclass
class JudgeRun:
    verdicts: dict[str, support.SupportVerdict] = field(default_factory=dict)  # by request key
    usage: dict[str, Usage] = field(default_factory=dict)
    gave_up: dict[str, str] = field(default_factory=dict)
    failed_usd: Decimal = Decimal(0)
    batches: list[dict[str, Any]] = field(default_factory=list)
    capped: bool = False

    @property
    def spent_usd(self) -> Decimal:
        return sum((_billed(u) for u in self.usage.values()), self.failed_usd)


def judge_under_cap(
    client: LLMClient,
    items: Sequence[Item],
    max_usd: Decimal,
    *,
    prior_failed_usd: Decimal = Decimal(0),
    on_failed_spend: Callable[[Decimal], None] = lambda _: None,
    chunk_size: int = CHUNK_SIZE,
    poll_interval_s: float = 30.0,
    log: Callable[[str], None] = print,
) -> JudgeRun:
    """Judge every item with evidence, identical requests once, never risking more than
    ``max_usd``. Stops (``capped``) when the next request's worst case no longer fits."""
    if client.mode not in (LLMMode.batch, LLMMode.replay):
        raise ValueError("the Gate 0 judge runs in batch or replay mode")
    model = client.resolve("judge")
    todo: dict[str, tuple[str, str]] = {}
    for item in items:
        if item.evidence is not None:
            todo[_key(client, item.claim_text, item.evidence)] = (item.claim_text, item.evidence)
    run = JudgeRun(failed_usd=prior_failed_usd)

    def serve(key: str) -> None:
        claim_text, evidence = todo[key]
        try:
            verdict, usage = support.judge_support(client, claim_text, evidence)
        except BatchPending:
            return
        except BatchGaveUp as err:
            run.gave_up[key] = str(err)
            return
        run.verdicts[key], run.usage[key] = verdict, usage

    for key in todo:  # what is already bought, first: it is not part of any chunk's risk
        if (client.cassette_dir / f"{key}.json").exists() or client.mode is LLMMode.replay:
            serve(key)

    def collect(note: str) -> None:
        log(note)
        failed_before = client.failed_usage_usd
        try:
            report = client.run_pending_batch(poll_interval_s=poll_interval_s)
        finally:  # a billed failure counts even if collecting dies part way
            run.failed_usd += client.failed_usage_usd - failed_before
            on_failed_spend(run.failed_usd)
        run.batches.append(
            {
                "batch_id": report.batch_id,
                "submitted": report.submitted,
                "succeeded": report.succeeded,
                "failed": len(report.failed),
            }
        )

    # A batch left by an interrupted run is already bought. Collect exactly it before sizing
    # anything: a chunk of another shape would not resume it, and would pay for it again.
    if client.mode is LLMMode.batch:
        for state in sorted(client.cassette_dir.glob(".batch_*.json")):
            saved = set(json.loads(state.read_text())["keys"])
            if not saved & todo.keys():
                continue
            bought = {k for k in saved if (client.cassette_dir / f"{k}.json").exists()}
            if bought or saved - todo.keys():
                raise UnresolvedBatch(
                    f"{state} is partly collected or holds other requests: resubmitting could "
                    "pay twice. Check that batch's results before running again."
                )
            for key in sorted(saved):
                serve(key)  # queues it
            collect(f"collecting the saved batch in {state.name}")
            for key in sorted(saved):
                if (client.cassette_dir / f"{key}.json").exists():
                    serve(key)

    while True:
        remaining = [k for k in todo if k not in run.verdicts and k not in run.gave_up]
        if not remaining:
            return run
        chunk: list[str] = []
        risk = run.spent_usd
        for key in remaining[:chunk_size]:
            worst = worst_case_usd(model, *todo[key])
            if risk + worst > max_usd:
                break
            risk += worst
            chunk.append(key)
        if not chunk:
            run.capped = True
            return run
        for key in chunk:
            serve(key)  # queues it
        if not client.pending:
            continue
        collect(
            f"submitting {client.pending} requests; spent ${run.spent_usd:.4f}, "
            f"worst case after this chunk ${risk:.4f} of ${max_usd}"
        )
        for key in chunk:
            if (client.cassette_dir / f"{key}.json").exists():
                serve(key)


def verdicts_for(
    client: LLMClient, items: Sequence[Item], run: JudgeRun
) -> tuple[list[Verdict], list[Item]]:
    """One verdict per claim, and the claims left unjudged. A claim citing a passage that does
    not exist is unsupported without asking the judge."""
    done: list[Verdict] = []
    unjudged: list[Item] = []
    for item in items:
        if item.evidence is None:
            label, reason, source = (
                "unsupported",
                "the cited passage does not exist",
                "missing_chunk",
            )
        else:
            found = run.verdicts.get(_key(client, item.claim_text, item.evidence))
            if found is None:
                unjudged.append(item)
                continue
            label, reason, source = found.label, found.reason, "judge"
        done.append(
            Verdict(
                question_id=item.question_id,
                set=item.set_name,
                claim_id=item.claim_id,
                kind=item.kind,
                label=label,
                reason=reason,
                source=source,
                refused=item.refused,
                shown=item.shown,
                cited=item.cited,
                claim_text=item.claim_text,
            )
        )
    return done, unjudged


# --- report ----------------------------------------------------------------------------------


def group_stats(verdicts: Sequence[Verdict]) -> dict[str, Any]:
    labels = [v.label for v in verdicts]
    n = len(labels)
    counts = {name: labels.count(name) for name in ("supported", "partial", "unsupported")}
    if not n:
        return {"n": 0, **counts, "unsupported_rate": None, "partial_rate": None}
    return {
        "n": n,
        **counts,
        "unsupported_rate": metrics.unsupported_claim_rate(labels),
        "unsupported_ci95_wilson": list(wilson_ci(counts["unsupported"], n)),
        "partial_rate": counts["partial"] / n,
        "partial_ci95_wilson": list(wilson_ci(counts["partial"], n)),
    }


def decide_branch(text: dict[str, Any]) -> str:
    """The pre-registered rule (ADR-049), in integers so 5.0% exactly is "at or below"."""
    if not text["n"]:
        raise ValueError("no text claims: the pre-registered rule cannot be applied")
    low = text["unsupported"] * 100 <= THRESHOLD_PERCENT * text["n"]
    return BRANCH_DETERMINISTIC if low else BRANCH_ENTAILMENT


def build_report(verdicts: Sequence[Verdict]) -> dict[str, Any]:
    def where(pred: Callable[[Verdict], bool]) -> dict[str, Any]:
        return group_stats([v for v in verdicts if pred(v)])

    text = where(lambda v: v.kind == "text")
    return {
        "headline_text_claims": text,
        "preregistered_rule": {
            "metric": "text-claim unsupported point estimate (partial in the denominator only)",
            "threshold": THRESHOLD_PERCENT / 100,
            "at_or_below": "no entail.py: a fully deterministic verifier",
            "above": "keep one strict entailment pass",
            "observed": text["unsupported_rate"],
            "observed_unsupported": text["unsupported"],
            "max_unsupported_for_deterministic": text["n"] * THRESHOLD_PERCENT // 100,
            "branch": decide_branch(text),
        },
        "numeric_claims": {"note": NUMERIC_LABEL, **where(lambda v: v.kind != "text")},
        "by_kind": {
            k: where(lambda v, k=k: v.kind == k) for k in ("numeric", "numeric_derived", "text")
        },
        "all_claims": group_stats(verdicts),
        "by_set": {
            s: {
                "text": where(lambda v, s=s: v.set == s and v.kind == "text"),
                "numeric": where(lambda v, s=s: v.set == s and v.kind != "text"),
            }
            for s in sorted({v.set for v in verdicts})
        },
        "by_answer": {
            name: {
                "text": where(lambda v, r=r: v.refused == r and v.kind == "text"),
                "numeric": where(lambda v, r=r: v.refused == r and v.kind != "text"),
            }
            for name, r in (("answered", False), ("refused", True))
        },
        "missing_chunk": [
            f"{v.question_id}/{v.claim_id}" for v in verdicts if v.source == "missing_chunk"
        ],
        "cited_but_not_shown": [f"{v.question_id}/{v.claim_id}" for v in verdicts if not v.shown],
    }


def false_refusal(source: dict[str, Any], source_file: str) -> dict[str, Any]:
    """The recorded run's answerable false refusal (copied, not recomputed), with the bootstrap
    interval over the same recorded rows."""
    value = source["sets"]["answerable_false_refusal"]
    flags = [
        float(q["refused"])
        for q in source["per_question"]
        if q["set"] in ANSWERABLE and q.get("refused") is not None
    ]
    if value is None or not flags or abs(sum(flags) / len(flags) - value) > 1e-9:
        raise ValueError("answerable_false_refusal does not match the source's per_question rows")
    return {
        "value": value,
        "n": len(flags),
        "ci95_bootstrap": list(bootstrap_ci(flags)),
        "source_file": source_file,
        "key": "sets.answerable_false_refusal (interval: per_question[].refused)",
    }


def review_markdown(items: Sequence[Item], verdicts: Sequence[Verdict]) -> str:
    """Every non-supported verdict with the claim, the judge's reason and the evidence it saw."""
    evidence = {(i.question_id, i.claim_id): i.evidence for i in items}
    bad = [v for v in verdicts if v.label != "supported"]
    bad.sort(key=lambda v: (v.kind != "text", v.label != "unsupported", v.question_id, v.claim_id))
    lines = [
        "# Gate 0: claims the support judge did not mark supported",
        "",
        f"{len(bad)} of {len(verdicts)} claims. Text claims first. The judge is uncalibrated: "
        "read these before trusting the rates.",
    ]
    for v in bad:
        shown = evidence[(v.question_id, v.claim_id)] or "(the cited passage does not exist)"
        lines += [
            "",
            f"## {v.question_id} {v.claim_id}: {v.label} ({v.kind})",
            f"**Claim:** {v.claim_text}",
            "",
            f"**Judge:** {v.reason}",
            "",
            "<details><summary>Evidence shown to the judge</summary>",
            "",
            "````text",
            shown,
            "````",
            "",
            "</details>",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    from eval.run import _git_sha

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trace", type=Path, default=DEFAULT_TRACE)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--cassettes", type=Path, default=Path("eval/cassettes"))
    ap.add_argument("--mode", choices=["batch", "replay"], default="batch")
    ap.add_argument("--max-usd", type=Decimal, required=True)
    ap.add_argument(
        "--prior-failed-usd",
        type=Decimal,
        default=Decimal(0),
        help="billed failures of an earlier, incomplete invocation (counted against the cap)",
    )
    args = ap.parse_args(argv)

    raw = args.trace.read_bytes()
    trace = Trace.model_validate_json(raw)
    source = json.loads(Path(trace.meta.source_results).read_text())
    mismatches = compare_with_source(trace.rows, source, trace.meta.corpus_version)
    if trace.meta.composer != WRITER:
        print(f"REFUSING: Gate 0 measures the {WRITER} writer (ADR-049)")
        return 2
    if not trace.meta.acceptance.passed or mismatches:
        print(f"REFUSING: the trace does not reproduce its source run: {mismatches[:5]}")
        return 2

    items = build_items(trace)
    client = LLMClient(mode=LLMMode(args.mode), cassette_dir=args.cassettes)
    print(f"{len(items)} claims, {sum(i.evidence is not None for i in items)} need the judge")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    state = args.out_dir / SPEND_STATE
    prior = args.prior_failed_usd
    if state.exists():  # billed failures of an earlier invocation are not in any cassette
        prior = max(prior, Decimal(json.loads(state.read_text())["failed_usd"]))
    run = judge_under_cap(
        client,
        items,
        args.max_usd,
        prior_failed_usd=prior,
        on_failed_spend=lambda usd: state.write_text(json.dumps({"failed_usd": str(usd)}) + "\n"),
    )
    verdicts, unjudged = verdicts_for(client, items, run)
    cost = {
        "cap_usd": str(args.max_usd),
        "judge_usd": str(run.spent_usd),
        "failed_usd": str(run.failed_usd),
        "unique_requests": len(run.usage),
        "input_tokens": sum(
            u.input_tokens + u.cache_write_tokens + u.cache_read_tokens for u in run.usage.values()
        ),
        "output_tokens": sum(u.output_tokens for u in run.usage.values()),
        "basis": "harness accounting from token counts at batch prices, not an invoice",
        "batches": run.batches,
    }
    if unjudged:
        path = args.out_dir / "gate0_dev_pre.incomplete.json"
        path.write_text(
            json.dumps(
                {
                    "n_claims": len(items),
                    "n_unjudged": len(unjudged),
                    "capped": run.capped,
                    "gave_up": run.gave_up,
                    "cost": cost,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"INCOMPLETE: {len(unjudged)} claims unjudged; no rate is published. See {path}")
        return 2

    report = {
        "kind": "gate0_pre_verifier",
        "git_sha": _git_sha(),
        "source_results": trace.meta.source_results,
        "source_run_id": trace.meta.source_run_id,
        "corpus_version": trace.meta.corpus_version,
        "trace_file": str(args.trace),
        "trace_sha256": hashlib.sha256(raw).hexdigest(),
        "trace_acceptance": trace.meta.acceptance.model_dump(),
        "judge": {
            "model": client.resolve("judge"),
            "rubric": support.RUBRIC_VERSION,
            "max_tokens": support.MAX_TOKENS,
            "evidence": EVIDENCE_VERSION,
            "calibrated": False,
        },
        "interval_note": "Wilson 95%, assuming independent claims; claims share questions, so "
        "the intervals are probably too narrow",
        "n_claims": len(items),
        **build_report(verdicts),
        "dev_answerable_false_refusal_no_verifier": false_refusal(
            source, trace.meta.source_results
        ),
        "cost": cost,
        "verdicts": [v.model_dump() for v in verdicts],
    }
    path = args.out_dir / "gate0_dev_pre.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    (args.out_dir / "gate0_dev_review.md").write_text(review_markdown(items, verdicts))
    head, rule = report["headline_text_claims"], report["preregistered_rule"]
    print(f"wrote {path} (${run.spent_usd:.4f})")
    print(f"TEXT claims: unsupported {head['unsupported']}/{head['n']} -> branch {rule['branch']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
