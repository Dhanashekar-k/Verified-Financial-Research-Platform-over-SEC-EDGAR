"""Retry/backoff behavior of ``LLMClient`` (contract: see test_client_core.py).

Real ``anthropic`` exception classes are raised by the fake SDK. ``sleep`` is injected so no test
actually waits. ``max_retries=N`` means up to N retries after the first attempt (N+1 SDK calls).
"""

from __future__ import annotations

from pathlib import Path

import anthropic
import pytest

from app.llm.client import LLMClient
from app.llm.types import LLMMode

from .fakes import (
    GOOD,
    MESSAGES,
    SYSTEM,
    Answer,
    FakeSDK,
    Sleeper,
    api_error,
    make_response,
)

RETRYABLE = [
    (anthropic.RateLimitError, 429),
    (anthropic.InternalServerError, 500),
    (anthropic.InternalServerError, 502),
    (anthropic.InternalServerError, 503),
    (anthropic.InternalServerError, 529),
]
NON_RETRYABLE = [
    (anthropic.BadRequestError, 400),
    (anthropic.AuthenticationError, 401),
    (anthropic.PermissionDeniedError, 403),
    (anthropic.NotFoundError, 404),
    (anthropic.UnprocessableEntityError, 422),
]


def _client(sdk: FakeSDK, sleep: Sleeper, tmp_path: Path, *, max_retries: int = 4) -> LLMClient:
    return LLMClient(
        mode=LLMMode.live,
        cassette_dir=tmp_path,
        sdk_client=sdk,
        sleep=sleep,
        max_retries=max_retries,
    )


@pytest.mark.parametrize(("cls", "status"), RETRYABLE)
def test_retryable_error_then_success(
    cls: type[anthropic.APIStatusError], status: int, tmp_path: Path
) -> None:
    """429, 529 and 5xx are transient: one failure then success returns the object."""
    sdk = FakeSDK([api_error(cls, status), make_response()])
    sleeper = Sleeper()
    obj, _ = _client(sdk, sleeper, tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)
    assert obj == GOOD
    assert len(sdk.calls) == 2
    assert len(sleeper.delays) == 1
    assert sleeper.delays[0] > 0


def test_backoff_delays_are_exponential(tmp_path: Path) -> None:
    """Three failures then success: three positive, strictly increasing delays."""
    errors = [api_error(anthropic.RateLimitError, 429) for _ in range(3)]
    sdk = FakeSDK([*errors, make_response()])
    sleeper = Sleeper()
    _client(sdk, sleeper, tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)
    assert len(sleeper.delays) == 3
    assert all(d > 0 for d in sleeper.delays)
    assert sleeper.delays[0] < sleeper.delays[1] < sleeper.delays[2]


def test_mixed_retryable_errors_are_all_retried(tmp_path: Path) -> None:
    """429 then 529 then 500 are all retryable in sequence."""
    sdk = FakeSDK(
        [
            api_error(anthropic.RateLimitError, 429),
            api_error(anthropic.InternalServerError, 529),
            api_error(anthropic.InternalServerError, 500),
            make_response(),
        ]
    )
    sleeper = Sleeper()
    obj, _ = _client(sdk, sleeper, tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)
    assert obj == GOOD
    assert len(sleeper.delays) == 3


def test_gives_up_after_max_retries_and_reraises_last_error(tmp_path: Path) -> None:
    """max_retries=4 -> 5 SDK calls, 4 sleeps, then the last error propagates unchanged."""
    errors = [api_error(anthropic.InternalServerError, 503) for _ in range(5)]
    sdk = FakeSDK(list(errors))
    sleeper = Sleeper()
    with pytest.raises(anthropic.InternalServerError) as exc:
        _client(sdk, sleeper, tmp_path, max_retries=4).structured(
            "planner", SYSTEM, MESSAGES, Answer
        )
    assert exc.value is errors[-1]
    assert len(sdk.calls) == 5
    assert len(sleeper.delays) == 4


def test_max_retries_zero_means_a_single_attempt(tmp_path: Path) -> None:
    """With max_retries=0 a 429 is raised immediately with no sleep."""
    sdk = FakeSDK([api_error(anthropic.RateLimitError, 429)])
    sleeper = Sleeper()
    with pytest.raises(anthropic.RateLimitError):
        _client(sdk, sleeper, tmp_path, max_retries=0).structured(
            "planner", SYSTEM, MESSAGES, Answer
        )
    assert len(sdk.calls) == 1
    assert sleeper.delays == []


@pytest.mark.parametrize(("cls", "status"), NON_RETRYABLE)
def test_client_errors_are_not_retried(
    cls: type[anthropic.APIStatusError], status: int, tmp_path: Path
) -> None:
    """A 4xx other than 429 is a caller bug (bad schema, bad key): retrying wastes time/money."""
    err = api_error(cls, status)
    sdk = FakeSDK([err, make_response()])
    sleeper = Sleeper()
    with pytest.raises(cls) as exc:
        _client(sdk, sleeper, tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)
    assert exc.value is err
    assert len(sdk.calls) == 1
    assert sleeper.delays == []


def test_retried_requests_are_identical(tmp_path: Path) -> None:
    """Each retry resends the same request (same model, system block, output_config)."""
    sdk = FakeSDK([api_error(anthropic.RateLimitError, 429), make_response()])
    _client(sdk, Sleeper(), tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)
    assert sdk.calls[0] == sdk.calls[1]


def test_usage_after_retry_counts_only_the_successful_call(tmp_path: Path) -> None:
    """Failed attempts carry no usage; the returned Usage is the successful response's."""
    sdk = FakeSDK(
        [
            api_error(anthropic.InternalServerError, 529),
            make_response(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        ]
    )
    _, usage = _client(sdk, Sleeper(), tmp_path).structured("planner", SYSTEM, MESSAGES, Answer)
    assert (usage.input_tokens, usage.output_tokens) == (10, 5)


def test_record_mode_retries_and_writes_cassette_once_on_success(tmp_path: Path) -> None:
    """record uses the same retry path and writes exactly one cassette after the success."""
    sdk = FakeSDK([api_error(anthropic.RateLimitError, 429), make_response()])
    client = LLMClient(
        mode=LLMMode.record,
        cassette_dir=tmp_path,
        sdk_client=sdk,
        sleep=Sleeper(),
        max_retries=3,
    )
    client.structured("planner", SYSTEM, MESSAGES, Answer)
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_record_mode_writes_nothing_when_retries_exhausted(tmp_path: Path) -> None:
    """If every attempt fails there is nothing to record."""
    sdk = FakeSDK([api_error(anthropic.RateLimitError, 429) for _ in range(3)])
    client = LLMClient(
        mode=LLMMode.record,
        cassette_dir=tmp_path,
        sdk_client=sdk,
        sleep=Sleeper(),
        max_retries=2,
    )
    with pytest.raises(anthropic.RateLimitError):
        client.structured("planner", SYSTEM, MESSAGES, Answer)
    assert list(tmp_path.glob("*.json")) == []
