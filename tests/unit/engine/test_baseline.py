"""app/engine/baseline.py against the stub LLM and injected retrieval (no DB, no model, $0).

The LLM boundary is stubbed (register_stub / a fake SDK). Retrieval, passage lookup and the
embedder are the baseline's own injected collaborators, so BaselineSystem itself runs for real.
Rule 4 allows stubbing only the LLM and SEC boundaries; the injected fakes here are justified
because the default retrieval and passage SQL run against real pgvector in
tests/integration/test_baseline_pg.py, and the default embedder's sha pin is tested below.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from app.engine.answer import Answer, ChunkProvenance, Claim
from app.engine.baseline import (
    COMPOSER_SYSTEM,
    EF_SEARCH,
    TOP_K,
    BaselineSystem,
    Passage,
    build_messages,
)
from app.engine.retrieval.types import ChunkHit
from app.llm.client import LLMClient
from app.llm.stub import register_stub
from app.llm.types import LLMMode
from eval.systems import AccountingLLM, EvalCfg, reset_retrieval, retrieval_sink

IDS = [f"fb:3M_2018_10K#p{p}#0000" for p in range(1, 11)]
TEXTS = {cid: f"text of {cid}: capital expenditures were $1,577 million" for cid in IDS}


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_queries(self, questions: Sequence[str]) -> np.ndarray:
        self.calls.append(list(questions))
        return np.full((len(questions), 384), 1 / np.sqrt(384), dtype=np.float32)


class Retrieval:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, vector: Sequence[float], k: int, ef: int) -> list[ChunkHit]:
        self.calls.append((k, ef))
        assert len(vector) == 384
        # returned out of rank order on purpose: the baseline must order by rank
        return [ChunkHit(chunk_id=IDS[i], rank=i + 1, score=1 - i / 100) for i in range(k)][::-1]


def fetch(ids: list[str]) -> dict[str, Passage]:
    return {i: Passage(i, TEXTS[i], "10-K", 2018, None, "financebench_pdf") for i in ids}


def _answer() -> Answer:
    return Answer(
        answer_markdown="3M's FY2018 capex was $1,577 million [1].",
        claims=[
            Claim(
                id="c1",
                kind="numeric",
                text="3M's FY2018 capex was $1,577 million.",
                value=1577,
                unit="USD millions",
                display="$1,577 million",
                primary=True,
                provenance=ChunkProvenance(type="chunk", chunk_id=IDS[0], quote="$1,577 million"),
            )
        ],
    )


def _system(**kw: Any) -> tuple[BaselineSystem, FakeEmbedder, Retrieval]:
    emb, ret = FakeEmbedder(), Retrieval()
    return BaselineSystem(embedder=emb, retrieve=ret, fetch_passages=fetch, **kw), emb, ret


def test_pipeline_returns_composer_answer_and_records_retrieval() -> None:
    register_stub(Answer, lambda **_: _answer())
    system, emb, ret = _system()
    cfg = EvalCfg(llm=AccountingLLM(LLMClient(mode=LLMMode.stub)))
    sink, token = retrieval_sink()
    try:
        ans = system.answer("What was 3M's FY2018 capex?", cfg)
    finally:
        reset_retrieval(token)
    assert ans == _answer()
    assert emb.calls == [["What was 3M's FY2018 capex?"]]
    assert ret.calls == [(TOP_K, EF_SEARCH)] == [(8, 800)]
    assert sink == IDS[:8]  # rank order, not the order retrieval returned
    assert cfg.llm.ledger[0].model == "claude-sonnet-5-5"


class _SDK:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=_answer().model_dump_json())],
            usage=SimpleNamespace(
                input_tokens=7000,
                output_tokens=600,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            stop_reason="end_turn",
        )


def test_composer_request_shape_static_system_then_passages_and_question() -> None:
    sdk = _SDK()
    client = LLMClient(mode=LLMMode.live, sdk_client=sdk, sleep=lambda s: None)
    system, _, _ = _system()
    cfg = EvalCfg(llm=AccountingLLM(client))
    system.answer("What was 3M's FY2018 capex?", cfg)
    req = sdk.calls[0]
    assert req["model"] == "claude-sonnet-5-5"
    assert req["system"][0]["text"] == COMPOSER_SYSTEM  # static: the cached prefix
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    user = req["messages"][0]["content"]
    assert user.index("[1] chunk_id=" + IDS[0]) < user.index("[8] chunk_id=" + IDS[7])
    assert IDS[8] not in user  # top-8 only
    assert user.rstrip().endswith("Question: What was 3M's FY2018 capex?")
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in req
    # cost from accounting: Sonnet 7000 in / 600 out = 0.014 + 0.006
    assert cfg.llm.ledger[0].cost_usd == Decimal("0.020")


def test_composer_role_follows_cfg_registry() -> None:
    register_stub(Answer, lambda **_: _answer())
    client = LLMClient(mode=LLMMode.stub, registry={"composer": "claude-haiku-5-5"})
    system, _, _ = _system()
    cfg = EvalCfg(llm=AccountingLLM(client))
    system.answer("q", cfg)
    assert cfg.llm.ledger[0].model == "claude-haiku-5-5"


def test_top_k_and_ef_search_come_from_cfg() -> None:
    register_stub(Answer, lambda **_: _answer())
    system, _, ret = _system()
    cfg = EvalCfg(llm=AccountingLLM(LLMClient(mode=LLMMode.stub)), top_k=5, ef_search=1000)
    system.answer("q", cfg)
    assert ret.calls == [(5, 1000)]


def test_missing_passage_fails_loudly() -> None:
    register_stub(Answer, lambda **_: _answer())
    system = BaselineSystem(
        embedder=FakeEmbedder(), retrieve=Retrieval(), fetch_passages=lambda ids: {}
    )
    with pytest.raises(LookupError, match="missing from the chunks view"):
        system.answer("q", EvalCfg(llm=AccountingLLM(LLMClient(mode=LLMMode.stub))))


def test_default_retrieval_needs_a_dsn() -> None:
    register_stub(Answer, lambda **_: _answer())
    system = BaselineSystem(embedder=FakeEmbedder())
    with pytest.raises(RuntimeError, match="serving DSN"):
        system.answer("q", EvalCfg(llm=AccountingLLM(LLMClient(mode=LLMMode.stub))))


def test_import_loads_no_model_and_names_the_pinned_embedder() -> None:
    system = BaselineSystem()
    assert system._embedder is None
    assert "onnx-Q" in system.embedder_name and "instruction" in system.embedder_name


def test_prompt_rules() -> None:
    """Chunk-only provenance, passages only, and (by default) derived numbers allowed: a naive
    RAG comparator that refused every growth/ratio question would inflate the engine's lead."""
    assert 'Never use {"type": "expr"}' in COMPOSER_SYSTEM
    assert "ONLY the numbered" in COMPOSER_SYSTEM
    assert '"numeric_derived"' in COMPOSER_SYSTEM
    assert "do not compute" not in COMPOSER_SYSTEM
    msgs = build_messages("Q?", [Passage("a#1#0000", "body")])
    assert msgs == [
        {"role": "user", "content": "Passages:\n\n[1] chunk_id=a#1#0000 ()\nbody\n\nQuestion: Q?"}
    ]


