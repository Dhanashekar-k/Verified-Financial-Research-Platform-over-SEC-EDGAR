"""The verifier's inputs rebuilt from a saved trace (``eval.verifier_trace``), so the offline
runners and the tests need no database.

For the answers **as recorded**, they are what the engine would check. The trace holds only the
passages and facts those answers cited or the compiler built, not every passage shown: a claim
changed to cite something else may find nothing here although the engine would have found it."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from app.engine.entities.resolve_company import ALIASES_PATH, CompanyRecord, load_aliases
from app.engine.plan.catalog import MetricDef, load_catalog
from app.engine.verify.models import (
    ChunkRow,
    CompanyName,
    Evidence,
    FactRow,
    VerifyContext,
    allowed_ciks,
    company_names,
)
from eval.verifier_trace import DEFAULT_OUT as TRACE_PATH
from eval.verifier_trace import Trace, TraceRow

COMPANIES_PATH = Path("eval/results/verifier/gate0_dev_companies.json")
# A committed copy of data/targets/tracked_ciks.csv as it was at the capture: ``data/`` is
# gitignored, and the offline runners and tests must work from a fresh checkout.
TRACKED_PATH = Path("eval/results/verifier/gate0_dev_tracked_ciks.csv")


@dataclass(frozen=True)
class Reference:
    """What every row's context shares: the tracked companies and the metric catalog."""

    companies: dict[int, CompanyName]
    universe: frozenset[int]
    catalog: dict[str, MetricDef]


def load_reference(
    companies_path: Path = COMPANIES_PATH, tracked_path: Path = TRACKED_PATH
) -> Reference:
    with tracked_path.open(newline="") as fh:
        universe = frozenset(int(r["cik"]) for r in csv.DictReader(fh))
    records = [
        CompanyRecord(cik=r["cik"], name=r["name"], ticker=r["ticker"], tickers=r["aliases"] or [])
        for r in json.loads(companies_path.read_text())
        if r["cik"] in universe
    ]
    return Reference(
        companies=company_names(records, load_aliases(ALIASES_PATH)),
        universe=universe,
        catalog=load_catalog(),
    )


def load_trace(path: Path = TRACE_PATH) -> Trace:
    trace = Trace.model_validate_json(path.read_text())
    if not trace.meta.acceptance.passed:
        raise ValueError(f"{path} was not accepted as a reproduction of its source run")
    return trace


def evidence_for(trace: Trace, row: TraceRow) -> Evidence:
    """The facts (all of the trace's: a row can cite only what exists) and the row's passages,
    restricted to those its composer was shown."""
    shown = set(row.shown_chunk_ids)
    return Evidence(
        facts={i: FactRow.model_validate(f.model_dump()) for i, f in trace.facts.items()},
        chunks={
            i: ChunkRow.model_validate(c.model_dump())
            for i, c in trace.chunks.items()
            if i in shown
        },
    )


def context_for(row: TraceRow, reference: Reference) -> VerifyContext:
    if row.plan is None:
        raise ValueError(f"{row.id}: the trace holds no plan")
    return VerifyContext(
        plan=row.plan,
        results=row.results,
        not_found=row.not_found,
        unsupported=row.unsupported,
        text_only=row.text_only_fallback,
        assumed_period=row.assumed_period is not None,
        allowed_ciks=allowed_ciks(row.plan, reference.universe),
        companies=reference.companies,
        catalog=reference.catalog,
    )


def row_by_id(trace: Trace, question_id: str) -> TraceRow:
    for row in trace.rows:
        if row.id == question_id:
            return row
    raise KeyError(f"no question {question_id!r} in the trace")
