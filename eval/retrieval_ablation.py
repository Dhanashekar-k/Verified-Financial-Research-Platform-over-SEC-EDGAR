"""Retrieval ablation on the dev split (M05 section 3.6): which component earns its place.

    uv run python -m eval.retrieval_ablation --dsn "$EDGARQA_SERVING_DSN"   # needs the serving box

Rows, each adding one component: lexical only, dense only, hybrid, + rerank, + filters, + section
boost. Questions: FinanceBench (gold evidence chunks) and narrative (gold chunks) rows of the DEV
split only. The plan is an ORACLE: the gold company and fiscal year, so this isolates retrieval from
planner errors; the end-to-end benchmark measures the planner too. Metric: hit@k
(`eval.metrics.recall_at_k`) for k in (1, 3, 5, 8) with a 95% bootstrap CI, per-question flags (so
the report can run paired bootstraps between rows) and client-side latency per call. The latency
includes the SSM tunnel's round trips and the query embedding; it is not the box's own speed.
Output: `eval/results/ablation/<run_id>.json` (a subdirectory: the results report reads only the
top level of `eval/results`).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.engine.engine import default_model_dir
from app.engine.plan.schema import QueryPlan
from app.engine.retrieval.query import RetrievalConfig, RetrievalResult, retrieve
from eval.metrics import recall_at_k
from eval.run import _load_rows
from eval.schemas import FinanceBenchRow, NarrativeRow
from eval.stats import bootstrap_ci, percentile

KS = (1, 3, 5, 8)
FB_COMPANIES = Path("tests/fixtures/entities/financebench.csv")
FB_GOLD = Path("eval/sets/drafts/financebench_evidence_chunks.json")

_BASE = RetrievalConfig()
ROWS: list[tuple[str, RetrievalConfig]] = [
    ("lexical only", replace(_BASE, dense=False, rerank=False, filters=False, section_boost=False)),
    ("dense only", replace(_BASE, lexical=False, rerank=False, filters=False, section_boost=False)),
    ("hybrid (RRF)", replace(_BASE, rerank=False, filters=False, section_boost=False)),
    ("hybrid + rerank", replace(_BASE, filters=False, section_boost=False)),
    ("hybrid + rerank + filters", replace(_BASE, section_boost=False)),
    ("hybrid + rerank + filters + section boost (oracle hint)", _BASE),
]


# A second row set with the company and year filter on throughout. The six rows above add the
# filter last, so they cannot show what a component adds once it is on (and rows without it run
# a full-text search over every chunk).
_FILTERED = replace(_BASE, section_boost=False)
FILTERED_ROWS: list[tuple[str, RetrievalConfig]] = [
    ("lexical + filters", replace(_FILTERED, dense=False, rerank=False)),
    ("dense + filters", replace(_FILTERED, lexical=False, rerank=False)),
    ("hybrid + filters", replace(_FILTERED, rerank=False)),
    ("lexical + filters + rerank", replace(_FILTERED, dense=False)),
    ("hybrid + filters + rerank", _FILTERED),
]
ROW_SETS = {"cumulative": ROWS, "filtered": FILTERED_ROWS}
OUT_DIRS = {
    "cumulative": Path("eval/results/ablation"),
    "filtered": Path("eval/results/ablation_filtered"),
}


@dataclass(frozen=True)
class Question:
    id: str
    set_name: str
    question: str
    plan: QueryPlan
    gold: list[str]


def fiscal_year_of(doc_name: str) -> int:
    """`3M_2018_10K` -> 2018, `Pfizer_2023Q2_10Q` -> 2023: the first four-digit group, which is how
    curated.chunks labels FinanceBench PDFs (checked on the local warehouse, 2026-11-25)."""
    m = re.search(r"_(\d{4})(?:Q[1-4])?_", doc_name)
    if m is None:
        raise ValueError(f"no fiscal year in doc_name {doc_name!r}")
    return int(m.group(1))


def gold_section(chunk_ids: Sequence[str]) -> str | None:
    """The section of an EDGAR gold chunk (`{accession}#{section}#{index}`), if every gold chunk
    agrees; None for a PDF chunk (`fb:...#p12#0`, no section) or mixed sections."""
    sections = {c.split("#")[1] for c in chunk_ids if not c.startswith("fb:") and c.count("#") == 2}
    return sections.pop() if len(sections) == 1 else None


def oracle_plan(company: str, cik: int, fiscal_year: int, section: str | None = None) -> QueryPlan:
    """The gold company and fiscal year; ``section`` (narrative rows) is the gold chunk's own
    section as a hint: a best case for the boost, a perfect planner."""
    return QueryPlan.model_validate(
        {
            "intent": "narrative",
            "companies": [{"mention": company, "cik": cik}],
            "periods": [{"fiscal_year": fiscal_year}],
            "section_hints": [section] if section else [],
        }
    )


def build_questions(
    items: Sequence[tuple[str, Any, str | None]],
    fb_ciks: dict[str, int],
    fb_gold: dict[str, list[str]],
    company_names: dict[int, str],
) -> list[Question]:
    out: list[Question] = []
    for set_name, row, _ in items:
        if isinstance(row, FinanceBenchRow):
            gold = fb_gold.get(row.id, [])
            if not gold:
                continue  # no gold chunk to hit: unscorable, never counted as a miss
            plan = oracle_plan(row.company, fb_ciks[row.company], fiscal_year_of(row.doc_name))
            out.append(Question(row.id, set_name, row.question, plan, gold))
        elif isinstance(row, NarrativeRow):
            name = company_names.get(row.cik, str(row.cik))
            plan = oracle_plan(name, row.cik, row.fiscal_year, gold_section(row.gold_chunk_ids))
            out.append(Question(row.id, set_name, row.question, plan, row.gold_chunk_ids))
    return out


def run_ablation(
    questions: Sequence[Question],
    rows: Sequence[tuple[str, RetrievalConfig]],
    *,
    retrieve_fn: Callable[..., RetrievalResult],
    conn: Any,
    embedder: Any,
    reranker: Any,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name, cfg in rows:
        per_q: list[dict[str, Any]] = []
        for q in questions:
            t0 = time.perf_counter()
            res = retrieve_fn(
                conn,
                q.plan,
                q.question,
                embedder=embedder if cfg.dense else None,
                reranker=reranker if cfg.rerank else None,
                cfg=cfg,
            )
            ms = (time.perf_counter() - t0) * 1000
            ids = res.chunk_ids
            per_q.append(
                {
                    "id": q.id,
                    "set": q.set_name,
                    "latency_ms": round(ms, 2),
                    "relaxed_year_filter": res.relaxed_year_filter,
                    "hits": {str(k): recall_at_k(ids, q.gold, k) for k in KS},
                }
            )
        results.append({"name": name, "config": asdict(cfg), "per_question": per_q})
        log(f"{name}: {len(per_q)} questions")
    return results


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add per-set recall@k with CIs and latency percentiles to each row (per-question kept)."""
    out = []
    for row in rows:
        sets: dict[str, Any] = {}
        for set_name in sorted({q["set"] for q in row["per_question"]}):
            qs = [q for q in row["per_question"] if q["set"] == set_name]
            stat: dict[str, Any] = {"n": len(qs)}
            for k in KS:
                flags = [q["hits"][str(k)] for q in qs]
                lo, hi = bootstrap_ci(flags)
                stat[f"recall_at_{k}"] = sum(flags) / len(flags)
                stat[f"recall_at_{k}_ci95"] = [lo, hi]
            sets[set_name] = stat
        lat = [q["latency_ms"] for q in row["per_question"]]
        out.append(
            {
                **row,
                "sets": sets,
                "latency_ms": {"p50": percentile(lat, 50), "p95": percentile(lat, 95)},
                "n_relaxed_year_filter": sum(q["relaxed_year_filter"] for q in row["per_question"]),
            }
        )
    return out


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Retrieval ablation on the dev split.")
    ap.add_argument("--split", default="dev", choices=["dev", "all"])
    ap.add_argument("--dsn", default=os.environ.get("EDGARQA_SERVING_DSN"))
    ap.add_argument("--rows", default="cumulative", choices=sorted(ROW_SETS))
    ap.add_argument("--out", type=Path, default=None, help="default: one folder per row set")
    ap.add_argument("--sets-dir", type=Path, default=Path("eval/sets"))
    ap.add_argument("--splits", type=Path, default=Path("eval/sets/splits.json"))
    ap.add_argument("--model-dir", type=Path, default=default_model_dir())
    ap.add_argument("--reranker", default=None, help="fastembed cross-encoder model name")
    ap.add_argument("--sample", type=int, default=None, help="first N questions (smoke runs)")
    args = ap.parse_args(argv)
    if not args.dsn:
        raise SystemExit("needs the serving DSN (--dsn or EDGARQA_SERVING_DSN)")

    import psycopg

    from app.engine.entities.resolve_company import CompanyIndex
    from app.engine.retrieval.rerank import DEFAULT_RERANKER, CrossEncoderReranker
    from pipelines.common.embedding import ARTIFACT_ONNX_SHA256, get_embedder

    items = _load_rows(["financebench", "narrative"], args.split, args.sets_dir, args.splits)
    with FB_COMPANIES.open(newline="") as fh:
        fb_ciks = {r["company"]: int(r["cik"]) for r in csv.DictReader(fh)}
    fb_gold = json.loads(FB_GOLD.read_text())["gold_chunk_ids"]
    index = CompanyIndex.from_csv(Path("tests/fixtures/entities/companies.csv"))
    names = {cik: r.name for cik, r in index.records.items()}
    questions = build_questions(items, fb_ciks, fb_gold, names)
    if args.sample:
        questions = questions[: args.sample]
    reranker_name = args.reranker or DEFAULT_RERANKER
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        state = conn.execute("SELECT live_version FROM corpus_state").fetchone()
        corpus_version = None if state is None else int(state[0])
        rows = run_ablation(
            questions,
            ROW_SETS[args.rows],
            retrieve_fn=retrieve,
            conn=conn,
            embedder=get_embedder(
                args.model_dir, threads=2, expected_onnx_sha256=ARTIFACT_ONNX_SHA256
            ),
            reranker=CrossEncoderReranker(reranker_name),
        )
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    run_id = f"{stamp}_retrieval-ablation-{args.rows}"
    doc = {
        "run_id": run_id,
        "git_sha": _git_sha(),
        "corpus_version": corpus_version,
        "config": {
            "split": args.split,
            "row_set": args.rows,
            "plan_source": "oracle: the gold company and fiscal year, not the planner; "
            "narrative rows also get their gold chunk's section as the hint (a best case for "
            "the boost; FinanceBench PDFs have no section, so that row repeats the one before)",
            "reranker": reranker_name,
            "ks": list(KS),
            "n_questions": len(questions),
            "sample": args.sample,  # None for a full run; the report skips sampled ones
            "latency_note": "client-side wall time per call, including the tunnel and the "
            "query embedding; not the serving box's own speed",
        },
        "rows": summarize(rows),
    }
    out_dir = args.out or OUT_DIRS[args.rows]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.json"
    path.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
