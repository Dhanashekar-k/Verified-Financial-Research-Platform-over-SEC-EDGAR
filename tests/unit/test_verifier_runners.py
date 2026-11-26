"""The offline M6 runners over the real committed trace. The verifier inside is a fake (see
tests/support/fake_verifier.py): these test the plumbing and the scoring, not the checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eval.schemas import load_set
from eval.verifier_ablation import ANSWERABLE, build_report, verified_arm
from eval.verifier_adversarial import (
    BOUNDARY_CATEGORIES,
    CONTROL_CATEGORY,
    DROP_CATEGORIES,
    SET_PATH,
    AdversarialRow,
    base_claim,
    changed_claim,
    list_bases,
    load_rows,
    problems,
    score,
    show,
    unshown_citations,
)
from eval.verifier_inputs import (
    context_for,
    evidence_for,
    load_reference,
    load_trace,
    row_by_id,
)
from tests.support.fake_verifier import FakeVerifier

GATE0 = json.loads(Path("eval/results/verifier/gate0_dev_pre.json").read_text())
LABELS = {(v["question_id"], v["claim_id"]): v["label"] for v in GATE0["verdicts"]}


@pytest.fixture(scope="module")
def trace() -> Any:
    return load_trace()


@pytest.fixture(scope="module")
def reference() -> Any:
    return load_reference()


def gold() -> dict[str, Any]:
    return {r.id: r for r in load_set("numeric", Path("eval/sets"))}  # type: ignore[attr-defined]


def test_every_composed_dev_row_has_verifier_inputs(trace: Any, reference: Any) -> None:
    composed = [r for r in trace.rows if r.composed]
    assert composed
    for row in composed:
        context_for(row, reference)
        evidence = evidence_for(trace, row)
        assert set(evidence.chunks) <= set(row.shown_chunk_ids)


def test_a_verifier_that_drops_nothing_reproduces_the_recorded_run(
    trace: Any, reference: Any
) -> None:
    arm = verified_arm(trace, reference, FakeVerifier().verifier())
    report = build_report(trace, arm, LABELS, gold())
    engine, verified = report["arms"]["engine"], report["arms"]["engine_plus_verifier"]
    recorded = GATE0["dev_answerable_false_refusal_no_verifier"]["value"]
    assert engine["answerable_false_refusal"]["rate"] == pytest.approx(recorded)
    assert verified["answerable_false_refusal"] == engine["answerable_false_refusal"]
    assert verified["claims"] == engine["claims"]
    assert engine["claims"]["all"]["unsupported"]["n"] == GATE0["n_claims"]
    assert engine["claims"]["text"]["unsupported"]["n"] == GATE0["headline_text_claims"]["n"]
    assert verified["numeric_accuracy"] == engine["numeric_accuracy"]
    assert report["false_refusal_difference"]["newly_refused"] == 0
    assert report["dropped_claims"] == []
    assert verified["financebench_accuracy"] == "NOT MEASURED"


def test_drops_show_up_as_new_refusals_with_their_judge_labels(trace: Any, reference: Any) -> None:
    texts = {c.text for r in trace.rows if r.answer for c in r.answer.claims if c.kind == "text"}
    arm = verified_arm(trace, reference, FakeVerifier(fail_texts=texts).verifier())
    report = build_report(trace, arm, LABELS, gold())
    diff = report["false_refusal_difference"]
    assert diff["verifier_minus_engine"] > 0 and diff["newly_refused"] > 0
    assert diff["n"] == sum(r.set in ANSWERABLE for r in trace.rows)
    n_text = GATE0["headline_text_claims"]["n"]
    assert report["dropped_claims_by_check"] == {"quote_substring": n_text}
    assert sum(report["dropped_claims_by_judge_label"].values()) == n_text
    assert report["arms"]["engine_plus_verifier"]["claims"]["text"]["unsupported"]["n"] == 0


# --- adversarial -----------------------------------------------------------------------------


def row(rid: str, category: str, change: dict[str, Any], base: str = "fb-00494/c2") -> Any:
    qid, cid = base.split("/")
    expected = "drop" if category in DROP_CATEGORIES else "keep"
    return AdversarialRow.model_validate(
        {
            "id": rid,
            "category": category,
            "base": {"question_id": qid, "claim_id": cid},
            "change": change,
            "expected": expected,
            "note": "test row, not gold",
            "authored_by": "test",
        }
    )


def test_scoring_separates_caught_missed_vacuous_boundary_and_controls(
    trace: Any, reference: Any
) -> None:
    caught_text, boundary_text = "The 787 rate rose to 50.", "The rate rose because of demand."
    rows = [
        row("a", "changed_number", {"text": caught_text}),
        row("b", "wrong_company", {"text": "Airbus increased the rate."}),
        row("c", "wrong_year", {"text": "In 1999 it rose."}, base="fb-00746/c1"),
        row("d", "added_cause", {"text": boundary_text}),
        row("e", "correct", {}),
    ]
    vacuous_base = base_claim(trace, rows[2].base).text
    fake = FakeVerifier(fail_texts={caught_text, vacuous_base, "In 1999 it rose."})
    report = score(rows, trace, reference, fake.verifier())
    outcomes = {r["id"]: r["outcome"] for r in report["rows"]}
    assert outcomes == {"a": "caught", "b": "missed", "c": "vacuous", "d": "kept", "e": "kept"}
    errors = report["errors_to_catch"]
    assert (errors["n"], errors["caught"], errors["missed"], errors["vacuous"]) == (3, 1, 1, 1)
    assert errors["recall"] == pytest.approx(1 / 3) and len(errors["ci95_wilson"]) == 2
    assert report["boundary_known_misses"]["kept"] == 1
    assert report["controls"] == {"n": 1, "kept": 1, "dropped": 0, "vacuous": 0}
    assert report["by_category"]["changed_number"] == {
        "expected": "drop", "rows": 1, "caught": 1, "missed": 0, "vacuous": 0,
    }  # fmt: skip
    assert report["rows"][3]["failed_check"] is None
    assert report["rows"][0]["failed_check"] == "quote_substring"


def test_dropping_everything_catches_nothing(trace: Any, reference: Any) -> None:
    rows = [row("a", "changed_number", {"text": "changed 9"})]
    fake = FakeVerifier(fail_ids={"c2"})
    report = score(rows, trace, reference, fake.verifier())
    assert report["errors_to_catch"]["caught"] == 0
    assert report["errors_to_catch"]["vacuous"] == 1


def test_a_change_replaces_only_what_it_names(trace: Any) -> None:
    claim = base_claim(trace, row("a", "correct", {}).base)
    new = changed_claim(claim, row("a", "changed_number", {"text": "x 1"}).change)
    assert new.text == "x 1" and new.provenance == claim.provenance and new.id == claim.id
    quoted = {"type": "chunk", "chunk_id": "z", "quote": "not there at all"}
    moved = changed_claim(claim, row("a", "absent_quote", {"provenance": quoted}).change)
    assert moved.text == claim.text and moved.provenance.model_dump() == quoted


@pytest.mark.parametrize(
    "bad",
    [
        {"category": "wrong_year", "expected": "keep", "change": {"text": "x"}},
        {"category": "added_cause", "expected": "drop", "change": {"text": "x"}},
        {"category": "sign_flip", "expected": "drop", "change": {}},
        {"category": "nonsense", "expected": "drop", "change": {"text": "x"}},
        {"category": "correct", "expected": "keep", "change": {"colour": "red"}},
        {"category": "correct", "expected": "keep", "change": {}, "authored_by": ""},
    ],
)
def test_invalid_rows_are_rejected(bad: dict[str, Any]) -> None:
    base = {
        "id": "x",
        "base": {"question_id": "q", "claim_id": "c1"},
        "note": "n",
        "authored_by": "t",
    }
    with pytest.raises(ValueError):
        AdversarialRow.model_validate(base | bad)


def test_unscorable_rows_are_reported(trace: Any) -> None:
    same = base_claim(trace, row("a", "correct", {}).base).text
    rows = [
        row("a", "changed_number", {"text": same}),
        row("b", "changed_number", {"text": "x"}, base="fb-00494/c99"),
        row(
            "c",
            "formula_not_built",
            {"provenance": {"type": "expr", "expr": "f:1 +", "fact_ids": [1]}},
        )
        if False
        else row("c", "changed_number", {"value": "abc"}),
    ]
    found = "\n".join(problems(rows, trace))
    assert "a: the change leaves" in found and "no claim 'c99'" in found and "c:" in found


def test_a_boundary_row_may_use_a_real_over_claim_unchanged(trace: Any) -> None:
    assert problems([row("a", "invalid_inference", {}, base="fb-01328/c2")], trace) == []


def test_repeated_ids_are_rejected(tmp_path: Path) -> None:
    line = row("a", "correct", {}).model_dump_json(exclude_unset=True)
    path = tmp_path / "s.jsonl"
    path.write_text(f"{line}\n{line}\n")
    with pytest.raises(ValueError, match="repeated"):
        load_rows(path)


def test_authoring_helpers_show_a_base_with_its_evidence(trace: Any) -> None:
    numeric = show(trace, "num-0003", "c1")
    assert "compiled results:" in numeric and "cited facts:" in numeric
    passage = show(trace, "fb-00494", "c2")
    assert "increased the production rate" in passage
    listed = list_bases(trace, LABELS).splitlines()
    assert len(listed) == GATE0["all_claims"]["supported"]
    assert not any(line.startswith("fb-01328 ") for line in listed)


def test_categories_do_not_overlap() -> None:
    assert not set(DROP_CATEGORIES) & set(BOUNDARY_CATEGORIES)
    assert CONTROL_CATEGORY not in DROP_CATEGORIES + BOUNDARY_CATEGORIES


@pytest.mark.skipif(not SET_PATH.exists(), reason="the hand-written set does not exist yet")
def test_the_hand_written_set_is_valid(trace: Any) -> None:
    """Validity only. Its score is a measurement, never a test assertion: a test that demanded
    catches would turn the set into a target to tune against."""
    assert problems(load_rows(SET_PATH), trace) == []


def test_a_changed_keep_row_on_a_dropped_base_is_vacuous_not_a_catch(
    trace: Any, reference: Any
) -> None:
    rows = [
        row("a", "invalid_inference", {"text": "Therefore it is zero."}),
        row("b", "invalid_inference", {}, base="fb-00746/c1"),
    ]
    dropped = {base_claim(trace, r.base).text for r in rows}
    report = score(rows, trace, reference, FakeVerifier(fail_texts=dropped).verifier())
    assert [r["outcome"] for r in report["rows"]] == ["vacuous", "dropped"]
    assert report["rows"][1]["failed_check"] == "quote_substring"
    assert report["boundary_known_misses"]["vacuous"] == 1


def test_a_row_cannot_cite_evidence_the_trace_did_not_save(trace: Any) -> None:
    passage = {"type": "chunk", "chunk_id": "typo#1#0000", "quote": "increased the production"}
    fact = {"type": "expr", "expr": "f:5", "fact_ids": [5]}
    found = "\n".join(
        problems(
            [
                row("a", "wrong_company", {"provenance": passage}),
                row("b", "formula_not_built", {"provenance": fact}, base="num-0003/c1"),
            ],
            trace,
        )
    )
    assert "a: passage not saved in the trace" in found
    assert "b: facts not saved in the trace: [5]" in found


def test_a_saved_but_unshown_passage_is_allowed_with_a_warning(trace: Any) -> None:
    shown = set(row_by_id(trace, "fb-00494").shown_chunk_ids)
    other = next(i for i in sorted(trace.chunks) if i not in shown)
    moved = {"type": "chunk", "chunk_id": other, "quote": "increased the production rate"}
    rows = [row("a", "wrong_company", {"provenance": moved})]
    assert problems(rows, trace) == []
    assert "was not shown" in unshown_citations(rows, trace)[0]
