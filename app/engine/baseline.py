"""Naive-RAG baseline (M04 3.9): the number the engine has to beat.

question -> pinned bge query embedding (quantized, with the BGE instruction: the served query
path, ADR-032/033) -> dense top-8 over ALL chunks (no filters) at a pinned ef_search (800,
ADR-040) -> Sonnet composer writing the same ``Answer`` schema as the engine -> no verifier.

It is deliberately naive: no planner, no XBRL facts, no hybrid retrieval, no reranker, no
verification. It returns the engine's ``Answer`` so both are scored by the same metrics and
judges (M04 pitfall). Claims may only cite chunks (it has no fact ids to put in an ``expr``).

Retrieval and passage lookup are injectable (``retrieve``, ``fetch_passages``) so unit tests run
without a database; the defaults query the serving Postgres ``chunks`` view through
``app.engine.retrieval.dense_search``. The embedder is created lazily from the sha-pinned
local model directory, so importing this module loads no model.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from app.engine.answer import Answer, Refusal
from app.engine.retrieval.types import ChunkHit

if TYPE_CHECKING:
    from eval.systems import EvalCfg

VERSION = "0.2.0"  # 0.2.0: numeric claims must carry a unit from NUMERIC_UNITS, full precision

# The unit labels a numeric claim may carry. eval.metrics.normalize_value reads exactly these
# (the test that ties the two together is tests/unit/eval/test_metrics.py, which lands with
# eval/metrics.py); the prompt below is built from it.
NUMERIC_UNITS: tuple[str, ...] = (
    "USD",
    "USD thousands",
    "USD millions",
    "USD billions",
    "USD/shares",
    "shares",
    "shares thousands",
    "shares millions",
    "percent",
    "ratio",
)
TOP_K = 8
EF_SEARCH = 800
MODEL_DIR_ENV = "EDGARQA_EMBED_MODEL_DIR"
DEFAULT_MODEL_DIR = Path("data/models/bge-small-en-v1.5")
MAX_ANSWER_TOKENS = 2048

COMPOSER_SYSTEM = """You answer questions about SEC filings (10-K, 10-Q) using ONLY the numbered
passages provided with each question. You are a baseline system: answer as well as the
passages allow and never use outside knowledge.

Return JSON matching the schema:
- answer_markdown: a concise answer. Cite passages inline as [n], using the passage numbers.
- claims: one entry per factual statement in the answer.
  - kind: "numeric" for a number stated in a passage, "numeric_derived" for a number you
    derived from passage numbers (a growth rate, margin, ratio or difference), otherwise "text".
  - text: the statement, self-contained.
  - for numeric claims: value (a plain number, no commas or units), unit, display (as written
    in the answer, e.g. "$1.58 billion"). unit is REQUIRED on every numeric claim and must
    describe the value field exactly, using one of: @UNITS@.
    value keeps every digit the passage gives; rounding belongs only in display and
    answer_markdown. Example: a passage figure of 1,583 (USD millions) with display
    "$1.58 billion" has value 1583 and unit "USD millions" (not value 1.58). A margin or
    growth rate of 29.8% is value 29.8 with unit "percent", or value 0.298 with unit "ratio".
  - primary: true for the single claim that directly answers the question, false otherwise.
  - provenance: {"type": "chunk", "chunk_id": <the passage's chunk_id>, "quote": <an EXACT,
    contiguous substring copied from that passage that supports the claim>}; for a derived
    number, quote the passage holding its main input. Never use {"type": "expr"}: you have no
    fact ids.
- refusal: null when the passages support an answer. When they don't, set
  {"reason": <why>, "found": [<what the passages do say, if anything relevant>],
   "missing": [<what would be needed>]} and leave claims empty.

Rules: base every number on numbers in the passages; do not guess periods or companies the
passages don't name."""
COMPOSER_SYSTEM = COMPOSER_SYSTEM.replace("@UNITS@", ", ".join(NUMERIC_UNITS))

# A naive-RAG comparator derives numbers the way a plain LLM would. Banning that would make the
# baseline refuse most growth/ratio/compare questions by construction and inflate the engine's
# lead (M04 3.3: 180 of 300 numeric golds are computed). The strict variant is kept for the
# record, behind a flag, never the default.
STRICT_NO_ARITHMETIC_RULE = """
Strict variant: every number you state must appear verbatim in a quoted passage; do not compute
new numbers; use kind "numeric" or "text" only."""


class BaselineOutputError(ValueError):
    """The composer returned something the baseline cannot have produced honestly."""


class QueryEmbedder(Protocol):
    def embed_queries(self, questions: Sequence[str]) -> Any: ...


@dataclass(frozen=True)
class Passage:
    chunk_id: str
    text: str
    form: str | None = None
    fiscal_year: int | None = None
    section: str | None = None
    doc_source: str | None = None


Retrieve = Callable[[Sequence[float], int, int], list[ChunkHit]]
FetchPassages = Callable[[list[str]], dict[str, Passage]]


