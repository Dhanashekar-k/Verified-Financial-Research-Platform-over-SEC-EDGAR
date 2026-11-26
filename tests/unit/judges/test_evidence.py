"""The support judge's evidence text (evidence-v1): identity is shown, nothing is computed."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from eval.judges.evidence import chunk_evidence, expr_evidence
from eval.verifier_trace import ChunkRecord, FactRecord

NAMES = {320193: "Apple Inc."}


def fact(fact_id: int, value: str, **over: object) -> FactRecord:
    base: dict[str, object] = {
        "fact_id": fact_id,
        "fact_key": fact_id,
        "cik": 320193,
        "tag": "OperatingIncomeLoss",
        "taxonomy": "us-gaap",
        "uom": "USD",
        "qtrs": 4,
        "period_start": date(2022, 10, 1),
        "period_end": date(2023, 9, 30),
        "fiscal_year": 2023,
        "fiscal_period": "FY",
        "value": Decimal(value),
        "accession": "0000320193-23-000106",
        "form": "10-K",
        "filed": date(2023, 11, 3),
        "is_latest": True,
        "is_original": True,
        "is_restated": False,
    }
    return FactRecord.model_validate(base | over)


def test_chunk_evidence_names_the_filing_and_keeps_the_whole_passage() -> None:
    chunk = ChunkRecord(
        chunk_id="a#7#0001",
        cik=320193,
        form="10-K",
        fiscal_year=2023,
        fiscal_period="FY",
        section="7",
        section_title="MD&A",
        text="First sentence.\nNet sales decreased 3%.\nLast sentence.",
    )
    out = chunk_evidence(chunk, NAMES)
    assert "Apple Inc. (CIK 320193)" in out
    assert "10-K" in out and "2023 (FY)" in out and "7 MD&A" in out
    assert out.endswith(chunk.text)


def test_chunk_evidence_says_unknown_for_missing_identity() -> None:
    out = chunk_evidence(ChunkRecord(chunk_id="x", text="body"), NAMES)
    assert "Company: unknown" in out and "Section: unknown" in out


def test_expr_evidence_shows_each_fact_and_does_not_evaluate() -> None:
    facts = {
        1: fact(1, "114301000000.0000"),
        -2: fact(-2, "383285000000", tag="Revenues"),
    }
    out = expr_evidence("f:1 / f:-2", [1, -2], facts, NAMES)
    assert "f:1 / f:-2" in out
    assert "Value: 114301000000\n" in out and "Value: 383285000000" in out
    assert "OperatingIncomeLoss" in out and "Revenues" in out
    assert "2022-10-01 to 2023-09-30, 4 quarter(s) long" in out
    assert "as originally filed" in out
    assert "0.298" not in out and "29.8" not in out  # the judge does its own arithmetic


def test_expr_evidence_instant_restated_and_missing() -> None:
    facts = {
        3: fact(3, "-5.5", qtrs=0, period_start=None, is_original=False, is_latest=True),
        4: fact(4, "0", is_latest=False),
    }
    out = expr_evidence("f:3 + f:4 + f:9", [3, 4, 9], facts, NAMES)
    assert "a balance as of 2023-09-30" in out
    assert "the latest restated value" in out and "(later restated)" in out
    assert "Value: -5.5" in out and "Value: 0\n" in out
    assert "Fact f:9: no such fact" in out


def test_expr_evidence_shows_derived_parents_in_order() -> None:
    facts = {
        10: fact(10, "30", fiscal_period="Q4", qtrs=1, derived_from=[11, 12]),
        11: fact(11, "100"),
        12: fact(12, "70", fiscal_period="9M", qtrs=3),
    }
    out = expr_evidence("f:10", [10], facts, NAMES)
    assert "first parent fact below minus the second" in out
    assert out.index("Fact f:11") < out.index("Fact f:12")
