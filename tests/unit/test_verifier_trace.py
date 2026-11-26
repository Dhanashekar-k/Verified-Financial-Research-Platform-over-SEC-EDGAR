"""M6 trace capture: what is read from the engine and the DB, and when a capture is rejected."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.engine.answer import Answer, ChunkProvenance, Claim, ExprProvenance, Refusal
from app.engine.engine import Diagnostics
from eval.schemas import UnanswerableRow
from eval.systems import record_retrieval, record_route
from eval.verifier_trace import (
    TraceRow,
    capture_rows,
    cited_ids,
    compare_with_source,
    fetch_chunks,
    fetch_facts,
    same_tokens,
)

ANSWER = Answer(
    answer_markdown="Revenue was 5 [1]. Management said so [2].",
    claims=[
        Claim(
            id="c1",
            kind="numeric",
            text="Revenue was 5.",
            provenance=ExprProvenance(type="expr", expr="f:7", fact_ids=[7]),
        ),
        Claim(
            id="c2",
            kind="text",
            text="Management said so.",
            provenance=ChunkProvenance(type="chunk", chunk_id="a#1#0001", quote="so"),
        ),
    ],
)


class FakeSystem:
    def __init__(self, fail: bool = False) -> None:
        self.diagnostics: dict[str, Diagnostics] = {}
        self.fail = fail

    def answer(self, question: str, cfg: Any) -> Answer:
        record_route("hybrid")
        record_retrieval(["a#1#0001", "b#1#0001"])
        diag = Diagnostics()
        diag.composed = True
        diag.retrieval = SimpleNamespace(chunks=[SimpleNamespace(chunk_id="a#1#0001")])  # type: ignore[assignment]
        self.diagnostics[question] = diag
        if self.fail:
            raise RuntimeError("no cassette")
        return ANSWER


def item(row_id: str, question: str) -> tuple[str, Any, str]:
    row = UnanswerableRow(id=row_id, question=question, reason="r", expected="refuse")
    return ("unanswerable", row, "dev")


def test_capture_keeps_answer_diagnostics_and_retrieval() -> None:
    [row] = capture_rows(FakeSystem(), None, [item("un-1", "q?")])
    assert row.answer == ANSWER and row.error is None
    assert row.route == "hybrid" and row.composed
    assert row.retrieved_top20 == ["a#1#0001", "b#1#0001"]
    assert row.shown_chunk_ids == ["a#1#0001"]


def test_capture_records_a_failure_without_stopping() -> None:
    rows = capture_rows(FakeSystem(fail=True), None, [item("un-1", "q?"), item("un-2", "r?")])
    assert [r.error for r in rows] == ["RuntimeError: no cassette"] * 2


def test_cited_ids_come_from_claims() -> None:
    row = TraceRow(id="x", set="numeric", question="q", answer=ANSWER)
    assert cited_ids([row]) == ({7}, {"a#1#0001"})


def source_for(rows: list[TraceRow]) -> dict[str, Any]:
    per_q = [
        {
            "id": r.id,
            "answer_markdown": r.answer.answer_markdown if r.answer else None,
            "refusal": None,
            "retrieved_top20": list(r.retrieved_top20),
            "route": r.route,
            "input_tokens": 2 * 10 + 100,  # the planner counted in both batch rounds
            "output_tokens": 2 * 5 + 50,
        }
        for r in rows
    ]
    claims = sum(len(r.answer.claims) for r in rows if r.answer)
    return {"corpus_version": 1, "per_question": per_q, "sets": {"claims": {"n": claims}}}


def good_rows() -> list[TraceRow]:
    return [
        TraceRow(
            id="x",
            set="numeric",
            question="q",
            answer=ANSWER,
            retrieved_top20=["a"],
            composed=True,
            llm_tokens=[(10, 5), (100, 50)],
        )
    ]


def test_an_identical_capture_is_accepted() -> None:
    rows = good_rows()
    assert compare_with_source(rows, source_for(rows), 1) == []


def test_every_kind_of_difference_is_reported() -> None:
    rows = good_rows()
    source = source_for(rows)
    changed = [
        rows[0].model_copy(
            update={
                "answer": ANSWER.model_copy(
                    update={"answer_markdown": "other", "refusal": Refusal(reason="no")}
                ),
                "retrieved_top20": ["b"],
                "route": "numeric",
            }
        )
    ]
    found = "\n".join(compare_with_source(changed, source, 2))
    for word in ("corpus_version", "answer_markdown", "refusal", "retrieved_top20", "route"):
        assert word in found


def test_errors_missing_rows_and_claim_count_are_reported() -> None:
    rows = good_rows()
    source = source_for(rows)
    errored = [rows[0].model_copy(update={"answer": None, "error": "boom"})]
    found = "\n".join(compare_with_source(errored, source, 1))
    assert "x: error: boom" in found and "claim count 0 != 2" in found
    assert "ids differ" in compare_with_source([], source, 1)[0]
    assert "ids differ" in compare_with_source(rows + rows, source, 1)[0]


class Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class FactsConn:
    """The ``facts`` view: id -> derived_from. Records each requested id list."""

    def __init__(self, parents: dict[int, list[int] | None]) -> None:
        self.parents = parents
        self.asked: list[list[int]] = []

    def execute(self, sql: str, params: list[Any]) -> Cursor:
        self.asked.append(params[0])
        columns = sql.split("SELECT ")[1].split(" FROM")[0].split(", ")
        base: dict[str, Any] = {
            "fact_key": 1,
            "cik": 1,
            "tag": "T",
            "taxonomy": "us-gaap",
            "uom": "USD",
            "qtrs": 4,
            "period_start": None,
            "period_end": "2023-12-31",
            "fiscal_year": 2023,
            "fiscal_period": "FY",
            "calendar_frame": None,
            "value": "1",
            "accession": "a",
            "form": "10-K",
            "filed": "2024-01-01",
            "is_latest": True,
            "is_original": True,
            "is_restated": False,
            "anomaly_score": None,
        }
        out = []
        for i in params[0]:
            if i in self.parents:
                row = base | {"fact_id": i, "derived_from": self.parents[i]}
                out.append(tuple(row[c] for c in columns))
        return Cursor(out)


def test_fetch_facts_follows_derived_parents_and_omits_missing_ids() -> None:
    conn = FactsConn({1: [2, 3], 2: None, 3: None})
    got = fetch_facts(conn, {1, 99})
    assert sorted(got) == [1, 2, 3]
    assert conn.asked == [[1, 99], [2, 3]]


def test_fetching_nothing_runs_no_query() -> None:
    conn = FactsConn({})
    assert fetch_facts(conn, set()) == {} and fetch_chunks(conn, set()) == {}
    assert conn.asked == []


@pytest.mark.parametrize(
    ("composed", "calls", "recorded", "ok"),
    [
        (True, [(10, 5), (100, 50)], (120, 60), True),  # planner counted twice
        (True, [(10, 5), (100, 50)], (110, 55), True),  # or once
        (True, [(10, 5), (100, 54)], (120, 60), False),  # another cassette, same markdown
        (True, [(10, 5)], (20, 10), False),  # composed without a composer call
        (False, [(10, 5)], (10, 5), True),
        (False, [(10, 5)], (30, 15), False),
        (False, [], (0, 0), False),
    ],
)
def test_token_counts_must_account_for_the_recorded_run(
    composed: bool, calls: list[tuple[int, int]], recorded: tuple[int, int], ok: bool
) -> None:
    row = TraceRow(id="x", set="numeric", question="q", composed=composed, llm_tokens=calls)
    assert same_tokens(row, {"input_tokens": recorded[0], "output_tokens": recorded[1]}) is ok


def test_capture_slices_the_ledger_per_row() -> None:
    cfg = SimpleNamespace(
        llm=SimpleNamespace(ledger=[SimpleNamespace(input_tokens=1, output_tokens=1)])
    )

    class Spending(FakeSystem):
        def answer(self, question: str, cfg: Any) -> Answer:
            cfg.llm.ledger.append(SimpleNamespace(input_tokens=7, output_tokens=3))
            return super().answer(question, cfg)

    rows = capture_rows(Spending(), cfg, [item("un-1", "q?"), item("un-2", "r?")])
    assert [r.llm_tokens for r in rows] == [[(7, 3)], [(7, 3)]]
