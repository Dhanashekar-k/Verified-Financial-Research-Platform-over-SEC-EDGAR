"""End-to-end harness tests at $0: stub/fake LLM boundary only, real run/score/aggregate code."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from app.engine.answer import Answer, ChunkProvenance, Claim, Refusal
from app.llm.client import LLMClient
from app.llm.stub import register_stub
from app.llm.types import LLMMode
from eval import metrics, report, run, splits
from eval.judges.correctness import CorrectnessVerdict
from eval.schemas import ResultsFile
from eval.systems import AccountingLLM, EvalCfg, record_retrieval

FB = {
    "id": "fb-00001",
    "question": "What was 3M's FY2018 capex?",
    "gold_answer": "$1577.00",
    "company": "3M",
    "doc_name": "3M_2018_10K",
    "evidence": [{"page": 60, "text": "capex 1,577"}],
    "question_type": "metrics-generated",
    "reasoning": "Information extraction",
}
UN = [
    {"id": f"un-{i:03d}", "question": f"Q{i}?", "reason": r, "expected": "refuse"}
    for i, r in enumerate(["future_guidance"] * 4 + ["out_of_scope"] * 4, start=1)
]


def _write_sets(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "financebench.jsonl").write_text(json.dumps(FB) + "\n")
    (d / "unanswerable.jsonl").write_text("\n".join(json.dumps(r) for r in UN) + "\n")
    return d


class Retriever:
    """A system that retrieves (reports chunk ids) and answers without refusing."""

    name = "fake"

    def answer(self, question: str, cfg: EvalCfg) -> Answer:
        record_retrieval(["fb:3M_2018_10K#p12#0000", "fb:3M_2018_10K#p60#0001"])
        return Answer(
            answer_markdown="$1,577 million",
            claims=[
                Claim(
                    id="c1",
                    kind="text",
                    text="capex was $1,577m",
                    provenance=ChunkProvenance(
                        type="chunk", chunk_id="fb:3M_2018_10K#p60#0001", quote="1,577"
                    ),
                )
            ],
        )


def _items(sets_dir: Path, names: list[str]) -> list[Any]:
    return run._load_rows(names, "all", sets_dir, sets_dir / "nope.json")


@pytest.fixture
def real_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand-in metric definitions (eval/metrics.py is Shekar's and still a stub)."""
    monkeypatch.setattr(metrics, "refused", lambda a: a.refusal is not None)
    monkeypatch.setattr(metrics, "financebench_correct", lambda label: label == "correct")
    monkeypatch.setattr(
        metrics, "recall_at_k", lambda got, gold, k: float(bool(set(got[:k]) & set(gold)))
    )


