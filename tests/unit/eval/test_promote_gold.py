"""Promotion of reviewed drafts to gold (the tool Shekar runs; tests write only to tmp_path)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from eval.gen import promote_gold
from eval.schemas import NarrativeRow, UnanswerableRow

DRAFT_DIR = Path("eval/sets/drafts")
ON = date(2026, 11, 25)


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _narrative(n: int = 50) -> list[dict[str, Any]]:
    return [
        {
            "id": f"nar-{i:03d}",
            "question": f"q{i}",
            "cik": 1,
            "fiscal_year": 2023,
            "gold_chunk_ids": [f"0-23-1#1A#{i:04d}"],
            "gold_points": ["a", "b"],
            "drafted_by": "claude-sonnet-5-5",
            "verified_by": None,
            "verified_at": None,
            "_draft_id": f"nar-d{i}",
            "_source_chunks": [{"chunk_id": "x", "text": "t"}],
        }
        for i in range(n)
    ]


def test_narrative_is_stripped_stamped_and_validated(tmp_path: Path) -> None:
    rows = promote_gold.prepare(
        "narrative", _write(tmp_path / "d.jsonl", _narrative()), "Shekar", ON
    )
    assert len(rows) == 50
    assert all(not k.startswith("_") for r in rows for k in r)
    assert rows[0]["verified_by"] == "Shekar" and rows[0]["verified_at"] == "2026-11-25"
    NarrativeRow.model_validate(rows[0])


def test_narrative_needs_a_named_reviewer(tmp_path: Path) -> None:
    path = _write(tmp_path / "d.jsonl", _narrative())
    for who in (None, "", "   "):
        with pytest.raises(ValueError, match="verified-by"):
            promote_gold.prepare("narrative", path, who, ON)


def test_wrong_row_count_and_duplicate_ids_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="expected 50"):
        promote_gold.prepare("narrative", _write(tmp_path / "a.jsonl", _narrative(49)), "S", ON)
    dup = _narrative()
    dup[1]["id"] = dup[0]["id"]
    with pytest.raises(ValueError, match="duplicate"):
        promote_gold.prepare("narrative", _write(tmp_path / "b.jsonl", dup), "S", ON)


def test_a_row_with_an_unknown_field_fails_schema_validation(tmp_path: Path) -> None:
    rows = _narrative()
    rows[3]["surprise"] = 1  # extra="forbid": not stripped, because it is not review-only
    with pytest.raises(ValueError):
        promote_gold.prepare("narrative", _write(tmp_path / "d.jsonl", rows), "S", ON)


def test_main_never_overwrites_an_existing_gold_file(tmp_path: Path) -> None:
    sets = tmp_path / "sets"
    sets.mkdir()
    (sets / "narrative.jsonl").write_text("gold\n")
    draft = _write(tmp_path / "d.jsonl", _narrative())
    code = promote_gold.main(
        ["narrative", "--verified-by", "S", "--drafts", str(draft), "--sets-dir", str(sets)]
    )
    assert code == 2 and (sets / "narrative.jsonl").read_text() == "gold\n"


def test_main_writes_the_gold_file_and_it_loads(tmp_path: Path) -> None:
    sets = tmp_path / "sets"
    sets.mkdir()
    draft = _write(tmp_path / "d.jsonl", _narrative())
    assert (
        promote_gold.main(
            ["narrative", "--verified-by", "S", "--drafts", str(draft), "--sets-dir", str(sets)]
        )
        == 0
    )
    from eval.schemas import load_set

    assert len(load_set("narrative", sets)) == 50


# --- the real drafts, as they will be reviewed --------------------------------------------


def test_the_real_unanswerable_draft_is_valid_balanced_and_unique() -> None:
    rows = [json.loads(x) for x in (DRAFT_DIR / "unanswerable.jsonl").read_text().splitlines()]
    parsed = [UnanswerableRow.model_validate(r) for r in rows]
    assert len(parsed) == 50 and len({r.question for r in parsed}) == 50
    # Shekar's review may reword or swap rows, so only the reasons and the count are pinned.
    assert {r.reason for r in parsed} == {
        "future_guidance_not_in_filings",
        "not_disclosed",
        "out_of_scope",
        "false_premise",
        "wrong_entity_period",
    }
    assert all(r.question.strip().endswith(("?", ".")) for r in parsed)


def test_the_real_narrative_draft_is_valid_and_matches_the_requested_shape() -> None:
    rows = promote_gold.prepare("narrative", DRAFT_DIR / "narrative_selected.jsonl", "reviewer", ON)
    assert len(rows) == 50
    assert all(2 <= len(r["gold_points"]) <= 4 and r["gold_chunk_ids"] for r in rows)
    assert len({r["question"] for r in rows}) == 50
