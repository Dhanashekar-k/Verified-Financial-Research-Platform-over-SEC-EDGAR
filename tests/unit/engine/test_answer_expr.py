"""The `expr` grammar of 05 section 4.3 with signed fact ids (ADR-046).

fact_id is a signed xxhash64: 179 of the 360 `gold_fact_ids` in numeric_auto.jsonl are negative, so
the grammar must accept `f:-123`. Everything valid before the widening stays valid.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.engine.answer import Answer, ExprProvenance

PINNED_ANSWER_SCHEMA_SHA256 = "1a10f56c6be5f5b18995167bed2a318a6d0f3c892241eba4c8f4e1a0c5b00678"
NEG = -6651281536542086215
POS = 5106506160777097432


def _ok(expr: str, ids: list[int]) -> ExprProvenance:
    return ExprProvenance(type="expr", expr=expr, fact_ids=ids)


@pytest.mark.parametrize(
    ("expr", "ids"),
    [
        ("f:111", [111]),
        ("f:111 / f:222", [111, 222]),
        ("(f:2 - f:1) / f:1", [2, 1]),
        ("f:111 * 100", [111]),
        (f"f:{NEG}", [NEG]),
        (f"f:{POS} - f:{NEG}", [POS, NEG]),
        (f"(f:{NEG} - f:{POS}) / f:{POS}", [NEG, POS]),
        ("f:-5 - f:-3", [-5, -3]),
        ("f:5-f:-3", [5, -3]),
        ("f:-5 / (f:-3 + f:2)", [-5, -3, 2]),
        ("f:1 * 1e3", [1]),  # numeric literals that were valid before stay valid
        ("f:1 * 1_000", [1]),
        ("-f:5 + f:-3", [5, -3]),
        (f"f:{2**63 - 1} - f:{-(2**63)}", [2**63 - 1, -(2**63)]),
    ],
)
def test_valid_exprs(expr: str, ids: list[int]) -> None:
    assert _ok(expr, ids).expr == expr


@pytest.mark.parametrize(
    ("expr", "ids"),
    [
        ("", []),
        ("f:", []),
        ("f:- 5", [5]),  # a sign must touch the digits
        ("f:--5", [5]),
        ("f:1 f:2", [1, 2]),  # juxtaposed tokens
        ("f:1 ** f:2", [1, 2]),
        ("f:1 % f:2", [1, 2]),
        ("abs(f:1)", [1]),
        ("f:1.5", [1]),
        ("f_5", []),  # a raw name that is not an f: token, even with no fact ids
        ("x + f:1", [1]),
        ("__f_1", []),
        ("f:1 + f:2", [1]),  # token not listed in fact_ids
        ("f:1", [1, 2]),  # listed id not used
        (f"f:{NEG}", [-NEG]),  # sign matters when comparing ids
        ("f:1 if f:2 else f:3", [1, 2, 3]),
        ("'a'", []),
        ("f:١٢٣", [123]),  # Arabic-Indic digits are not an f: token
        ("f:１２３", [123]),  # fullwidth digits
        ("f:1e3", [1]),  # a name glued to a token
        (f"f:{2**63}", [2**63]),  # outside signed 64 bits (a bigint lookup would overflow)
        (f"f:{-(2**63) - 1}", [-(2**63) - 1]),
        ("f:" + "9" * 5000, [10**4999]),  # absurdly long literal: invalid, not a crash
        ("f_5 + f:5", [5]),
    ],
)
def test_invalid_exprs(expr: str, ids: list[int]) -> None:
    with pytest.raises(ValidationError):
        _ok(expr, ids)


def test_answer_with_negative_fact_id_round_trips() -> None:
    payload = {
        "answer_markdown": "Capex was $11.8 million [1].",
        "claims": [
            {
                "id": "c1",
                "kind": "numeric",
                "text": "Capex was $11.8 million.",
                "value": "11800000",
                "unit": "USD",
                "primary": True,
                "provenance": {"type": "expr", "expr": f"f:{NEG}", "fact_ids": [NEG]},
            }
        ],
    }
    answer = Answer.model_validate(payload)
    assert Answer.model_validate_json(answer.model_dump_json()) == answer


def test_every_numeric_gold_fact_id_is_citable() -> None:
    """Regression for the real data: every gold fact id, signed, passes the grammar."""
    path = Path(__file__).resolve().parents[3] / "eval" / "sets" / "numeric_auto.jsonl"
    ids = [i for line in path.read_text().splitlines() for i in json.loads(line)["gold_fact_ids"]]
    assert any(i < 0 for i in ids)
    for i in ids:
        _ok(f"f:{i}", [i])


def test_answer_json_schema_is_unchanged_by_the_grammar() -> None:
    """The cassette key hashes `Answer.model_json_schema()`; widening the expr grammar must not
    move it (that would invalidate every banked baseline cassette). The grammar lives in a
    validator, not in the schema."""
    schema = json.dumps(Answer.model_json_schema(), sort_keys=True)
    # Measured 2026-11-25 on the schema the M4 baseline cassettes were recorded against. A change
    # here means every banked cassette misses (re-recording costs real money): decide it on purpose.
    assert hashlib.sha256(schema.encode()).hexdigest() == PINNED_ANSWER_SCHEMA_SHA256
