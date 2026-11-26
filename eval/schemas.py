"""Pydantic models for eval set rows and the results file (05_data_contracts.md section 6).

Set rows are read from ``eval/sets/*.jsonl`` (gold, Shekar-curated) or ``eval/sets/drafts/``.
The results file is the only source for ``docs/results.md``. Its listed keys follow section 6
exactly; a set may carry extra metrics as ``<metric>`` plus ``<metric>_ci95`` keys (for example
``recall_at_10`` / ``recall_at_10_ci95``), which keeps section 6's shape while letting the
narrative and retrieval metrics ride along.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, PlainSerializer

# Money is Decimal in code and a JSON number in the results file (section 6 shows numbers).
# Costs are sums of per-token prices with at most ~10 significant digits, which float holds.
MoneyJSON = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]

SetName = Literal["numeric", "financebench", "narrative", "unanswerable"]
SET_FILES: dict[str, str] = {
    "numeric": "numeric_auto.jsonl",
    "financebench": "financebench.jsonl",
    "narrative": "narrative.jsonl",
    "unanswerable": "unanswerable.jsonl",
}

# --- set rows -------------------------------------------------------------------------------


class NumericRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    qtype: Literal["single_value", "growth", "ratio", "compare"]
    cik: int
    metric: str
    fiscal_year: int
    fiscal_period: str
    gold_value: Decimal
    unit: str
    tolerance_rel: Decimal = Decimal("0.01")
    gold_fact_ids: list[int]
    template_id: str
    paraphrased: bool
    second_source_check: Literal["match", "mismatch", "unchecked"]
    # ADR-044 (section 6 notes): metric-aware tolerance, version policy, provenance.
    # Correct when |v - gold| <= max(tolerance_abs, tolerance_rel * |gold|).
    tolerance_abs: Decimal = Decimal(0)
    tolerance_basis: str | None = None
    value_basis: Literal["as_filed", "latest_restated"] = "as_filed"
    latest_value: Decimal | None = None
    is_restated: bool = False
    value_kind: Literal["reported", "derived"] = "reported"
    source_tags: list[str] = []
    resolution: str | None = None
    compare_cik: int | None = None
    period_end: date | None = None


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int  # 1-based PDF page (FinanceBench's 0-based evidence_page_num + 1, section 1.6)
    text: str


class FinanceBenchRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    gold_answer: str
    company: str
    doc_name: str
    evidence: list[Evidence]
    question_type: str
    reasoning: str


class NarrativeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    cik: int
    fiscal_year: int
    gold_chunk_ids: list[str]
    gold_points: list[str]
    drafted_by: str
    verified_by: str
    verified_at: date


class UnanswerableRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    reason: str
    expected: Literal["refuse"] = "refuse"


SetRow = NumericRow | FinanceBenchRow | NarrativeRow | UnanswerableRow
ROW_MODELS: dict[str, type[BaseModel]] = {
    "numeric": NumericRow,
    "financebench": FinanceBenchRow,
    "narrative": NarrativeRow,
    "unanswerable": UnanswerableRow,
}


def load_set(name: str, sets_dir: Path = Path("eval/sets")) -> list[BaseModel]:
    """Read and validate one set file; a malformed row fails loudly with its line number."""
    model = ROW_MODELS[name]
    path = sets_dir / SET_FILES[name]
    rows: list[BaseModel] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(model.model_validate_json(line))
        except ValueError as err:
            raise ValueError(f"{path}:{lineno}: {err}") from err
    return rows


# --- results file ---------------------------------------------------------------------------


class SetResult(BaseModel):
    """Per-set metrics. Section 6 names n, accuracy, ci95, judge_kappa, refusal_rate; extra
    metrics are allowed as <metric> and <metric>_ci95."""

    model_config = ConfigDict(extra="allow")

    n: int


class ClaimsResult(BaseModel):
    n: int
    unsupported_rate_pre: float | None = None
    unsupported_rate_post: float | None = None


class LatencySummary(BaseModel):
    """None when latency wasn't measured (replay, batch, stub): printed as NOT MEASURED."""

    p50: float | None
    p95: float | None


class CostSummary(BaseModel):
    total_usd: MoneyJSON
    per_question_usd: MoneyJSON


class PerQuestion(BaseModel):
    """One row per question. Section 6 keys plus set, split, refused, error and usage counts."""

    model_config = ConfigDict(extra="allow")

    id: str
    set: str
    correct: bool | None
    route: str
    cost_usd: MoneyJSON
    latency_ms: int
    trace_id: str | None = None


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    system: str
    system_version: str = "0"
    composer: str
    mode: str
    split: str
    sets: list[str]
    embedder: str | None = None
    ef_search: int | None = None
    top_k: int | None = None
    judge: str | None = None
    judge_rubrics: dict[str, str] = {}
    batches: list[dict[str, Any]] = []
    failed_usage_usd: str = "0"  # billed batch results that came back unusable
    latency_note: str | None = None
    sample: int | None = None  # --sample N: a seeded sample of N questions per set (None: all)
    sample_seed: int | None = None
    request_timeout_s: float | None = None  # raised from the 120 s default for long prompts
    verifier: str | None = None  # engine runs from M6: "on" | "off"


class ResultsFile(BaseModel):
    run_id: str
    git_sha: str
    corpus_version: int | None
    config: RunConfig
    sets: dict[str, SetResult | ClaimsResult | float | None]
    latency_ms: LatencySummary
    cost: CostSummary
    per_question: list[PerQuestion]