def format_passages(passages: list[Passage]) -> str:
    blocks = []
    for n, p in enumerate(passages, start=1):
        meta = ", ".join(
            str(x)
            for x in (
                p.form,
                f"FY{p.fiscal_year}" if p.fiscal_year else None,
                f"section {p.section}" if p.section else None,
                p.doc_source,
            )
            if x
        )
        blocks.append(f"[{n}] chunk_id={p.chunk_id} ({meta})\n{p.text}")
    return "\n\n".join(blocks)


def build_messages(question: str, passages: list[Passage]) -> list[dict[str, Any]]:
    """Passages then the question, in one user turn after the cached static system prompt."""
    return [
        {
            "role": "user",
            "content": f"Passages:\n\n{format_passages(passages)}\n\nQuestion: {question}",
        }
    ]


class BaselineSystem:
    name = "baseline"
    version = VERSION

    def __init__(
        self,
        *,
        embedder: QueryEmbedder | None = None,
        retrieve: Retrieve | None = None,
        fetch_passages: FetchPassages | None = None,
        model_dir: Path | None = None,
        strict_no_arithmetic: bool = False,
    ) -> None:
        self._embedder = embedder
        self._system_prompt = COMPOSER_SYSTEM + (
            STRICT_NO_ARITHMETIC_RULE if strict_no_arithmetic else ""
        )
        self._dsn: str | None = None
        self._retrieve = retrieve
        self._fetch = fetch_passages
        self._model_dir = model_dir or Path(os.environ.get(MODEL_DIR_ENV, DEFAULT_MODEL_DIR))
        self._conn: Any = None

    @property
    def embedder_name(self) -> str:
        from pipelines.common.embedding_artifact import ARTIFACT_HF_REPO, ARTIFACT_ONNX_FILE

        return f"{ARTIFACT_HF_REPO}/{ARTIFACT_ONNX_FILE} (quantized) + BGE query instruction"

    # --- collaborators (lazy defaults) ---------------------------------------------------

    def _get_embedder(self) -> QueryEmbedder:
        if self._embedder is None:
            from pipelines.common.embedding import ARTIFACT_ONNX_SHA256, get_embedder

            self._embedder = get_embedder(
                self._model_dir, threads=2, expected_onnx_sha256=ARTIFACT_ONNX_SHA256
            )
        return self._embedder

    def _connection(self, dsn: str | None) -> Any:
        """One autocommit connection, reopened when it was closed (a dropped tunnel) or the DSN
        changed; a failure is raised to the harness, which records the question as errored."""
        if not dsn:
            raise RuntimeError("baseline needs the serving DSN (EDGARQA_SERVING_DSN / --dsn)")
        if self._conn is not None and (self._conn.closed or dsn != self._dsn):
            self.close()
        if self._conn is None:
            import psycopg

            self._conn = psycopg.connect(dsn, autocommit=True)
            self._dsn = dsn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def _default_retrieve(self, dsn: str | None) -> Retrieve:
        from app.engine.retrieval.dense import dense_search

        def run(vector: Sequence[float], k: int, ef_search: int) -> list[ChunkHit]:
            return dense_search(self._connection(dsn), vector, limit=k, ef_search=ef_search)

        return run

    def _default_fetch(self, dsn: str | None) -> FetchPassages:
        def run(ids: list[str]) -> dict[str, Passage]:
            rows = (
                self._connection(dsn)
                .execute(
                    "SELECT chunk_id, text, form, fiscal_year, section, doc_source "
                    "FROM chunks WHERE chunk_id = ANY(%s)",
                    [ids],
                )
                .fetchall()
            )
            return {r[0]: Passage(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows}

        return run

    # --- the system --------------------------------------------------------------------

    def answer(self, question: str, cfg: EvalCfg) -> Answer:
        from eval.systems import record_retrieval, record_route

        record_route("dense")
        vector = [float(x) for x in self._get_embedder().embed_queries([question])[0]]
        retrieve = self._retrieve or self._default_retrieve(cfg.dsn)
        hits = retrieve(vector, cfg.top_k, cfg.ef_search)
        ids = [h.chunk_id for h in sorted(hits, key=lambda h: h.rank)]
        record_retrieval(ids)
        if not ids:  # an empty or swapped-out chunks view: don't pay the composer for nothing
            return Answer(
                answer_markdown="No passages were retrieved.",
                refusal=Refusal(reason="retrieval returned no passages"),
            )
        fetched = (self._fetch or self._default_fetch(cfg.dsn))(ids)
        missing = [i for i in ids if i not in fetched]
        if missing:
            raise LookupError(f"retrieved chunks missing from the chunks view: {missing}")
        passages = [fetched[i] for i in ids]
        answer, _ = cfg.llm.structured(
            cfg.composer,
            self._system_prompt,
            build_messages(question, passages),
            Answer,
            max_tokens=MAX_ANSWER_TOKENS,
        )
        result: Answer = answer
        invented = [c.id for c in result.claims if c.provenance.type == "expr"]
        if invented:
            # The baseline has no fact ids, so any expr provenance is invented.
            raise BaselineOutputError(f"claims {invented} cite fact ids the baseline never saw")
        return result
