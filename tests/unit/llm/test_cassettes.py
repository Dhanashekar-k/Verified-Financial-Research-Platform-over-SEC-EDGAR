"""Record / replay behavior of ``LLMClient`` (contract: see test_client_core.py).

Cassettes make eval reruns free: ``record`` pays once, ``replay`` serves the stored response for
the same request hash and must never reach the SDK.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.llm.client import CassetteMissError, LLMClient
from app.llm.types import LLMMode

from .fakes import (
    GOOD,
    MESSAGES,
    SYSTEM,
    Answer,
    ExplodingSDK,
    FakeSDK,
    Sleeper,
    make_response,
)

MODEL = "claude-haiku-5-5"


def _record_client(tmp_path: Path, sdk: FakeSDK) -> LLMClient:
    return LLMClient(mode=LLMMode.record, cassette_dir=tmp_path, sdk_client=sdk, sleep=Sleeper())


def _replay_client(tmp_path: Path, sdk: ExplodingSDK | None = None) -> LLMClient:
    return LLMClient(mode=LLMMode.replay, cassette_dir=tmp_path, sdk_client=sdk or ExplodingSDK())


def test_record_writes_cassette_named_by_request_key(tmp_path: Path) -> None:
    """record persists exactly one <request_key>.json, and the file is valid JSON."""
    client = _record_client(tmp_path, FakeSDK())
    client.structured(MODEL, SYSTEM, MESSAGES, Answer)
    key = client.request_key(MODEL, SYSTEM, MESSAGES, Answer)
    files = list(tmp_path.glob("*.json"))
    assert [f.name for f in files] == [f"{key}.json"]
    json.loads(files[0].read_text())


def test_record_role_call_is_keyed_by_the_resolved_model(tmp_path: Path) -> None:
    """A call made with role 'planner' lands at the key of claude-haiku-5-5."""
    client = _record_client(tmp_path, FakeSDK())
    client.structured("planner", SYSTEM, MESSAGES, Answer)
    key = client.request_key("claude-haiku-5-5", SYSTEM, MESSAGES, Answer)
    assert (tmp_path / f"{key}.json").exists()


def test_record_creates_missing_cassette_dir(tmp_path: Path) -> None:
    """The first record run into a fresh eval/cassettes/ must not fail on a missing directory."""
    target = tmp_path / "nested" / "cassettes"
    client = _record_client(target, FakeSDK())
    client.structured(MODEL, SYSTEM, MESSAGES, Answer)
    assert len(list(target.glob("*.json"))) == 1


def test_record_then_replay_returns_same_object(tmp_path: Path) -> None:
    """The point of cassettes: a replay client on the same dir returns the recorded object."""
    recorded, _ = _record_client(tmp_path, FakeSDK()).structured(MODEL, SYSTEM, MESSAGES, Answer)
    replayed, _ = _replay_client(tmp_path).structured(MODEL, SYSTEM, MESSAGES, Answer)
    assert isinstance(replayed, Answer)
    assert replayed == recorded == GOOD


def test_replay_never_calls_the_sdk(tmp_path: Path) -> None:
    """Replay costs $0: neither a hit nor a miss may reach the SDK."""
    _record_client(tmp_path, FakeSDK()).structured(MODEL, SYSTEM, MESSAGES, Answer)
    sdk = ExplodingSDK()
    client = _replay_client(tmp_path, sdk)
    client.structured(MODEL, SYSTEM, MESSAGES, Answer)
    with pytest.raises(CassetteMissError):
        client.structured(MODEL, SYSTEM, [{"role": "user", "content": "other"}], Answer)
    assert sdk.calls == []


def test_replay_twice_is_deterministic(tmp_path: Path) -> None:
    """Same request twice -> equal objects and equal Usage (scoring reruns must be reproducible)."""
    _record_client(tmp_path, FakeSDK()).structured(MODEL, SYSTEM, MESSAGES, Answer)
    client = _replay_client(tmp_path)
    obj1, usage1 = client.structured(MODEL, SYSTEM, MESSAGES, Answer)
    obj2, usage2 = client.structured(MODEL, SYSTEM, MESSAGES, Answer)
    assert obj1 == obj2
    assert usage1 == usage2


def test_replay_is_reproducible_across_client_instances(tmp_path: Path) -> None:
    """A fresh process (fresh client) replays the same answer."""
    _record_client(tmp_path, FakeSDK()).structured(MODEL, SYSTEM, MESSAGES, Answer)
    a = _replay_client(tmp_path).structured(MODEL, SYSTEM, MESSAGES, Answer)
    b = _replay_client(tmp_path).structured(MODEL, SYSTEM, MESSAGES, Answer)
    assert a == b


def test_replay_miss_raises_cassette_miss_with_key(tmp_path: Path) -> None:
    """No cassette -> CassetteMissError naming the key, so the missing request is findable."""
    client = _replay_client(tmp_path)
    key = client.request_key(MODEL, SYSTEM, MESSAGES, Answer)
    with pytest.raises(CassetteMissError, match=key):
        client.structured(MODEL, SYSTEM, MESSAGES, Answer)


def test_replay_miss_when_directory_does_not_exist(tmp_path: Path) -> None:
    """A nonexistent cassette dir is a miss (CassetteMissError), not a FileNotFoundError."""
    client = _replay_client(tmp_path / "nope")
    with pytest.raises(CassetteMissError):
        client.structured(MODEL, SYSTEM, MESSAGES, Answer)


@pytest.mark.parametrize(
    "change",
    [
        {"system": SYSTEM + " v2"},
        {"messages": [{"role": "user", "content": "different"}]},
        {"max_tokens": 1024},
        {"model": "claude-sonnet-5-5"},
    ],
    ids=["system", "messages", "max_tokens", "model"],
)
def test_changing_any_request_component_misses(
    tmp_path: Path,
    change: dict,  # type: ignore[type-arg]
) -> None:
    """A cassette recorded for one request must not be served for a different one.

    Serving a stale answer after a prompt edit would silently score the wrong prompt.
    """
    _record_client(tmp_path, FakeSDK()).structured(MODEL, SYSTEM, MESSAGES, Answer)
    request = {"model": MODEL, "system": SYSTEM, "messages": MESSAGES, "max_tokens": 2048}
    request.update(change)
    with pytest.raises(CassetteMissError):
        _replay_client(tmp_path).structured(
            request["model"],  # type: ignore[arg-type]
            request["system"],  # type: ignore[arg-type]
            request["messages"],  # type: ignore[arg-type]
            Answer,
            max_tokens=request["max_tokens"],  # type: ignore[arg-type]
        )


def test_replay_does_not_write_files(tmp_path: Path) -> None:
    """Replay is read-only (an eval rerun must not mutate committed cassettes)."""
    _record_client(tmp_path, FakeSDK()).structured(MODEL, SYSTEM, MESSAGES, Answer)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    _replay_client(tmp_path).structured(MODEL, SYSTEM, MESSAGES, Answer)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_record_keeps_recorded_usage_numbers_available(tmp_path: Path) -> None:
    """Recording stores the original token counts so cost of the recorded run can be audited.

    Replay's own cost is not pinned here; only that the recorded counts survive in the replay.
    """
    sdk = FakeSDK([make_response(input_tokens=777, output_tokens=33)])
    _record_client(tmp_path, sdk).structured(MODEL, SYSTEM, MESSAGES, Answer)
    _, usage = _replay_client(tmp_path).structured(MODEL, SYSTEM, MESSAGES, Answer)
    assert (usage.input_tokens, usage.output_tokens) == (777, 33)
    assert usage.model == MODEL


def test_truncated_response_is_not_recorded(tmp_path: Path) -> None:
    """A max_tokens-truncated response raises and must NOT poison the cassette dir."""
    sdk = FakeSDK([make_response('{"value": 4', stop_reason="max_tokens")])
    with pytest.raises((ValueError, RuntimeError)):
        _record_client(tmp_path, sdk).structured(MODEL, SYSTEM, MESSAGES, Answer)
    assert list(tmp_path.glob("*.json")) == []


def test_unparseable_response_is_not_recorded(tmp_path: Path) -> None:
    """A response that fails schema validation raises and is not written to a cassette."""
    sdk = FakeSDK([make_response("not json at all")])
    with pytest.raises((ValueError, RuntimeError)):
        _record_client(tmp_path, sdk).structured(MODEL, SYSTEM, MESSAGES, Answer)
    assert list(tmp_path.glob("*.json")) == []
