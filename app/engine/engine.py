"""The engine: question -> plan -> (numbers || passages) -> composed Answer (M05 section 3.8).

    plan (Haiku) -> attach CIKs by code
      out_of_scope / needs_clarification -> a templated refusal, no composer call
      periods: explicit ones as named; relative wording resolved by periods.py on filed periods
      numeric / hybrid  : compile_plan over `facts`   \\ run in parallel (asyncio.gather)
      narrative / hybrid: retrieve over `chunks`      /
      numeric / hybrid with no catalog metric: nothing to compile, so search the text (ADR-048)
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
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.engine.answer import Answer, Refusal
from app.engine.compose.composer import citable_fact_ids, compose
from app.engine.compose.grounding import Violation, check_grounding, check_markdown
from app.engine.entities.resolve_company import CompanyIndex
from app.engine.plan.compiler import compile_plan
from app.engine.plan.periods import ASK_NO_QUARTER, LatestFiled, resolve_period
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

VERSION = "0.6.0"
SQL_FALLBACK_ENV = "ENGINE_SQL_FALLBACK"
MIN_COMPANIES_FOR_A_FISCAL_YEAR = 100
TRACKED_CIKS_PATH = Path("data/targets/tracked_ciks.csv")
OUT_OF_SCOPE_REASON = (
    "This question is outside what SEC filings can answer (a forecast, a price or a topic that is "
    "not about a listed company's reported results)."
)

# The anchors for relative periods: the newest period in an annual and in a quarterly filing.
# `period_end <= filed` keeps out forward-looking amounts.
_LATEST_ANNUAL = (
    "SELECT fiscal_year, fiscal_period, period_end FROM facts "
    "WHERE cik = %s AND fiscal_period = 'FY' AND qtrs = 4 AND form LIKE '10-K%%' "
    "AND period_end <= filed ORDER BY period_end DESC, fiscal_year DESC LIMIT 1"
)
_LATEST_QUARTERLY = (
    "SELECT fiscal_year, fiscal_period, period_end FROM facts "
    "WHERE cik = %s AND fiscal_period IN ('Q1', 'Q2', 'Q3') AND qtrs = 1 AND form LIKE '10-Q%%' "
    "AND period_end <= filed ORDER BY period_end DESC, fiscal_year DESC LIMIT 1"
)
_PERIOD_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5}
# A year as users type one: 2023, FY23, fy 23, '23. Not part of an amount ($2000, 2,000, 2000.5).
_FULL_YEAR = re.compile(r"(?<![0-9$,.])(?:19|20)[0-9]{2}(?![0-9]|[,.][0-9])")
_SHORT_YEAR = re.compile(
    r"(?:\bfy ?['\u2019]?|(?<![a-z])['\u2019])([0-9]{2})(?![0-9])", re.IGNORECASE
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
    text_only_fallback: bool = False  # no catalog metric: answered from filing text (ADR-048)
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


def period_label(period: PeriodRef) -> str:
    if period.fiscal_period == "FY":
        return f"FY{period.fiscal_year}"
    return f"{period.fiscal_period} FY{period.fiscal_year}"


@dataclass(frozen=True)
class PeriodDecision:
    """The periods to use, what the answer must say about them, or the question to ask."""

    plan: QueryPlan
    note: str | None = None
    clarification: str | None = None


def years_named(text: str) -> set[int]:
    """The calendar-style years written in ``text``; two digits read as the planner reads them."""
    full = {int(y) for y in _FULL_YEAR.findall(text)}
    short = {int(y) for y in _SHORT_YEAR.findall(text)}
    return full | {1900 + y if y >= 93 else 2000 + y for y in short}


def uncovered_years(question: str, plan: QueryPlan) -> list[int]:
    """Years the question names that nothing in the plan accounts for: its periods, the implied
    base of a one-period growth rate, or a year inside ``period_text``."""
    covered = {p.fiscal_year for p in plan.periods}
    if plan.operation == "growth" and len(covered) == 1:
        covered |= {y - 1 for y in covered}
    covered |= years_named(plan.period_text or "")
    return sorted(years_named(question) - covered)


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
        from eval.systems import record_compiled_facts, record_retrieval, record_route

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

        asks_numbers = plan.intent in ("numeric", "hybrid")
        # No catalog key means what was asked: nothing to compile, so quote the filings instead.
        # A mapped metric the compiler abstains on does NOT come here: text must not route
        # around an abstention.
        diag.text_only_fallback = asks_numbers and not plan.metrics
        want_numbers = asks_numbers and bool(plan.metrics)
        want_text = plan.intent in ("narrative", "hybrid") or diag.text_only_fallback

        decision = self._resolve_periods(question, plan, cfg.dsn, want_numbers=want_numbers)
        if decision.clarification:
            return refusal_answer(decision.clarification, missing=["a fiscal period to answer for"])
        plan, diag.assumed_period = decision.plan, decision.note
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
        record_compiled_facts(citable_fact_ids(diag.results))
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
                text_only=diag.text_only_fallback,
                role=cfg.composer,
            )
            record_usage(span, usage)
            diag.timings_ms["compose"] = (time.perf_counter() - t0) * 1000
        diag.composed = True
        by_id = {c.chunk_id: c.text for c in chunks}
        years = {p.fiscal_year for p in plan.periods}
        diag.violations = check_grounding(answer, diag.results, by_id, years) + check_markdown(
            answer, diag.results, by_id, years
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

    # --- periods -------------------------------------------------------------------------

    def _resolve_periods(
        self, question: str, plan: QueryPlan, dsn: str | None, *, want_numbers: bool
    ) -> PeriodDecision:
        """05 4.1 / ADR-048. Named periods stand. Relative wording is resolved by ``periods.py``
        against what each company has filed; numbers are never computed for a guessed period."""
        wording = (plan.period_text or "").strip()
        if want_numbers:  # whatever the plan did, never answer for fewer or other years
            dropped = uncovered_years(question, plan)
            if dropped:
                named = ", ".join(str(y) for y in dropped)
                also = f" and also says '{wording}'" if wording else ""
                return PeriodDecision(
                    plan,
                    clarification=(
                        f"The question names {named}{also}, which I could not place in the "
                        "periods to compute. Which fiscal periods do you mean?"
                    ),
                )
        if not wording and (plan.periods or not want_numbers):
            return PeriodDecision(plan)  # named periods, or a text search over all years
        anchors = self._anchors(plan, dsn)
        if not anchors:
            if wording and want_numbers:  # cannot check the wording, so do not compute
                return PeriodDecision(
                    plan,
                    clarification=(
                        f"I could not find the latest filings needed to read '{wording}'. Which "
                        "fiscal period do you mean (for example FY2023 or Q3 2022)?"
                    ),
                )
            return PeriodDecision(plan)  # nothing filed: the compiler reports what is missing
        resolutions = [resolve_period(wording or None, latest) for latest in anchors]
        questions = [r.clarification for r in resolutions if r.clarification]
        if questions:
            if not want_numbers:
                return PeriodDecision(plan)
            ask = questions[0]
            if ask == ASK_NO_QUARTER and not any(c.cik is not None for c in plan.companies):
                # the all-companies anchor looks up no quarter, so do not claim there is none
                ask = (
                    "A relative quarter cannot be dated when ranking all tracked companies. "
                    "Please name the quarter (for example Q3 2022)."
                )
            return PeriodDecision(plan, clarification=ask)
        found = [r.periods[0] for r in resolutions]
        # The earliest of them: the latest period every named company has filed.
        chosen = min(found, key=lambda p: (p.fiscal_year, _PERIOD_ORDER[p.fiscal_period]))
        if plan.periods:
            # Beside named periods, wording is accepted only when it restates one of them
            # ("fiscal 2023"). "FY2023 and the year before" must not collapse into one year.
            restates = chosen in plan.periods and str(chosen.fiscal_year) in wording
            if restates or not want_numbers:
                return PeriodDecision(plan)
            named = ", ".join(period_label(p) for p in plan.periods)
            return PeriodDecision(
                plan,
                clarification=(
                    f"I read '{wording}' as {period_label(chosen)}, but the question also names "
                    f"{named}. Which periods do you mean?"
                ),
            )
        differ = (
            " The companies' latest filed periods differ; this is the latest one all of them "
            "have filed."
            if any(p != found[0] for p in found)
            else ""
        )
        if wording:
            note = (
                f"'{wording}' was read as {period_label(chosen)}, from the latest filings on "
                f"record.{differ} Say which fiscal period the figures are for."
            )
        elif any(c.cik is not None for c in plan.companies):
            note = (
                "The question named no period; the latest fiscal year filed by every company "
                f"asked about ({period_label(chosen)}) is used. Say so in the answer."
            )
        else:
            note = (
                "The question named no period; the latest fiscal year most companies have filed "
                f"({period_label(chosen)}) is used. Say so in the answer."
            )
        return PeriodDecision(plan.model_copy(update={"periods": [chosen]}), note)

    def _anchors(self, plan: QueryPlan, dsn: str | None) -> list[LatestFiled]:
        """One ``LatestFiled`` per named company; with none named (a rank over the tracked set),
        the latest fiscal year most companies have filed, not one early filer's. Empty when a
        company has no annual filing in the facts."""
        ciks = sorted({c.cik for c in plan.companies if c.cik is not None})
        conn = self._conn("meta", dsn)
        if not ciks:
            row = conn.execute(
                "SELECT fiscal_year FROM facts WHERE fiscal_period = 'FY' GROUP BY fiscal_year "
                "HAVING count(DISTINCT cik) >= %s ORDER BY fiscal_year DESC LIMIT 1",
                [MIN_COMPANIES_FOR_A_FISCAL_YEAR],
            ).fetchone()
            if row is None or row[0] is None:
                return []
            year = int(row[0])
            return [LatestFiled(fiscal_year=year, latest=PeriodRef(fiscal_year=year))]
        anchors = []
        for cik in ciks:
            annual = conn.execute(_LATEST_ANNUAL, [cik]).fetchone()
            if annual is None:
                return []
            quarter = conn.execute(_LATEST_QUARTERLY, [cik]).fetchone()
            newest = quarter if quarter is not None and quarter[2] > annual[2] else annual
            anchors.append(
                LatestFiled(
                    fiscal_year=int(annual[0]),
                    latest=PeriodRef(fiscal_year=int(newest[0]), fiscal_period=newest[1]),
                )
            )
        return anchors


def _default_connect(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn, autocommit=True)
