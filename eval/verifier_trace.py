"""Replay a recorded engine run and keep what its results file does not (M6 Gate 0, ADR-049).

    uv run python -m eval.verifier_trace --source eval/results/<run_id>.json

A results file stores no claims and no evidence. This replays the same questions through the
engine against the serving DB with the LLM client in **replay** mode, which cannot call the API:
a request without a cassette is an error, never a spend. For each question it keeps the answer,
the engine's diagnostics, and the facts and passages behind the claims.

The capture is accepted only if it reproduces the recorded run (answer text, refusal, retrieved
ids, route, claim count, corpus version, no errors). A rejected capture is written beside the
target as ``*.rejected.json`` and the command exits 2.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Collection, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.engine.answer import Answer, ChunkProvenance, ExprProvenance
from app.engine.plan.schema import NotFound, NumericResult, QueryPlan
from app.llm.client import LLMClient
from app.llm.types import LLMMode
from eval.systems import (
    AccountingLLM,
    EvalCfg,
    compiled_facts_sink,
    reset_compiled_facts,
    reset_retrieval,
    reset_route,
    retrieval_sink,
    route_sink_set,
)

DEFAULT_OUT = Path("eval/results/verifier/gate0_dev_trace.json")
WRITER = "claude-haiku-5-5"  # the writer ADR-049 measures


class FactRecord(BaseModel):
    """One row of the serving ``facts`` view, every column (serving/sql/templates/facts.sql)."""

    fact_id: int
    fact_key: int
    cik: int
    tag: str
    taxonomy: str
    uom: str
    qtrs: int
    period_start: date | None = None
    period_end: date
    fiscal_year: int
    fiscal_period: str
    calendar_frame: str | None = None
    value: Decimal
    accession: str
    form: str
    filed: date
    is_latest: bool
    is_original: bool
    is_restated: bool
    anomaly_score: float | None = None
    derived_from: list[int] | None = None


class ChunkRecord(BaseModel):
    """One row of the serving ``chunks`` view without ``tsv`` and ``embedding``."""

    chunk_id: str
    cik: int | None = None
    accession: str | None = None
    form: str | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    section: str | None = None
    section_title: str | None = None
    chunk_index: int | None = None
    text: str
    n_tokens: int | None = None
    is_table: bool | None = None
    source_url: str | None = None
    anchor: str | None = None
    page: int | None = None
    doc_source: str | None = None
    tenant_id: int | None = None


_FACT_COLUMNS = tuple(FactRecord.model_fields)
_CHUNK_COLUMNS = tuple(ChunkRecord.model_fields)
_SELECT_FACTS = f"SELECT {', '.join(_FACT_COLUMNS)} FROM facts WHERE fact_id = ANY(%s)"
_SELECT_CHUNKS = f"SELECT {', '.join(_CHUNK_COLUMNS)} FROM chunks WHERE chunk_id = ANY(%s)"
_SELECT_NAMES = "SELECT cik, name FROM companies WHERE cik = ANY(%s)"


class TraceRow(BaseModel):
    id: str
    set: str
    split: str | None = None
    question: str
    answer: Answer | None = None
    error: str | None = None
    route: str = "n/a"
    retrieved_top20: list[str] = []
    shown_chunk_ids: list[str] = []  # the passages the composer was given
    plan: QueryPlan | None = None
    results: list[NumericResult] = []
    not_found: list[NotFound] = []
    assumed_period: str | None = None
    unsupported: str | None = None
    text_only_fallback: bool = False
    composed: bool = False
    llm_tokens: list[tuple[int, int]] = []  # (input, output) of each replayed call, in order


class Acceptance(BaseModel):
    passed: bool
    mismatches: list[str] = []


class TraceMeta(BaseModel):
    source_results: str
    source_run_id: str
    source_git_sha: str
    capture_git_sha: str
    captured_at: str
    split: str
    composer: str
    ef_search: int
    top_k: int
    corpus_version: int | None
    acceptance: Acceptance


class Trace(BaseModel):
    meta: TraceMeta
    rows: list[TraceRow]
    facts: dict[int, FactRecord] = {}
    chunks: dict[str, ChunkRecord] = {}
    company_names: dict[int, str] = {}


# --- capture ---------------------------------------------------------------------------------


def capture_rows(system: Any, cfg: Any, items: Sequence[tuple[str, Any, str | None]]) -> list[Any]:
    """Answer each ``(set, gold row, split)`` and keep the answer with the engine's diagnostics.
    Diagnostics are keyed by question text, so they are read right after each call."""
    out: list[TraceRow] = []
    for set_name, row, split in items:
        retrieved, r_token = retrieval_sink()
        route, route_token = route_sink_set()
        _, c_token = compiled_facts_sink()
        system.diagnostics.pop(row.question, None)  # never read an earlier row's
        record = TraceRow(id=row.id, set=set_name, split=split, question=row.question)
        ledger = cfg.llm.ledger if cfg is not None else []
        mark = len(ledger)
        try:
            record.answer = system.answer(row.question, cfg)
        except Exception as err:  # noqa: BLE001 - recorded; the acceptance check rejects it
            record.error = f"{type(err).__name__}: {err}"
        finally:
            record.retrieved_top20 = list(retrieved)[:20]
            record.route = route.get("route", "n/a")
            record.llm_tokens = [(u.input_tokens, u.output_tokens) for u in ledger[mark:]]
            reset_retrieval(r_token)
            reset_route(route_token)
            reset_compiled_facts(c_token)
        diag = system.diagnostics.get(row.question)
        if diag is not None:
            record.plan = diag.plan
            record.results = list(diag.results)
            record.not_found = list(diag.not_found)
            record.assumed_period = diag.assumed_period
            record.unsupported = diag.unsupported
            record.text_only_fallback = diag.text_only_fallback
            record.composed = diag.composed
            if diag.retrieval is not None:
                record.shown_chunk_ids = [c.chunk_id for c in diag.retrieval.chunks]
        out.append(record)
    return out


def cited_ids(rows: Sequence[TraceRow]) -> tuple[set[int], set[str]]:
    """Fact ids cited by a claim or compiled for it (with restated latest ids), and cited chunks."""
    facts: set[int] = set()
    chunks: set[str] = set()
    for row in rows:
        for r in row.results:
            facts.update(r.fact_ids)
            facts.update(n.latest_fact_id for n in r.notes if n.latest_fact_id is not None)
        for claim in row.answer.claims if row.answer else []:
            p = claim.provenance
            if isinstance(p, ExprProvenance):
                facts.update(p.fact_ids)
            elif isinstance(p, ChunkProvenance):
                chunks.add(p.chunk_id)
    return facts, chunks


def _records(conn: Any, sql: str, columns: Sequence[str], ids: Collection[Any]) -> list[dict]:
    if not ids:
        return []
    cursor = conn.execute(sql, [sorted(ids)])
    return [dict(zip(columns, r, strict=True)) for r in cursor.fetchall()]


def fetch_facts(conn: Any, fact_ids: Collection[int]) -> dict[int, FactRecord]:
    """The facts and, for derived ones, their ``derived_from`` parents. A missing id is absent."""
    out: dict[int, FactRecord] = {}
    wanted = set(fact_ids)
    while wanted:
        found = [
            FactRecord.model_validate(r)
            for r in _records(conn, _SELECT_FACTS, _FACT_COLUMNS, wanted)
        ]
        out.update({f.fact_id: f for f in found})
        parents = {p for f in found for p in f.derived_from or []}
        wanted = parents - out.keys()
    return out


def fetch_chunks(conn: Any, chunk_ids: Collection[str]) -> dict[str, ChunkRecord]:
    rows = _records(conn, _SELECT_CHUNKS, _CHUNK_COLUMNS, chunk_ids)
    return {r["chunk_id"]: ChunkRecord.model_validate(r) for r in rows}


def fetch_company_names(conn: Any, ciks: Collection[int]) -> dict[int, str]:
    return {r["cik"]: r["name"] for r in _records(conn, _SELECT_NAMES, ("cik", "name"), set(ciks))}


# --- acceptance ------------------------------------------------------------------------------

# A batch run adds a row's usage once per round it takes: the planner call is counted again in
# the round that serves the composer.
PLANNER_ROUNDS = (1, 2)


def same_tokens(row: TraceRow, want: dict[str, Any]) -> bool:
    """Whether the replayed calls account for the recorded run's token counts. Two cassettes can
    give the same ``answer_markdown`` with different claims; their token counts differ."""
    calls = row.llm_tokens
    if len(calls) != (2 if row.composed else 1):
        return False
    planner, composer = calls[0], calls[1] if row.composed else (0, 0)
    recorded = (want.get("input_tokens"), want.get("output_tokens"))
    return any(
        (k * planner[0] + composer[0], k * planner[1] + composer[1]) == recorded
        for k in PLANNER_ROUNDS
    )


def compare_with_source(
    rows: Sequence[TraceRow], source: dict[str, Any], corpus_version: int | None
) -> list[str]:
    """How the capture differs from the recorded run. Empty means the same answers."""
    out: list[str] = []
    if corpus_version != source["corpus_version"]:
        out.append(f"corpus_version {corpus_version} != {source['corpus_version']}")
    recorded = {q["id"]: q for q in source["per_question"]}
    got = [r.id for r in rows]
    if sorted(got) != sorted(recorded):
        out.append(
            f"question ids differ: {len(set(recorded) - set(got))} missing, "
            f"{len(set(got) - set(recorded))} extra, {len(got) - len(set(got))} repeated"
        )
    for row in rows:
        want = recorded.get(row.id)
        if want is None:
            continue
        if row.error is not None or row.answer is None:
            out.append(f"{row.id}: error: {row.error}")
            continue
        refusal = row.answer.refusal.model_dump() if row.answer.refusal else None
        pairs = (
            ("answer_markdown", row.answer.answer_markdown, want.get("answer_markdown")),
            ("refusal", refusal, want.get("refusal")),
            ("retrieved_top20", row.retrieved_top20, want.get("retrieved_top20") or []),
            ("route", row.route, want.get("route")),
        )
        out += [f"{row.id}: {name} differs" for name, a, b in pairs if a != b]
        if not same_tokens(row, want):
            out.append(f"{row.id}: token counts differ (not the recorded composer reply?)")
    claims = sum(len(r.answer.claims) for r in rows if r.answer)
    if claims != source["sets"]["claims"]["n"]:
        out.append(f"claim count {claims} != {source['sets']['claims']['n']}")
    return out


# --- CLI -------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from app.engine.engine import EngineSystem
    from eval.run import _git_sha, _load_rows, resolve_corpus_version

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, required=True, help="the recorded results file")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dsn", default=os.environ.get("EDGARQA_SERVING_DSN"))
    ap.add_argument("--cassettes", type=Path, default=Path("eval/cassettes"))
    ap.add_argument("--sets-dir", type=Path, default=Path("eval/sets"))
    ap.add_argument("--splits", type=Path, default=Path("eval/sets/splits.json"))
    args = ap.parse_args(argv)
    if not args.dsn:
        ap.error("no DSN: set EDGARQA_SERVING_DSN or pass --dsn")

    source = json.loads(args.source.read_text())
    config = source["config"]
    if config["system"] != "engine" or config["split"] != "dev" or config["sample"] is not None:
        ap.error("the source must be a full engine run on the dev split")
    if config["composer"] != WRITER:
        ap.error(f"Gate 0 measures the {WRITER} writer (ADR-049)")
    client = LLMClient(
        mode=LLMMode.replay,  # never calls the API: no key is read and nothing can be spent
        cassette_dir=args.cassettes,
        registry={"composer": config["composer"]},
    )
    system = EngineSystem()
    corpus_version = resolve_corpus_version(system, args.dsn, None)
    cfg = EvalCfg(
        llm=AccountingLLM(client),
        composer="composer",
        ef_search=config["ef_search"],
        top_k=config["top_k"],
        corpus_version=corpus_version,
        dsn=args.dsn,
    )
    items = _load_rows(config["sets"], config["split"], args.sets_dir, args.splits)
    try:
        rows = capture_rows(system, cfg, items)
    finally:
        system.close()

    import psycopg

    fact_ids, chunk_ids = cited_ids(rows)
    with psycopg.connect(
        args.dsn, autocommit=True, options="-c default_transaction_read_only=on"
    ) as conn:
        facts = fetch_facts(conn, fact_ids)
        chunks = fetch_chunks(conn, chunk_ids)
        ciks = {f.cik for f in facts.values()}
        ciks |= {c.cik for c in chunks.values() if c.cik is not None}
        names = fetch_company_names(conn, ciks)
    after = resolve_corpus_version(system, args.dsn, None)
    mismatches = compare_with_source(rows, source, corpus_version)
    if after != corpus_version:
        mismatches.append(f"corpus version changed during the capture: {corpus_version} -> {after}")

    trace = Trace(
        meta=TraceMeta(
            source_results=str(args.source),
            source_run_id=source["run_id"],
            source_git_sha=source["git_sha"],
            capture_git_sha=_git_sha(),
            captured_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            split=config["split"],
            composer=config["composer"],
            ef_search=config["ef_search"],
            top_k=config["top_k"],
            corpus_version=corpus_version,
            acceptance=Acceptance(passed=not mismatches, mismatches=mismatches),
        ),
        rows=rows,
        facts=facts,
        chunks=chunks,
        company_names=names,
    )
    out = args.out if not mismatches else args.out.with_suffix(".rejected.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(trace.model_dump_json(indent=1) + "\n")
    n_claims = sum(len(r.answer.claims) for r in rows if r.answer)
    print(
        f"wrote {out}: {len(rows)} rows, {n_claims} claims, {len(facts)} facts, "
        f"{len(chunks)} cited chunks, {len(chunk_ids - chunks.keys())} cited ids not found"
    )
    if mismatches:
        print(f"REJECTED: {len(mismatches)} differences from {source['run_id']}")
        for m in mismatches[:25]:
            print(f"  {m}")
        return 2
    print("ACCEPTED: the capture reproduces the recorded run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
