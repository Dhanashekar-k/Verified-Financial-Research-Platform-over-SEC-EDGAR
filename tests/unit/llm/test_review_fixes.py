"""Behavior added after the step-1 review: schema transform, effort pinning, transport retries,
billed-failure accounting, pre-call price check, and request-shape versioning of cassettes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from app.llm import client as client_mod
from app.llm.client import EFFORT, LLMClient, LLMResponseError
from app.llm.prices import cost_usd
from app.llm.types import LLMMode

from .fakes import MESSAGES, SYSTEM, FakeSDK, Sleeper, make_response


class Inner(BaseModel):
    a: int


class Nested(BaseModel):
    x: str
    items: list[Inner]
    w: int = 3


def _objects(node: Any) -> list[dict[str, Any]]:
    """Every JSON-schema object node, including those under $defs."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found.extend(_objects(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_objects(value))
    return found


def _live(sdk: Any, sleeper: Sleeper | None = None, **kw: Any) -> LLMClient:
    return LLMClient(mode=LLMMode.live, sdk_client=sdk, sleep=sleeper or Sleeper(), **kw)


def test_sent_schema_closes_every_object_including_defs() -> None:
    payload = Nested(x="q", items=[Inner(a=1)])
    sdk = FakeSDK([make_response(payload)])
    _live(sdk).structured("planner", SYSTEM, MESSAGES, Nested)
    schema = sdk.calls[0]["output_config"]["format"]["schema"]
    objects = _objects(schema)
    assert len(objects) >= 2  # root and $defs/Inner
    assert all(o.get("additionalProperties") is False for o in objects)


@pytest.mark.parametrize("role", ["planner", "composer"])
def test_effort_is_pinned_per_model(role: str) -> None:
    sdk = FakeSDK()
    from .fakes import Answer

    _live(sdk).structured(role, SYSTEM, MESSAGES, Answer)
    model = sdk.calls[0]["model"]
    assert sdk.calls[0]["output_config"]["effort"] == EFFORT[model]


def _conn_error() -> anthropic.APIConnectionError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(request=request)


def _timeout_error() -> anthropic.APITimeoutError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APITimeoutError(request=request)


def test_connection_and_timeout_errors_are_retried() -> None:
    from .fakes import Answer

    sleeper = Sleeper()
    sdk = FakeSDK([_conn_error(), _timeout_error(), make_response()])
    obj, _ = _live(sdk, sleeper).structured("planner", SYSTEM, MESSAGES, Answer)
    assert isinstance(obj, Answer)
    assert len(sdk.calls) == 3
    assert len(sleeper.delays) == 2


def test_retry_after_header_is_honored_when_longer_than_backoff() -> None:
    from .fakes import Answer

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(429, request=request, headers={"retry-after": "30"})
    err = anthropic.RateLimitError("HTTP 429", response=response, body=None)
    sleeper = Sleeper()
    _live(FakeSDK([err, make_response()]), sleeper).structured("planner", SYSTEM, MESSAGES, Answer)
    assert sleeper.delays == [30.0]


@pytest.mark.parametrize("payload,stop", [("{}", "end_turn"), ('{"value": 1', "max_tokens")])
def test_billed_failures_carry_their_usage(payload: str, stop: str) -> None:
    """A truncated or schema-invalid reply was billed: the error carries the cost."""
    from .fakes import Answer

    sdk = FakeSDK([make_response(payload, stop_reason=stop, input_tokens=900, output_tokens=40)])
    with pytest.raises(LLMResponseError) as info:
        _live(sdk).structured("planner", SYSTEM, MESSAGES, Answer)
    usage = info.value.usage
    assert (usage.input_tokens, usage.output_tokens) == (900, 40)
    assert usage.cost_usd == cost_usd("claude-haiku-5-5", 900, 3000, 500, 40)


def test_unpriced_model_fails_before_any_call() -> None:
    from .fakes import Answer, ExplodingSDK

    sdk = ExplodingSDK()
    with pytest.raises(KeyError, match="no price"):
        _live(sdk).structured("claude-unpriced-9", SYSTEM, MESSAGES, Answer)
    assert sdk.calls == []


def test_request_shape_version_is_part_of_the_cassette_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from .fakes import Answer

    client = LLMClient(mode=LLMMode.replay, cassette_dir=tmp_path)
    before = client.request_key("planner", SYSTEM, MESSAGES, Answer)
    monkeypatch.setattr(client_mod, "REQUEST_SHAPE_VERSION", client_mod.REQUEST_SHAPE_VERSION + 1)
    assert client.request_key("planner", SYSTEM, MESSAGES, Answer) != before


def test_live_client_pins_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGARQA_ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    client = LLMClient(mode=LLMMode.live)
    assert str(client._sdk.base_url).rstrip("/") == "https://api.anthropic.com"
