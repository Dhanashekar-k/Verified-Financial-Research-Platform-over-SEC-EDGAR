"""BaselineSystem's default retrieval path against a real Postgres + pgvector (Testcontainers).

The serving tables are loaded the production way (export -> load_version -> swap), the query
vector is one chunk's own exported vector (so no embedding model is needed), and the composer is
the stub LLM. Checks the default dense_search + passage lookup wiring, not answer quality.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import numpy as np
import pytest

from app.engine.answer import Answer
from app.engine.baseline import BaselineSystem
from app.llm.client import LLMClient
from app.llm.stub import register_stub
from app.llm.types import LLMMode
from eval.systems import AccountingLLM, EvalCfg, reset_retrieval, retrieval_sink
from tests.integration.test_retrieval_pg import Dataset, _build, half

pytest.importorskip("testcontainers")


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # older testcontainers
        from testcontainers.postgres import PostgresContainer  # type: ignore[no-redef]
    with PostgresContainer("pgvector/pgvector:pg17", driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="module")
def data(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    return _build(pg_url, tmp_path_factory.mktemp("baseline_export"))


class OwnVector:
    def __init__(self, vector: np.ndarray) -> None:
        self.vector = vector

    def embed_queries(self, questions: Sequence[str]) -> np.ndarray:
        return np.stack([self.vector for _ in questions])


def test_default_path_retrieves_exact_top8_and_reads_passages(data: Dataset) -> None:
    seen: list[str] = []

    def compose(**_: object) -> Answer:
        return Answer(answer_markdown="ok")

    register_stub(Answer, compose)
    target = 0
    system = BaselineSystem(embedder=OwnVector(data.vectors[target]))
    cfg = EvalCfg(llm=AccountingLLM(LLMClient(mode=LLMMode.stub)), dsn=data.dsn)
    sink, token = retrieval_sink()
    try:
        system.answer("question", cfg)
        seen = list(sink)
    finally:
        reset_retrieval(token)
    # brute-force cosine on half-precision vectors (the column is halfvec)
    q = half(data.vectors[target])
    scores = half(data.vectors) @ q
    expected = [data.ids[i] for i in np.argsort(-scores, kind="stable")[:8]]
    assert seen[0] == data.ids[target]
    assert len(seen) == 8
    assert seen == expected  # ef_search 800 over a small table is exhaustive


def test_passages_come_from_the_chunks_view(data: Dataset) -> None:
    system = BaselineSystem(embedder=OwnVector(data.vectors[1]))
    fetched = system._default_fetch(data.dsn)(data.ids[:3])
    assert [fetched[i].text for i in data.ids[:3]] == data.texts[:3]


def test_dropped_connection_is_reopened(data: Dataset) -> None:
    register_stub(Answer, lambda **_: Answer(answer_markdown="ok"))
    system = BaselineSystem(embedder=OwnVector(data.vectors[2]))
    cfg = EvalCfg(llm=AccountingLLM(LLMClient(mode=LLMMode.stub)), dsn=data.dsn)
    system.answer("q", cfg)
    system._conn.close()  # the tunnel dropped
    system.answer("q", cfg)
    assert not system._conn.closed
    system.close()
    assert system._conn is None
