"""Test doubles for the Anthropic SDK boundary (the only thing faked in tests/unit/llm).

``FakeSDK`` mimics ``anthropic.Anthropic``: ``sdk.messages.create(**kwargs)`` returns an object
with ``.content`` (text blocks), ``.usage`` and ``.stop_reason``. Items queued in ``script`` are
returned in order; exception instances are raised instead. Every call's kwargs are recorded in
``sdk.calls`` so tests can assert on the exact request the client built.

``anthropic`` 2.9.0 builds its API errors on ``httpx2`` (not ``httpx``), so errors are
constructed with an ``httpx2.Response``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
from pydantic import BaseModel


class Answer(BaseModel):
    """Schema used by most client tests (required fields, no defaults)."""

    value: int
    label: str


class DefaultedAnswer(BaseModel):
    """Schema whose every field has a default (so a stub built with model_construct validates)."""

    value: int = 0
    label: str = ""


SYSTEM = "You are a planner. Answer in JSON."
MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "What was FY2023 revenue?"}]
GOOD = Answer(value=42, label="revenue")


def make_response(
    payload: BaseModel | str = GOOD,
    *,
    input_tokens: int = 1200,
    output_tokens: int = 80,
    cache_creation_input_tokens: int = 3000,
    cache_read_input_tokens: int = 500,
    stop_reason: str = "end_turn",
) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else payload.model_dump_json()
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        ),
        stop_reason=stop_reason,
    )


def api_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    """Build a real ``anthropic`` status error with the given HTTP status."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    return cls(f"HTTP {status}", response=response, body=None)


class _Messages:
    def __init__(self, owner: FakeSDK) -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> Any:
        self._owner.calls.append(kwargs)
        if not self._owner.script:
            raise AssertionError("FakeSDK script exhausted: client made more calls than expected")
        item = self._owner.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeSDK:
    def __init__(self, script: list[Any] | None = None) -> None:
        self.script: list[Any] = list(script) if script is not None else [make_response()]
        self.calls: list[dict[str, Any]] = []
        self.messages = _Messages(self)


class ExplodingSDK:
    """An SDK that must never be called (replay and stub modes)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._boom)

    def _boom(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        raise AssertionError("the SDK must not be called in this mode")


class Sleeper:
    """Injected ``sleep`` callable: records requested delays, never sleeps."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
