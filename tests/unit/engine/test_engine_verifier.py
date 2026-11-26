"""The verify stage in the engine and the harness (ADR-049). The owned checks are injected fakes;
the engine, composer inputs and evidence loading are real."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.engine.answer import Answer
from app.engine.entities.resolve_company import load_aliases
from app.engine.plan.catalog import load_catalog
from app.engine.plan.schema import CompileOutcome, PeriodRef
from app.engine.verify import pipeline
from app.engine.verify.models import company_names
from eval.systems import EvalCfg, reset_verification, verification_sink
from eval.verifier_inputs import Reference, context_for
from eval.verifier_trace import TraceRow
from tests.support.fake_verifier import FakeVerifier
from tests.unit.engine.test_engine import (
    APPLE,
    Conn,
    ScriptedLLM,
    Spy,
    engine_with,
    numeric_answer,
    numeric_plan,
    result,
)


class VerifierConn(Conn):
    """Also answers the verifier's evidence reads (no rows: nothing cited exists)."""

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


def run(
    verifier_flag: str, fake: FakeVerifier | None = None, question: str = "q"
) -> tuple[Any, Answer, ScriptedLLM, VerifierConn]:
    conn = VerifierConn()
    eng = engine_with(Spy(CompileOutcome(results=[result()])), lambda: conn)
    if fake is not None:
        eng._verifier = fake.verifier()
    llm = ScriptedLLM(numeric_plan(periods=[PeriodRef(fiscal_year=2024)]), numeric_answer())
    cfg = EvalCfg(llm=llm, dsn="postgres://x", verifier=verifier_flag)  # type: ignore[arg-type]
    return eng, eng.answer(question, cfg), llm, conn


def test_off_returns_the_composer_answer_and_reads_no_evidence() -> None:
    fake = FakeVerifier()
    eng, answer, _, conn = run("off", fake)
    assert answer == numeric_answer()
    assert eng.diagnostics["q"].verification is None
    assert fake.numeric_calls == [] and not any("fact_id = ANY" in s for s in conn.queries)


def test_the_default_is_off() -> None:
    assert EvalCfg(llm=None).verifier == "off"  # type: ignore[arg-type]


def test_on_verifies_with_no_extra_model_call_and_records_the_report() -> None:
    sink, token = verification_sink()
    try:
        fake = FakeVerifier()
        eng, answer, llm, conn = run("on", fake)
    finally:
        reset_verification(token)
    assert len(llm.calls) == 2  # planner and composer only
    assert answer.claims == numeric_answer().claims
    assert answer.answer_markdown == "Net income was $97.0 billion. [1]"  # not composer prose
    report = eng.diagnostics["q"].verification
    assert report is not None and report.status == "verified"
    assert sink["status"] == "verified" and sink["claims_total"] == len(answer.claims)
    assert any("fact_id = ANY" in s for s in conn.queries)
    assert "verify" in eng.diagnostics["q"].timings_ms
    ctx = fake.contexts[0]
    assert ctx.results == [result()] and ctx.allowed_ciks == {APPLE}
    assert ctx.catalog and APPLE in ctx.companies and not ctx.assumed_period


def test_the_engine_builds_the_context_the_offline_runners_build() -> None:
    fake = FakeVerifier()
    eng, _, _, _ = run("on", fake)
    diag = eng.diagnostics["q"]
    index = eng._company_index("postgres://x")
    recorded = TraceRow(
        id="x",
        set="numeric",
        question="q",
        plan=diag.plan,
        results=diag.results,
        not_found=diag.not_found,
        assumed_period=diag.assumed_period,
        unsupported=diag.unsupported,
        text_only_fallback=diag.text_only_fallback,
    )
    reference = Reference(
        companies=company_names(index.records.values(), load_aliases()),
        universe=frozenset({APPLE}),
        catalog=load_catalog(),
    )
    assert fake.contexts[0] == context_for(recorded, reference)


def test_a_templated_refusal_is_not_verified() -> None:
    fake = FakeVerifier()
    eng = engine_with(Spy(), VerifierConn)
    eng._verifier = fake.verifier()
    llm = ScriptedLLM(numeric_plan(needs_clarification="Which company?"))
    answer = eng.answer("q", EvalCfg(llm=llm, dsn="x", verifier="on"))  # type: ignore[arg-type]
    assert answer.refusal and eng.diagnostics["q"].verification is None


def test_with_stubbed_cores_on_raises_instead_of_passing_claims_through() -> None:
    from app.engine.verify import numeric

    if numeric.IMPLEMENTED:
        pytest.skip("the cores are implemented")
    with pytest.raises(NotImplementedError):
        run("on")


def test_the_cli_refuses_on_before_any_work_while_a_core_is_a_stub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from eval import run as harness

    def stubbed(*_: Any) -> None:
        raise pipeline.VerifierNotImplemented("still stubbed: numeric")

    monkeypatch.setattr(pipeline, "require_implemented", stubbed)
    monkeypatch.setattr(harness, "LLMClient", lambda **_: pytest.fail("a client was built"))
    args = ["--system", "engine", "--verifier", "on", "--out", str(tmp_path)]
    with pytest.raises(SystemExit, match="still stubbed"):
        harness.main(args)
    assert list(tmp_path.iterdir()) == []


def test_the_cli_allows_on_for_the_engine_only(tmp_path: Path) -> None:
    from eval import run as harness

    with pytest.raises(SystemExit) as err:
        harness.main(["--system", "baseline", "--verifier", "on", "--out", str(tmp_path)])
    assert err.value.code == 2


def test_report_keeps_verifier_runs_apart_and_treats_old_runs_as_off(tmp_path: Path) -> None:
    import json

    from eval.report import select_runs

    def write(run_id: str, verifier: str | None) -> None:
        config = {"system": "engine", "composer": "c", "split": "dev", "sets": ["numeric"],
                  "mode": "batch"}  # fmt: skip
        if verifier is not None:
            config["verifier"] = verifier
        (tmp_path / f"{run_id}.json").write_text(
            json.dumps({"run_id": run_id, "sets": {}, "config": config})
        )

    write("1_old", None)
    write("2_off", "off")
    write("3_on", "on")
    assert sorted(r["run_id"] for r in select_runs(tmp_path)) == ["2_off", "3_on"]
