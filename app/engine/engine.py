"""The engine: question -> plan -> (numbers || passages) -> composed Answer (M05 section 3.8).

    plan (Haiku) -> attach CIKs by code
      out_of_scope / needs_clarification -> a templated refusal, no composer call
      numeric / hybrid  : compile_plan over `facts`   \\ run in parallel (asyncio.gather)
      narrative / hybrid: retrieve over `chunks`      /
    -> compose (Sonnet or Haiku) -> grounding check -> verify (pass-through until M6)

The LLM never writes SQL here and never does arithmetic: numbers come from the compiler with
provenance, text from retrieval. Every collaborator is injectable, so the orchestration is tested
without a database or a model. Batch mode works because ``answer`` is re-entrant: a stage that needs
a model call not yet in the cassettes raises ``BatchPending`` and the harness re-runs the question
after the round (plan, then compose: two rounds).
"""

from __future__ import annotations

import asyncio
import csv
import os
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.engine.answer import Answer, Refusal
from app.engine.compose.composer import compose
from app.engine.compose.grounding import Violation, check_grounding, check_markdown
from app.engine.entities.resolve_company import CompanyIndex
from app.engine.plan.compiler import compile_plan
from app.engine.plan.planner import attach_ciks, build_system_prompt, plan_question
from app.engine.plan.schema import (
    CompileOutcome,
    NotFound,
    NumericResult,
    PeriodRef,
    QueryPlan,
    UnsupportedPlan,
)
from app.engine.retrieval.query import RetrievalConfig, RetrievalResult, retrieve
from app.engine.retrieval.rerank import CrossEncoderReranker, Reranker
from app.engine.telemetry import record_usage, stage

VERSION = "0.5.0"
SQL_FALLBACK_ENV = "ENGINE_SQL_FALLBACK"
MIN_COMPANIES_FOR_A_FISCAL_YEAR = 100
TRACKED_CIKS_PATH = Path("data/targets/tracked_ciks.csv")
OUT_OF_SCOPE_REASON = (
    "This question is outside what SEC filings can answer (a forecast, a price or a topic that is "
    "not about a listed company's reported results)."
)

CompileFn = Callable[..., CompileOutcome]
RetrieveFn = Callable[..., RetrievalResult]


@dataclass
class Diagnostics:
    """What happened to one question; the harness and the M6 verifier read it."""

    plan: QueryPlan | None = None
    assumed_period: str | None = None
    results: list[NumericResult] = field(default_factory=list)
    not_found: list[NotFound] = field(default_factory=list)
    unsupported: str | None = None
    retrieval: RetrievalResult | None = None
    violations: list[Violation] = field(default_factory=list)
    composed: bool = False
    timings_ms: dict[str, float] = field(default_factory=dict)


def load_tracked_ciks(path: Path = TRACKED_CIKS_PATH) -> list[int]:
    with path.open(newline="") as fh:
        return sorted({int(r["cik"]) for r in csv.DictReader(fh)})


def refusal_answer(reason: str, found: Sequence[str] = (), missing: Sequence[str] = ()) -> Answer:
    return Answer(
        answer_markdown=reason,
        claims=[],
        refusal=Refusal(reason=reason, found=list(found), missing=list(missing)),
    )


def describe_missing(not_found: Sequence[NotFound], names: Mapping[int, str]) -> list[str]:
    out = []
    for n in not_found:
        who = names.get(n.cik, f"CIK {n.cik}")
        when = (
            f"FY{n.fiscal_year}"
            if n.fiscal_period == "FY"
            else f"{n.fiscal_period} FY{n.fiscal_year}"
        )
        out.append(f"{n.metric} for {who}, {when} ({n.reason})")
    return out


