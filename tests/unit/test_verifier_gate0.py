"""M6 Gate 0: the claim items, the spending cap, the rates and the pre-registered rule."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.engine.answer import Answer, ChunkProvenance, Claim, ExprProvenance, Refusal
from app.llm.client import LLMClient
from app.llm.types import LLMMode
from eval.judges.support import MAX_TOKENS, SupportVerdict
from eval.verifier_gate0 import (
    Item,
    UnresolvedBatch,
    Verdict,
    build_items,
    build_report,
    decide_branch,
    false_refusal,
    group_stats,
    judge_under_cap,
    review_markdown,
    verdicts_for,
    worst_case_usd,
)
from eval.verifier_trace import Acceptance, ChunkRecord, Trace, TraceMeta, TraceRow
from tests.unit.llm.fakes import ExplodingSDK, make_response

MODEL = "claude-sonnet-5-5"


def trace() -> Trace:
    answer = Answer(
        answer_markdown="m",
        refusal=Refusal(reason="partly"),
        claims=[
            Claim(
                id="c1",
                kind="numeric",
                text="Revenue was 5.",
                provenance=ExprProvenance(type="expr", expr="f:7", fact_ids=[7]),
            ),
            Claim(
                id="c2",
                kind="text",
                text="Sales fell.",
                provenance=ChunkProvenance(type="chunk", chunk_id="shown", quote="fell"),
            ),
            Claim(
                id="c3",
                kind="text",
                text="Costs rose.",
                provenance=ChunkProvenance(type="chunk", chunk_id="ghost", quote="rose"),
            ),
        ],
    )
    meta = TraceMeta(
        source_results="s.json",
        source_run_id="r",
        source_git_sha="a",
        capture_git_sha="b",
        captured_at="t",
        split="dev",
        composer="c",
        ef_search=800,
        top_k=8,
        corpus_version=1,
        acceptance=Acceptance(passed=True),
    )
    row = TraceRow(id="q1", set="numeric", question="q", answer=answer, shown_chunk_ids=["other"])
    silent = TraceRow(id="q2", set="unanswerable", question="q2", answer=None)
    return Trace(
        meta=meta,
        rows=[row, silent],
        chunks={"shown": ChunkRecord(chunk_id="shown", text="Sales fell.")},
    )


def test_items_cover_every_claim_and_flag_missing_and_unshown_passages() -> None:
    items = build_items(trace())
    assert [i.claim_id for i in items] == ["c1", "c2", "c3"]
    assert "Fact f:7: no such fact" in (items[0].evidence or "")
    assert items[1].evidence and not items[1].shown  # exists, but the composer never saw it
    assert items[2].evidence is None
    assert all(i.refused for i in items)


def item(n: int, text: str | None = None) -> Item:
    return Item(
        question_id=f"q{n}",
        set_name="numeric",
        claim_id="c1",
        kind="text",
        claim_text=text or f"claim {n}",
        evidence=f"evidence {n if text is None else text}",
        refused=False,
        cited="x",
    )


class FakeBatches:
    """The SDK batch boundary: every request succeeds with a 'supported' verdict."""

    def __init__(self, input_tokens: int = 1000, output_tokens: int = 200) -> None:
        self.tokens = (input_tokens, output_tokens)
        self.submitted: list[list[str]] = []
        self.requests: list[dict[str, Any]] = []

    def create(self, requests: list[dict[str, Any]]) -> Any:
        self.submitted.append([r["custom_id"] for r in requests])
        self.requests += [r["params"] for r in requests]
        return SimpleNamespace(id=f"batch_{len(self.submitted)}")

    def retrieve(self, batch_id: str) -> Any:
        return SimpleNamespace(processing_status="ended")

    def results(self, batch_id: str) -> list[Any]:
        keys = self.submitted[int(batch_id.split("_")[1]) - 1]
        message = make_response(
            SupportVerdict(label="supported", reason="ok"),
            input_tokens=self.tokens[0],
            output_tokens=self.tokens[1],
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        return [
            SimpleNamespace(custom_id=k, result=SimpleNamespace(type="succeeded", message=message))
            for k in keys
        ]


def batch_client(tmp_path: Path, batches: FakeBatches) -> LLMClient:
    sdk = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    return LLMClient(mode=LLMMode.batch, cassette_dir=tmp_path, sdk_client=sdk)


def test_judges_every_item_once_in_chunks(tmp_path: Path) -> None:
    batches = FakeBatches()
    client = batch_client(tmp_path, batches)
    items = [item(n) for n in range(5)] + [item(99, "claim 0"), item(98, "claim 0")]
    run = judge_under_cap(client, items, Decimal(2), chunk_size=2, log=lambda _: None)
    verdicts, unjudged = verdicts_for(client, items, run)
    assert not unjudged and not run.capped and len(verdicts) == 7
    sent = [k for chunk in batches.submitted for k in chunk]
    assert len(sent) == len(set(sent)) == 6  # the two identical items were bought once
    assert max(map(len, batches.submitted)) <= 2
    assert all(r["max_tokens"] == MAX_TOKENS for r in batches.requests)
    # 6 x (1000 in at $1 + 200 out at $5) per million, batch prices
    assert run.spent_usd == Decimal("0.012")


def test_never_submits_a_chunk_whose_worst_case_could_break_the_cap(tmp_path: Path) -> None:
    batches = FakeBatches()
    client = batch_client(tmp_path, batches)
    items = [item(n) for n in range(10)]
    one = worst_case_usd(MODEL, "claim 0", "evidence 0")
    cap = one * 3 + Decimal("0.001")
    run = judge_under_cap(client, items, cap, log=lambda _: None)
    assert len(batches.submitted[0]) == 3  # three worst cases fit, a fourth does not
    assert run.spent_usd <= cap
    _, unjudged = verdicts_for(client, items, run)
    assert (not unjudged) or run.capped


def test_a_cap_below_one_request_submits_nothing(tmp_path: Path) -> None:
    batches = FakeBatches()
    client = batch_client(tmp_path, batches)
    run = judge_under_cap(client, [item(1)], Decimal("0.001"), log=lambda _: None)
    assert run.capped and not batches.submitted and run.spent_usd == 0


def test_billed_failures_and_earlier_purchases_count_against_the_cap(tmp_path: Path) -> None:
    first = batch_client(tmp_path, FakeBatches())
    judge_under_cap(first, [item(1)], Decimal(1), log=lambda _: None)
    batches = FakeBatches()
    again = batch_client(tmp_path, batches)
    run = judge_under_cap(
        again,
        [item(1), item(2)],
        Decimal("0.05"),
        prior_failed_usd=Decimal("0.049"),
        log=lambda _: None,
    )
    assert run.capped and not batches.submitted
    assert run.spent_usd == Decimal("0.049") + Decimal("0.002")  # item 1's cassette still counts


def test_worst_case_bounds_the_whole_reply_allowance() -> None:
    # 8192 output tokens at $5 per million (batch) is $0.04096 before any input
    assert worst_case_usd(MODEL, "c", "e") > Decimal("0.04096")


def test_replay_mode_never_calls_the_api_and_leaves_misses_unjudged(tmp_path: Path) -> None:
    client = LLMClient(mode=LLMMode.replay, cassette_dir=tmp_path, sdk_client=ExplodingSDK())
    with pytest.raises(Exception, match="no cassette"):
        judge_under_cap(client, [item(1)], Decimal(2), log=lambda _: None)


def test_a_missing_passage_is_unsupported_without_a_judge_call(tmp_path: Path) -> None:
    batches = FakeBatches()
    client = batch_client(tmp_path, batches)
    items = [i for i in build_items(trace()) if i.claim_id == "c3"]
    run = judge_under_cap(client, items, Decimal(1), log=lambda _: None)
    [verdict], unjudged = verdicts_for(client, items, run)
    assert (verdict.label, verdict.source, unjudged) == ("unsupported", "missing_chunk", [])
    assert not batches.submitted


def verdict(label: str, kind: str = "text", n: int = 0, **over: Any) -> Verdict:
    base = {
        "question_id": f"q{n}",
        "set": "numeric",
        "claim_id": f"c{n}",
        "kind": kind,
        "label": label,
        "reason": "r",
        "source": "judge",
        "refused": False,
        "shown": True,
        "cited": "x",
        "claim_text": "t",
    }
    return Verdict.model_validate(base | over)


def many(text_bad: int, text_total: int) -> list[Verdict]:
    return [verdict("unsupported" if i < text_bad else "supported", n=i) for i in range(text_total)]


def test_partial_is_in_the_denominator_only() -> None:
    stats = group_stats([verdict("unsupported"), verdict("partial"), verdict("supported")])
    assert stats["unsupported_rate"] == pytest.approx(1 / 3)
    assert stats["partial_rate"] == pytest.approx(1 / 3)
    assert len(stats["unsupported_ci95_wilson"]) == 2


def test_empty_group_has_no_rate() -> None:
    assert group_stats([])["unsupported_rate"] is None


@pytest.mark.parametrize(
    ("bad", "total", "branch"),
    [
        (0, 20, "deterministic"),
        (5, 100, "deterministic"),
        (6, 100, "entailment"),
        (1, 19, "entailment"),
    ],
)
def test_preregistered_rule_includes_exactly_five_percent(
    bad: int, total: int, branch: str
) -> None:
    assert decide_branch(group_stats(many(bad, total))) == branch


def test_rule_cannot_be_applied_without_text_claims() -> None:
    with pytest.raises(ValueError):
        decide_branch(group_stats([]))


def test_numeric_claims_never_move_the_headline_or_the_branch() -> None:
    verdicts = many(1, 40) + [verdict("unsupported", "numeric", n=100 + i) for i in range(30)]
    report = build_report(verdicts)
    assert report["headline_text_claims"]["n"] == 40
    assert report["preregistered_rule"]["branch"] == "deterministic"
    assert report["numeric_claims"]["n"] == 30
    assert "does not decide headroom" in report["numeric_claims"]["note"]
    assert report["all_claims"]["n"] == 70


def test_false_refusal_is_copied_with_an_interval_and_cross_checked() -> None:
    rows = [
        {"set": "numeric", "refused": True},
        {"set": "narrative", "refused": False},
        {"set": "unanswerable", "refused": True},  # not answerable: left out
    ]
    out = false_refusal({"sets": {"answerable_false_refusal": 0.5}, "per_question": rows}, "f")
    assert (out["value"], out["n"]) == (0.5, 2)
    with pytest.raises(ValueError):
        false_refusal({"sets": {"answerable_false_refusal": 0.9}, "per_question": rows}, "f")


def test_review_lists_only_non_supported_claims_with_their_evidence() -> None:
    items = build_items(trace())
    verdicts = [
        verdict("supported", n=1, question_id="q1", claim_id="c1"),
        verdict("partial", n=2, question_id="q1", claim_id="c2"),
        verdict("unsupported", n=3, question_id="q1", claim_id="c3"),
    ]
    out = review_markdown(items, verdicts)
    assert "q1 c2: partial" in out and "q1 c3: unsupported" in out and "q1 c1" not in out
    assert "Sales fell." in out and "does not exist" in out
    assert out.index("q1 c3") < out.index("q1 c2")  # unsupported first


class Interrupted(FakeBatches):
    """Creates the batch, then dies while polling (a crash after paying)."""

    def retrieve(self, batch_id: str) -> Any:
        raise KeyboardInterrupt


def test_an_interrupted_batch_is_collected_first_and_never_bought_twice(tmp_path: Path) -> None:
    broken = Interrupted()
    items = [item(n) for n in range(10)]
    with pytest.raises(KeyboardInterrupt):
        judge_under_cap(
            batch_client(tmp_path, broken), items, Decimal(2), chunk_size=10, log=lambda _: None
        )
    batches = FakeBatches()
    batches.submitted = list(broken.submitted)  # the API still holds batch_1
    one = worst_case_usd(MODEL, "claim 0", "evidence 0")
    client = batch_client(tmp_path, batches)
    # a smaller cap would shape a smaller chunk, which could not resume the saved batch
    run = judge_under_cap(client, items, one * 5, chunk_size=3, log=lambda _: None)
    _, unjudged = verdicts_for(client, items, run)
    assert not unjudged and len(batches.submitted) == 1
    assert not list(tmp_path.glob(".batch_*.json"))


def test_a_partly_collected_saved_batch_stops_the_run(tmp_path: Path) -> None:
    broken = Interrupted()
    items = [item(1), item(2)]
    with pytest.raises(KeyboardInterrupt):
        judge_under_cap(batch_client(tmp_path, broken), items, Decimal(2), log=lambda _: None)
    first = broken.submitted[0][0]
    judged = SupportVerdict(label="supported", reason="ok").model_dump_json()
    usage = (
        '{"model": "claude-sonnet-5-5", "input_tokens": 1, "cache_write_tokens": 0, '
        '"cache_read_tokens": 0, "output_tokens": 1, "cost_usd": "0", "latency_ms": 0}'
    )
    (tmp_path / f"{first}.json").write_text(
        f'{{"request_key": "{first}", "model": "{MODEL}", "output": {judged}, "usage": {usage}}}'
    )
    batches = FakeBatches()
    with pytest.raises(UnresolvedBatch):
        judge_under_cap(batch_client(tmp_path, batches), items, Decimal(2), log=lambda _: None)
    assert not batches.submitted


def test_billed_failures_are_reported_for_persisting(tmp_path: Path) -> None:
    class Truncating(FakeBatches):
        def results(self, batch_id: str) -> list[Any]:
            out = super().results(batch_id)
            for entry in out:
                entry.result.message.stop_reason = "max_tokens"
            return out

    seen: list[Decimal] = []
    client = batch_client(tmp_path, Truncating())
    run = judge_under_cap(
        client, [item(1)], Decimal(2), on_failed_spend=seen.append, log=lambda _: None
    )
    assert run.gave_up and seen[-1] == run.failed_usd > 0


def test_rule_reports_how_close_the_result_is_to_the_threshold() -> None:
    rule = build_report(many(5, 118))["preregistered_rule"]
    assert (rule["observed_unsupported"], rule["max_unsupported_for_deterministic"]) == (5, 5)