def test_unscored_when_metrics_are_unimplemented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness contract for a metric that raises NotImplementedError. Patched in, so the
    test holds whether or not eval/metrics.py (Shekar's) has been written yet."""

    def unwritten(*_: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(metrics, "refused", unwritten)
    sets_dir = _write_sets(tmp_path / "sets")
    client = LLMClient(mode=LLMMode.stub)
    cfg = EvalCfg(llm=AccountingLLM(client))
    outcomes, _ = run.run_questions(
        run.SYSTEMS["stub"](), cfg, None, _items(sets_dir, ["unanswerable"]), {}, client
    )
    sets = run.aggregate(outcomes, None)
    assert sets["unanswerable"].refusal_rate is None  # type: ignore[union-attr]
    assert all(o.scores["refused"] is None for o in outcomes)


def test_a_metric_valueerror_leaves_that_row_unscored_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A metric may reject a malformed row (e.g. no gold points). One bad row must not abort a
    paid run: it is recorded as a scoring error, unscored, and every other row is still scored."""
    calls = {"n": 0}

    def refused(answer: Answer) -> bool:
        calls["n"] += 1
        if calls["n"] == 3:
            raise ValueError("malformed row")
        return answer.refusal is not None

    monkeypatch.setattr(metrics, "refused", refused)
    sets_dir = _write_sets(tmp_path / "sets")
    client = LLMClient(mode=LLMMode.stub)
    cfg = EvalCfg(llm=AccountingLLM(client))
    outcomes, _ = run.run_questions(
        run.SYSTEMS["stub"](), cfg, None, _items(sets_dir, ["unanswerable"]), {}, client
    )
    assert len(outcomes) == 8
    bad = [o for o in outcomes if "scoring_error" in o.scores]
    assert len(bad) == 1 and "malformed row" in bad[0].scores["scoring_error"]
    assert bad[0].scores.get("correct") is None  # unscored, not guessed incorrect
    assert sum(o.scores.get("refused") is not None for o in outcomes) == 7
    sets = run.aggregate(outcomes, None)
    assert sets["unanswerable"].scoring_errors == 1  # type: ignore[union-attr]


def test_a_metric_that_rejects_most_rows_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One or two malformed rows are tolerated; a metric that raises on most rows (a new judge
    label, say) must fail the run instead of reporting accuracy=None as if it were a result."""

    def refused(answer: Answer) -> bool:
        raise ValueError("unknown label")

    monkeypatch.setattr(metrics, "refused", refused)
    sets_dir = _write_sets(tmp_path / "sets")
    client = LLMClient(mode=LLMMode.stub)
    cfg = EvalCfg(llm=AccountingLLM(client))
    outcomes, _ = run.run_questions(
        run.SYSTEMS["stub"](), cfg, None, _items(sets_dir, ["unanswerable"]), {}, client
    )
    assert run.scoring_error_breaches(outcomes) == {"unanswerable": (8, 8)}
    ok = outcomes[:]
    for o in ok[1:]:
        o.scores.pop("scoring_error", None)  # 1 of 8 = 12.5% is still over; 0 of 8 is not
    assert run.scoring_error_breaches(ok) == {"unanswerable": (1, 8)}
    ok[0].scores.pop("scoring_error")
    assert run.scoring_error_breaches(ok) == {}


def test_end_to_end_stub_run_writes_valid_results(tmp_path: Path, real_metrics: None) -> None:
    sets_dir = _write_sets(tmp_path / "sets")
    register_stub(CorrectnessVerdict, lambda **_: CorrectnessVerdict(label="correct", reason="ok"))
    client = LLMClient(mode=LLMMode.stub)
    cfg = EvalCfg(llm=AccountingLLM(client))
    gold = {"fb-00001": ["fb:3M_2018_10K#p60#0001"]}
    outcomes, info = run.run_questions(
        Retriever(),
        cfg,
        AccountingLLM(client),
        _items(sets_dir, ["financebench", "unanswerable"]),
        gold,
        client,
    )
    cfg_model = run.RunConfig(
        system="fake",
        composer="claude-sonnet-5-5",
        mode="stub",
        split="all",
        sets=["financebench", "unanswerable"],
        ef_search=800,
    )
    res = run.build_results(outcomes, cfg_model, 1, 0.8, Decimal(0))
    ResultsFile.model_validate_json(res.model_dump_json())  # round-trips
    fb = res.sets["financebench"]
    assert fb.accuracy == 1.0 and fb.judge_kappa == 0.8  # type: ignore[union-attr]
    assert fb.recall_at_5 == 1.0  # type: ignore[union-attr]
    un = res.sets["unanswerable"]
    assert un.refusal_rate == 0.0 and un.n == 8  # type: ignore[union-attr]
    assert res.config.ef_search == 800 and res.corpus_version == 1
    row = next(p for p in res.per_question if p.id == "fb-00001")
    assert row.model_extra["gold_answer"] == "$1577.00"  # type: ignore[index]
    assert row.model_extra["judge_label"] == "correct"  # type: ignore[index]
    assert res.cost.total_usd == Decimal(0)


class Echo(BaseModel):
    text: str


class LLMSystem:
    """Makes one LLM call per question, so batch mode needs a round."""

    name = "llm"

    def answer(self, question: str, cfg: EvalCfg) -> Answer:
        obj, _ = cfg.llm.structured(
            "composer", "Echo.", [{"role": "user", "content": question}], Echo
        )
        return Answer(answer_markdown=obj.text, refusal=Refusal(reason="echo"))


class _Batches:
    def __init__(self) -> None:
        self.created: list[list[dict[str, Any]]] = []

    def create(self, *, requests: list[dict[str, Any]]) -> SimpleNamespace:
        self.created.append(requests)
        return SimpleNamespace(id=f"b{len(self.created)}")

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        return SimpleNamespace(processing_status="ended")

    def results(self, batch_id: str) -> list[SimpleNamespace]:
        out = []
        for r in self.created[-1]:
            msg = SimpleNamespace(
                content=[SimpleNamespace(type="text", text='{"text": "hi"}')],
                usage=SimpleNamespace(
                    input_tokens=1000,
                    output_tokens=100,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                ),
                stop_reason="end_turn",
            )
            out.append(
                SimpleNamespace(
                    custom_id=r["custom_id"], result=SimpleNamespace(type="succeeded", message=msg)
                )
            )
        return out


def test_batch_mode_runs_in_rounds_and_counts_cost_once(tmp_path: Path, real_metrics: None) -> None:
    sets_dir = _write_sets(tmp_path / "sets")
    batches = _Batches()
    sdk = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    client = LLMClient(
        mode=LLMMode.batch, cassette_dir=tmp_path / "cas", sdk_client=sdk, sleep=lambda s: None
    )
    cfg = EvalCfg(llm=AccountingLLM(client))
    outcomes, info = run.run_questions(
        LLMSystem(), cfg, None, _items(sets_dir, ["unanswerable"]), {}, client, log=lambda s: None
    )
    assert len(batches.created) == 1 and len(batches.created[0]) == 8
    assert all(o.answer is not None for o in outcomes)
    per_q = [sum(u.cost_usd for u in o.system_usage) for o in outcomes]
    # Sonnet batch: (1000 * 2 + 100 * 10) / 1e6 * 0.5 = 0.0015 per distinct request
    assert all(c == Decimal("0.0015") for c in per_q)
    assert info["batches"][0]["succeeded"] == 8


def test_splits_are_stratified_deterministic_and_disjoint(tmp_path: Path) -> None:
    sets_dir = _write_sets(tmp_path / "sets")
    a = splits.make_splits(sets_dir)
    b = splits.make_splits(sets_dir)
    assert a == b
    un = a["sets"]["unanswerable"]
    assert not set(un["dev"]) & set(un["test"])
    assert len(un["dev"]) + len(un["test"]) == 8
    # each 4-item stratum puts round(4 * 0.3) = 1 in dev
    assert len(un["dev"]) == 2
    assert "numeric" in a["missing"]


def test_report_splices_only_between_markers(tmp_path: Path) -> None:
    page = "intro\n<!-- AUTO:START -->\nold\n<!-- AUTO:END -->\noutro\n"
    out = report.splice(page, "NEW")
    assert out.startswith("intro\n") and out.endswith("outro\n")
    assert "old" not in out and "NEW" in out
    with pytest.raises(ValueError):
        report.splice("no markers", "x")


def test_report_prints_not_measured_and_kappa(tmp_path: Path) -> None:
    runs = [
        {
            "run_id": "r1",
            "config": {
                "system": "baseline",
                "composer": "c",
                "split": "dev",
                "mode": "batch",
                "ef_search": 800,
            },
            "sets": {
                "financebench": {"n": 3, "accuracy": 0.5, "ci95": [0.2, 0.8], "judge_kappa": None},
                "answerable_false_refusal": None,
            },
            "cost": {"total_usd": "0.1", "per_question_usd": "0.0333"},
            "latency_ms": {"p50": None, "p95": None},
            "per_question": [{}, {}, {}],
        }
    ]
    text = report.render(runs)
    assert "50.0%" in text and "[20.0, 80.0]" in text
    assert "kappa NOT MEASURED" in text
    assert "| NOT MEASURED | NOT MEASURED |" in text  # batch latency


# --- review fixes -------------------------------------------------------------------------


class Failing:
    """Fails on every question (as a BatchGaveUp / LLMResponseError would)."""

    name = "failing"

    def answer(self, question: str, cfg: EvalCfg) -> Answer:
        from app.llm.client import LLMResponseError
        from app.llm.types import Usage

        usage = Usage(
            model="claude-sonnet-5-5",
            input_tokens=10,
            cache_write_tokens=0,
            cache_read_tokens=0,
            output_tokens=5,
            cost_usd=Decimal("0.01"),
            latency_ms=1,
        )
        cfg.llm.ledger.append(usage)
        raise LLMResponseError("truncated", usage)


def test_errored_questions_count_against_the_system(tmp_path: Path, real_metrics: None) -> None:
    """A failing system can't score better by failing: errors are incorrect / not refused."""
    sets_dir = _write_sets(tmp_path / "sets")
    client = LLMClient(mode=LLMMode.stub)
    cfg = EvalCfg(llm=AccountingLLM(client))
    outcomes, _ = run.run_questions(
        Failing(), cfg, None, _items(sets_dir, ["financebench", "unanswerable"]), {}, client
    )
    sets = run.aggregate(outcomes, None)
    assert sets["financebench"].accuracy == 0.0  # type: ignore[union-attr]
    assert sets["financebench"].n_scored == 1  # type: ignore[union-attr]
    assert sets["unanswerable"].refusal_rate == 0.0  # type: ignore[union-attr]
    assert sets["unanswerable"].errors == 8  # type: ignore[union-attr]
    # the billed failure is still in the per-question cost
    assert all(sum(u.cost_usd for u in o.system_usage) == Decimal("0.01") for o in outcomes)


def test_last_round_does_not_submit_a_batch_it_cannot_use(
    tmp_path: Path, real_metrics: None
) -> None:
    sets_dir = _write_sets(tmp_path / "sets")
    batches = _Batches()
    sdk = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    client = LLMClient(
        mode=LLMMode.batch, cassette_dir=tmp_path / "cas", sdk_client=sdk, sleep=lambda s: None
    )
    cfg = EvalCfg(llm=AccountingLLM(client))
    outcomes, info = run.run_questions(
        LLMSystem(),
        cfg,
        None,
        _items(sets_dir, ["unanswerable"]),
        {},
        client,
        batch_rounds=1,
        log=lambda s: None,
    )
    assert batches.created == []  # nothing paid for
    assert all(o.error == "batch rounds exhausted" for o in outcomes)


def test_failed_judge_call_cost_is_kept(tmp_path: Path, real_metrics: None) -> None:
    from app.llm.client import LLMResponseError

    sets_dir = _write_sets(tmp_path / "sets")
    client = LLMClient(mode=LLMMode.stub)

    class BadJudge(AccountingLLM):
        def structured(self, *a: Any, **k: Any) -> Any:
            from app.llm.types import Usage

            u = Usage(
                model="claude-sonnet-5-5",
                input_tokens=1,
                cache_write_tokens=0,
                cache_read_tokens=0,
                output_tokens=1,
                cost_usd=Decimal("0.02"),
                latency_ms=1,
            )
            self.ledger.append(u)
            raise LLMResponseError("bad json", u)

    cfg = EvalCfg(llm=AccountingLLM(client))
    outcomes, _ = run.run_questions(
        Retriever(), cfg, BadJudge(client), _items(sets_dir, ["financebench"]), {}, client
    )
    (o,) = outcomes
    assert "judge_error" in o.scores
    assert sum(u.cost_usd for u in o.judge_usage) == Decimal("0.02")


def test_stub_mode_refuses_to_write_results(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="fake answers"):
        run.main(["--system", "stub", "--mode", "stub", "--sets-dir", str(tmp_path)])


def test_report_skips_stub_runs_and_keeps_partial_runs_separate(tmp_path: Path) -> None:
    def write(run_id: str, mode: str, sets: list[str]) -> None:
        body = {
            "run_id": run_id,
            "config": {
                "system": "baseline",
                "composer": "c",
                "split": "dev",
                "mode": mode,
                "sets": sets,
                "judge": "j",
            },
            "sets": {},
        }
        (tmp_path / f"{run_id}.json").write_text(json.dumps(body))

    write("2026-01-01_a", "batch", ["financebench", "unanswerable"])
    write("2026-01-02_b", "replay", ["unanswerable"])
    write("2026-01-03_c", "stub", ["financebench", "unanswerable"])
    ids = {r["run_id"] for r in report.select_runs(tmp_path)}
    assert ids == {"2026-01-01_a", "2026-01-02_b"}


def test_splits_are_stable_when_a_row_is_added(tmp_path: Path) -> None:
    sets_dir = _write_sets(tmp_path / "sets")
    before = splits.make_splits(sets_dir)["sets"]["unanswerable"]
    rows = (sets_dir / "unanswerable.jsonl").read_text()
    extra = {"id": "un-999", "question": "Q?", "reason": "out_of_scope", "expected": "refuse"}
    (sets_dir / "unanswerable.jsonl").write_text(rows + json.dumps(extra) + "\n")
    after = splits.make_splits(sets_dir)["sets"]["unanswerable"]
    # the future_guidance stratum is untouched
    fg = {f"un-{i:03d}" for i in range(1, 5)}
    assert set(before["dev"]) & fg == set(after["dev"]) & fg


def test_stale_splits_file_fails(tmp_path: Path) -> None:
    sets_dir = _write_sets(tmp_path / "sets")
    sp = splits.make_splits(sets_dir)
    sp["sets"]["unanswerable"]["test"].remove(sp["sets"]["unanswerable"]["test"][0])
    path = tmp_path / "splits.json"
    path.write_text(json.dumps(sp))
    with pytest.raises(ValueError, match="stale"):
        run._load_rows(["unanswerable"], "dev", sets_dir, path)


@pytest.mark.parametrize(
    ("expr", "ids"),
    [
        (" ", []),
        ("f:1 f:2", [1, 2]),
        ("()", []),
        ("f:1 / f:2", [9]),
        ("abs(f:1)", [1]),
        ("f:1 ** 2", [1]),
    ],
)
def test_expr_provenance_rejects_bad_expressions(expr: str, ids: list[int]) -> None:
    from app.engine.answer import ExprProvenance

    with pytest.raises(ValueError):
        ExprProvenance(type="expr", expr=expr, fact_ids=ids)


def test_expr_provenance_accepts_contract_example() -> None:
    from app.engine.answer import ExprProvenance

    p = ExprProvenance(type="expr", expr="(f:111 - f:222) / f:222 * 100", fact_ids=[111, 222])
    assert p.fact_ids == [111, 222]


def test_any_system_exception_is_a_recorded_error_not_an_abort(tmp_path: Path) -> None:
    sets_dir = _write_sets(tmp_path / "sets")

    class Broken:
        name = "broken"

        def answer(self, question: str, cfg: EvalCfg) -> Answer:
            raise ConnectionError("tunnel dropped")

    client = LLMClient(mode=LLMMode.stub)
    outcomes, _ = run.run_questions(
        Broken(),
        EvalCfg(llm=AccountingLLM(client)),
        None,
        _items(sets_dir, ["unanswerable"]),
        {},
        client,
    )
    assert len(outcomes) == 8
    assert all(o.error == "ConnectionError: tunnel dropped" for o in outcomes)


def test_results_config_records_the_sample_and_timeout() -> None:
    cfg = run.RunConfig(
        system="longctx",
        composer="claude-sonnet-5-5",
        mode="record",
        split="all",
        sets=["financebench"],
        sample=30,
        sample_seed=13,
        request_timeout_s=600.0,
    )
    back = run.RunConfig.model_validate_json(cfg.model_dump_json())
    assert (back.sample, back.sample_seed, back.request_timeout_s) == (30, 13, 600.0)
    assert run.RunConfig(system="s", composer="c", mode="m", split="all", sets=[]).sample is None