class EngineSystem:
    """The eval-harness ``System`` for the engine (`eval/systems.py`)."""

    name = "engine"
    version = VERSION
    reads_corpus = True

    def __init__(
        self,
        *,
        index: CompanyIndex | None = None,
        embedder: Any = None,
        reranker: Reranker | None = None,
        retrieval_cfg: RetrievalConfig | None = None,
        compile_fn: CompileFn = compile_plan,
        retrieve_fn: RetrieveFn = retrieve,
        connect: Callable[[str], Any] | None = None,
        tracked_ciks: Collection[int] | None = None,
        model_dir: Path | None = None,
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._reranker = reranker
        self.retrieval_cfg = retrieval_cfg or RetrievalConfig()
        self._compile = compile_fn
        self._retrieve = retrieve_fn
        self._connect = connect or _default_connect
        self._tracked = tracked_ciks
        self._model_dir = model_dir
        self._conns: dict[str, Any] = {}
        self._system: str | None = None
        self.diagnostics: dict[str, Diagnostics] = {}

    # --- collaborators -------------------------------------------------------------------

    def _conn(self, slot: str, dsn: str | None) -> Any:
        """One connection per slot ("numeric", "retrieval", "meta"): a psycopg connection is not
        shared across threads, and the two branches run in parallel."""
        if not dsn:
            raise RuntimeError("the engine needs the serving DSN (EDGARQA_SERVING_DSN / --dsn)")
        conn = self._conns.get(slot)
        if conn is None or getattr(conn, "closed", False):
            conn = self._connect(dsn)
            self._conns[slot] = conn
        return conn

    def close(self) -> None:
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing a dead tunnel must not mask a result
                pass
        self._conns.clear()

    def _company_index(self, dsn: str | None) -> CompanyIndex:
        if self._index is None:
            tracked = self._tracked if self._tracked is not None else load_tracked_ciks()
            self._index = CompanyIndex.from_connection(self._conn("meta", dsn), sorted(tracked))
        return self._index

    def _get_embedder(self) -> Any:
        if self._embedder is None:
            from pipelines.common.embedding import ARTIFACT_ONNX_SHA256, get_embedder

            model_dir = self._model_dir or Path(os.environ.get("EDGARQA_MODEL_DIR", "data/models"))
            self._embedder = get_embedder(
                model_dir, threads=2, expected_onnx_sha256=ARTIFACT_ONNX_SHA256
            )
        return self._embedder

    def _get_reranker(self) -> Reranker:
        if self._reranker is None:
            self._reranker = CrossEncoderReranker()
        return self._reranker

    @property
    def embedder_name(self) -> str:
        from pipelines.common.embedding_artifact import ARTIFACT_HF_REPO, ARTIFACT_ONNX_FILE

        return f"{ARTIFACT_HF_REPO}/{ARTIFACT_ONNX_FILE} (quantized) + BGE query instruction"

    # --- the system ----------------------------------------------------------------------

    def answer(self, question: str, cfg: Any) -> Answer:
        return asyncio.run(self.aanswer(question, cfg))

    async def aanswer(self, question: str, cfg: Any) -> Answer:
        from eval.systems import record_retrieval, record_route

        diag = Diagnostics()
        self.diagnostics[question] = diag
        t_all = time.perf_counter()

        with stage("plan") as span:
            if self._system is None:
                self._system = build_system_prompt()
            t0 = time.perf_counter()
            plan, usage = plan_question(question, cfg.llm, system=self._system)
            record_usage(span, usage)
            plan = attach_ciks(plan, self._company_index(cfg.dsn))
            diag.timings_ms["plan"] = (time.perf_counter() - t0) * 1000
        diag.plan = plan
        record_route(plan.intent)

        names = {cik: r.name for cik, r in self._company_index(cfg.dsn).records.items()}
        if plan.intent == "out_of_scope":
            return refusal_answer(OUT_OF_SCOPE_REASON)
        if plan.needs_clarification:
            return refusal_answer(
                plan.needs_clarification, missing=["a company, metric or period to answer for"]
            )

        want_numbers = plan.intent in ("numeric", "hybrid")
        want_text = plan.intent in ("narrative", "hybrid")
        if want_numbers:
            plan, diag.assumed_period = self._default_period(plan, cfg.dsn)
            diag.plan = plan

        async def numbers() -> CompileOutcome | None:
            if not want_numbers:
                return None
            t = time.perf_counter()
            try:
                with stage("compile"):
                    return await asyncio.to_thread(
                        self._compile,
                        plan,
                        self._conn("numeric", cfg.dsn),
                        universe=self._universe_for(plan),
                    )
            except UnsupportedPlan as err:
                diag.unsupported = err.reason
                return None
            finally:
                diag.timings_ms["compile"] = (time.perf_counter() - t) * 1000

        async def passages() -> RetrievalResult | None:
            if not want_text:
                return None
            with stage("retrieve"):
                return await asyncio.to_thread(
                    self._retrieve,
                    self._conn("retrieval", cfg.dsn),
                    plan,
                    question,
                    embedder=self._get_embedder() if self.retrieval_cfg.dense else None,
                    reranker=self._get_reranker() if self.retrieval_cfg.rerank else None,
                    cfg=replace(self.retrieval_cfg, top_k=cfg.top_k, ef_search=cfg.ef_search),
                )

        outcome, retrieved = await asyncio.gather(numbers(), passages())
        diag.timings_ms["retrieve"] = sum(
            (retrieved.timings_ms.values() if retrieved else ()), start=0.0
        )
        if outcome is not None:
            diag.results, diag.not_found = list(outcome.results), list(outcome.not_found)
        diag.retrieval = retrieved
        chunks = list(retrieved.chunks) if retrieved else []
        record_retrieval([c.chunk_id for c in chunks])

        if not diag.results and not chunks:
            missing = describe_missing(diag.not_found, names)
            if diag.unsupported:
                missing.append(f"a supported way to compute this ({diag.unsupported})")
            if want_text and not chunks:
                missing.append("passages in the filings that address the question")
            return refusal_answer(
                "The filings in the corpus do not support an answer to this question.",
                missing=missing or ["supporting data"],
            )

        with stage("compose") as span:
            t0 = time.perf_counter()
            answer, usage = compose(
                question,
                plan,
                diag.results,
                diag.not_found,
                chunks,
                names,
                cfg.llm,
                assumed_period=diag.assumed_period,
                unsupported=diag.unsupported,
                role=cfg.composer,
            )
            record_usage(span, usage)
            diag.timings_ms["compose"] = (time.perf_counter() - t0) * 1000
        diag.composed = True
        by_id = {c.chunk_id: c.text for c in chunks}
        diag.violations = check_grounding(answer, diag.results, by_id) + check_markdown(
            answer, diag.results, by_id
        )
        with stage("verify") as span:  # pass-through until M6 (the verifier fills this slot)
            span.set_attribute("verifier", "off")
        diag.timings_ms["total"] = (time.perf_counter() - t_all) * 1000
        return answer

    def _universe_for(self, plan: QueryPlan) -> list[int] | None:
        """The tracked CIKs a ``rank`` ranges over (read once from the tracked-CIK file); every
        other operation ignores the universe."""
        if plan.operation != "rank":
            return None
        if self._tracked is None:
            self._tracked = load_tracked_ciks()
        return sorted(self._tracked)

    def _default_period(self, plan: QueryPlan, dsn: str | None) -> tuple[QueryPlan, str | None]:
        """No period named: use the latest fiscal year every named company has filed, and say so
        (M05 section 3.2). Relative phrases ("last year") are not in QueryPlan yet, so they land
        here too (ADR-046 open question). Shekar's ``resolve_period`` replaces this once written."""
        if plan.periods:
            return plan, None
        ciks = sorted({c.cik for c in plan.companies if c.cik is not None})
        conn = self._conn("meta", dsn)
        if not ciks:
            # No company named (a rank over the tracked set): the latest fiscal year most
            # companies have filed, not one early filer's.
            row = conn.execute(
                "SELECT fiscal_year FROM facts WHERE fiscal_period = 'FY' GROUP BY fiscal_year "
                "HAVING count(DISTINCT cik) >= %s ORDER BY fiscal_year DESC LIMIT 1",
                [MIN_COMPANIES_FOR_A_FISCAL_YEAR],
            ).fetchone()
            if row is None or row[0] is None:
                return plan, None
            fy = int(row[0])
            note = (
                f"The question named no period; the latest fiscal year most companies have filed "
                f"(FY{fy}) is used. Say so in the answer."
            )
            return plan.model_copy(update={"periods": [PeriodRef(fiscal_year=fy)]}), note
        years = []
        for cik in ciks:
            row = conn.execute(
                "SELECT max(fiscal_year) FROM facts WHERE cik = %s AND fiscal_period = 'FY'", [cik]
            ).fetchone()
            if row is None or row[0] is None:
                return plan, None  # nothing filed: the compiler reports NotFound
            years.append(int(row[0]))
        fy = min(years)
        note = (
            f"The question named no period; the latest fiscal year filed by every company "
            f"asked about (FY{fy}) is used. Say so in the answer."
        )
        return plan.model_copy(update={"periods": [PeriodRef(fiscal_year=fy)]}), note


def _default_connect(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn, autocommit=True)