def test_strict_variant_adds_the_no_arithmetic_rule() -> None:
    sdk = _SDK()
    client = LLMClient(mode=LLMMode.live, sdk_client=sdk, sleep=lambda s: None)
    system = BaselineSystem(
        embedder=FakeEmbedder(),
        retrieve=Retrieval(),
        fetch_passages=fetch,
        strict_no_arithmetic=True,
    )
    system.answer("q", EvalCfg(llm=AccountingLLM(client)))
    sent = sdk.calls[0]["system"][0]["text"]
    assert sent.startswith(COMPOSER_SYSTEM) and "do not compute" in sent


def test_empty_retrieval_refuses_without_calling_the_llm() -> None:
    from app.llm.client import LLMClient as C

    class Nothing:
        def __call__(self, v: Sequence[float], k: int, ef: int) -> list[ChunkHit]:
            return []

    sdk = _SDK()
    cfg = EvalCfg(llm=AccountingLLM(C(mode=LLMMode.live, sdk_client=sdk)))
    system = BaselineSystem(embedder=FakeEmbedder(), retrieve=Nothing(), fetch_passages=fetch)
    ans = system.answer("q", cfg)
    assert ans.refusal is not None and sdk.calls == [] and cfg.llm.ledger == []


def test_invented_fact_ids_are_rejected() -> None:
    from app.engine.answer import ExprProvenance
    from app.engine.baseline import BaselineOutputError

    bad = Answer(
        answer_markdown="x",
        claims=[
            Claim(
                id="c1",
                kind="numeric_derived",
                text="margin 16.5%",
                value=0.165,
                provenance=ExprProvenance(type="expr", expr="f:1 / f:2", fact_ids=[1, 2]),
            )
        ],
    )
    register_stub(Answer, lambda **_: bad)
    system, _, _ = _system()
    with pytest.raises(BaselineOutputError, match="c1"):
        system.answer("q", EvalCfg(llm=AccountingLLM(LLMClient(mode=LLMMode.stub))))


def test_real_embedder_path_checks_the_pinned_sha(tmp_path: Any) -> None:
    from pipelines.common.embedding import ARTIFACT_ONNX_FILE, ModelIntegrityError

    (tmp_path / ARTIFACT_ONNX_FILE).write_bytes(b"not the pinned model")
    system = BaselineSystem(model_dir=tmp_path)
    with pytest.raises(ModelIntegrityError, match="pinned"):
        system._get_embedder()


def test_harness_registry_builds_the_baseline() -> None:
    from eval.systems import SYSTEMS

    assert isinstance(SYSTEMS["baseline"](), BaselineSystem)
