"""Contract tests for ``app/llm/types.py`` (see the interface contract in test_client_core.py)."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

import pytest
from pydantic import ValidationError

from app.llm.types import LLMMode, Usage


def _usage(**over: object) -> Usage:
    base: dict[str, object] = {
        "model": "claude-haiku-5-5",
        "input_tokens": 1200,
        "cache_write_tokens": 3000,
        "cache_read_tokens": 500,
        "output_tokens": 80,
        "cost_usd": Decimal("0.0012345"),
        "latency_ms": 321,
    }
    base.update(over)
    return Usage(**base)  # type: ignore[arg-type]


def test_llm_mode_has_exactly_the_five_modes_as_strings() -> None:
    """Modes are a StrEnum so EDGARQA_LLM_MODE values map 1:1 (live/stub/record/replay/batch)."""
    assert issubclass(LLMMode, StrEnum)
    assert {m.value for m in LLMMode} == {"live", "stub", "record", "replay", "batch"}
    assert LLMMode("replay") is LLMMode.replay
    assert LLMMode.batch == "batch"


def test_llm_mode_rejects_unknown_value() -> None:
    """A typo in EDGARQA_LLM_MODE must fail loudly, not silently fall back to live (costs money)."""
    with pytest.raises(ValueError):
        LLMMode("lvie")


def test_usage_fields_and_batch_default_false() -> None:
    """Usage carries the eight contract fields; ``batch`` defaults to False."""
    u = _usage()
    assert u.model == "claude-haiku-5-5"
    assert (u.input_tokens, u.cache_write_tokens, u.cache_read_tokens, u.output_tokens) == (
        1200,
        3000,
        500,
        80,
    )
    assert u.latency_ms == 321
    assert u.batch is False
    assert _usage(batch=True).batch is True


def test_usage_cost_is_decimal_not_float() -> None:
    """CLAUDE.md: money is Decimal. A Decimal passed in must stay an exact Decimal."""
    u = _usage(cost_usd=Decimal("0.0500005"))
    assert isinstance(u.cost_usd, Decimal)
    assert u.cost_usd == Decimal("0.0500005")


def test_usage_json_round_trip_preserves_decimal_exactly() -> None:
    """Usage is written into cassettes and results JSON; the round trip must not lose precision."""
    u = _usage(cost_usd=Decimal("0.0500005"), batch=True)
    again = Usage.model_validate_json(u.model_dump_json())
    assert again == u
    assert again.cost_usd == Decimal("0.0500005")


@pytest.mark.parametrize(
    "missing",
    ["model", "input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens",
     "cost_usd", "latency_ms"],
)  # fmt: skip
def test_usage_requires_every_non_default_field(missing: str) -> None:
    """No silent zeros: omitting any accounting field is a validation error."""
    data: dict[str, object] = {
        "model": "claude-haiku-5-5",
        "input_tokens": 1,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 1,
        "cost_usd": Decimal("0"),
        "latency_ms": 1,
    }
    del data[missing]
    with pytest.raises(ValidationError):
        Usage(**data)  # type: ignore[arg-type]
