"""The request timeout is a client parameter: a ~150K-token long-context prompt needs more
than the 120 s default, and a timed-out attempt is billed server-side but carries no Usage."""

from __future__ import annotations

from typing import Any

import pytest

from app.llm import client as client_mod
from app.llm.client import REQUEST_TIMEOUT_S, LLMClient
from app.llm.types import LLMMode


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake(**kw: Any) -> object:
        seen.update(kw)
        return object()

    monkeypatch.setenv(client_mod.KEY_ENV, "test-key")
    monkeypatch.setattr(client_mod.anthropic, "Anthropic", fake)
    return seen


def test_default_timeout_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    LLMClient(mode=LLMMode.live)
    assert seen["timeout"] == REQUEST_TIMEOUT_S == 120.0 and seen["max_retries"] == 0


def test_timeout_can_be_raised_for_long_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    c = LLMClient(mode=LLMMode.record, request_timeout_s=600.0)
    assert seen["timeout"] == 600.0 and c.request_timeout_s == 600.0
