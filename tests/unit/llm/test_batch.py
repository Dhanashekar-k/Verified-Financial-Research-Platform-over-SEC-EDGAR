"""Batch mode: queue misses, submit one Message Batch, poll, collect by custom_id into cassettes,
then serve the rerun from cassettes. Only the SDK boundary is faked."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm.client import BatchPending, LLMClient
from app.llm.prices import cost_usd
from app.llm.types import LLMMode

from .fakes import GOOD, MESSAGES, SYSTEM, Answer, Sleeper, make_response


class FakeBatches:
    def __init__(self, polls_until_ended: int = 2, results: dict[str, Any] | None = None) -> None:
        self.created: list[list[dict[str, Any]]] = []
        self.polls_until_ended = polls_until_ended
        self.retrieves = 0
        self.overrides = results or {}

    def create(self, *, requests: list[dict[str, Any]]) -> SimpleNamespace:
        self.created.append(requests)
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}")

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        self.retrieves += 1
        done = self.retrieves >= self.polls_until_ended
        return SimpleNamespace(processing_status="ended" if done else "in_progress")

    def results(self, batch_id: str) -> list[SimpleNamespace]:
        requests = self.created[-1]
        out = []
        # Reverse order: results arrive in any order and must be matched by custom_id.
        for req in reversed(requests):
            cid = req["custom_id"]
            if cid in self.overrides:
                result = self.overrides[cid]
            else:
                result = SimpleNamespace(type="succeeded", message=make_response())
            out.append(SimpleNamespace(custom_id=cid, result=result))
        return out


class FakeBatchSDK:
    def __init__(self, batches: FakeBatches) -> None:
        self.messages = SimpleNamespace(batches=batches, create=self._no_sync)

    def _no_sync(self, **kwargs: Any) -> Any:
        raise AssertionError("batch mode must not make synchronous calls")


def _client(tmp_path: Path, batches: FakeBatches) -> LLMClient:
    return LLMClient(
        mode=LLMMode.batch, cassette_dir=tmp_path, sdk_client=FakeBatchSDK(batches), sleep=Sleeper()
    )


def _ask(client: LLMClient, text: str) -> tuple[Answer, Any]:
    return client.structured("planner", SYSTEM, [{"role": "user", "content": text}], Answer)


def test_miss_queues_and_raises_pending(tmp_path: Path) -> None:
    client = _client(tmp_path, FakeBatches())
    with pytest.raises(BatchPending) as info:
        _ask(client, "q1")
    assert client.pending == 1
    assert info.value.key == client.request_key(
        "planner", SYSTEM, [{"role": "user", "content": "q1"}], Answer
    )


def test_round_trip_collects_into_cassettes_and_rerun_serves_them(tmp_path: Path) -> None:
    batches = FakeBatches(polls_until_ended=3)
    client = _client(tmp_path, batches)
    for q in ("q1", "q2", "q3"):
        with pytest.raises(BatchPending):
            _ask(client, q)
    report = client.run_pending_batch(poll_interval_s=10)
    assert (report.submitted, report.succeeded, report.failed) == (3, 3, {})
    assert len(batches.created) == 1 and len(batches.created[0]) == 3
    assert client.pending == 0
    obj, usage = _ask(client, "q2")
    assert obj == GOOD
    assert usage.batch is True
    # Spent in this run, at the batch (50% off) price.
    assert usage.cost_usd == cost_usd("claude-haiku-5-5", 1200, 3000, 500, 80, batch=True)


def test_custom_id_is_the_request_key_and_params_match_sync_shape(tmp_path: Path) -> None:
    batches = FakeBatches()
    client = _client(tmp_path, batches)
    with pytest.raises(BatchPending):
        client.structured("planner", SYSTEM, MESSAGES, Answer)
    client.run_pending_batch()
    req = batches.created[0][0]
    assert req["custom_id"] == client.request_key("planner", SYSTEM, MESSAGES, Answer)
    assert len(req["custom_id"]) == 64
    params = req["params"]
    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_failed_entries_are_reported_and_not_cassetted(tmp_path: Path) -> None:
    client = _client(tmp_path, FakeBatches())
    keys = []
    for q in ("ok", "err", "trunc"):
        with pytest.raises(BatchPending) as info:
            _ask(client, q)
        keys.append(info.value.key)
    batches = FakeBatches(
        results={
            keys[1]: SimpleNamespace(type="errored"),
            keys[2]: SimpleNamespace(
                type="succeeded", message=make_response(stop_reason="max_tokens")
            ),
        }
    )
    client._sdk = FakeBatchSDK(batches)  # same queue, fresh fake
    report = client.run_pending_batch()
    assert report.succeeded == 1
    assert set(report.failed) == {keys[1], keys[2]}
    assert report.failed[keys[1]] == "errored"
    assert not (tmp_path / f"{keys[1]}.json").exists()
    assert not (tmp_path / f"{keys[2]}.json").exists()
    # The truncated result was billed: it is counted, not lost.
    assert client.failed_usage_usd == cost_usd("claude-haiku-5-5", 1200, 3000, 500, 80, batch=True)
    with pytest.raises(BatchPending):  # a rerun re-queues the failures
        _ask(client, "err")


def test_cassette_from_a_previous_run_costs_zero(tmp_path: Path) -> None:
    first = _client(tmp_path, FakeBatches())
    with pytest.raises(BatchPending):
        _ask(first, "q1")
    first.run_pending_batch()
    later = _client(tmp_path, FakeBatches())
    _, usage = _ask(later, "q1")
    assert usage.cost_usd == Decimal(0)
    assert later.pending == 0


def test_empty_queue_submits_nothing(tmp_path: Path) -> None:
    batches = FakeBatches()
    report = _client(tmp_path, batches).run_pending_batch()
    assert report.submitted == 0 and batches.created == []


def test_poll_timeout_raises(tmp_path: Path) -> None:
    client = _client(tmp_path, FakeBatches(polls_until_ended=10**6))
    with pytest.raises(BatchPending):
        _ask(client, "q1")
    with pytest.raises(TimeoutError):
        client.run_pending_batch(poll_interval_s=60, timeout_s=300)


def test_batch_requires_api_key_without_injected_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDGARQA_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="EDGARQA_ANTHROPIC_API_KEY"):
        LLMClient(mode=LLMMode.batch)


# --- review fixes: resume, idempotency, accounting, attempt cap ----------------------------


def _queue(client: LLMClient, *texts: str) -> list[str]:
    keys = []
    for t in texts:
        with pytest.raises(BatchPending) as info:
            _ask(client, t)
        keys.append(info.value.key)
    return keys


def test_entry_missing_from_results_is_reported(tmp_path: Path) -> None:
    class Dropping(FakeBatches):
        def results(self, batch_id: str) -> list[SimpleNamespace]:
            return super().results(batch_id)[1:]  # drop one entry

    client = _client(tmp_path, Dropping())
    keys = _queue(client, "a", "b")
    report = client.run_pending_batch()
    assert report.succeeded + len(report.failed) == report.submitted == 2
    assert list(report.failed.values()) == ["no result returned"]
    assert set(report.failed) < set(keys)


def test_same_request_twice_in_a_round_is_one_batch_entry(tmp_path: Path) -> None:
    batches = FakeBatches()
    client = _client(tmp_path, batches)
    _queue(client, "same", "same")
    client.run_pending_batch()
    assert len(batches.created[0]) == 1


def test_results_for_unqueued_ids_are_ignored(tmp_path: Path) -> None:
    class Extra(FakeBatches):
        def results(self, batch_id: str) -> list[SimpleNamespace]:
            stranger = SimpleNamespace(
                custom_id="f" * 64,
                result=SimpleNamespace(type="succeeded", message=make_response()),
            )
            return [stranger, *super().results(batch_id)]

    client = _client(tmp_path, Extra())
    _queue(client, "a")
    report = client.run_pending_batch()
    assert report.succeeded == 1
    assert not (tmp_path / f"{'f' * 64}.json").exists()


@pytest.mark.parametrize("kind", ["expired", "canceled"])
def test_expired_and_canceled_results_fail(tmp_path: Path, kind: str) -> None:
    probe = _client(tmp_path, FakeBatches())
    (key,) = _queue(probe, "a")
    client = _client(tmp_path, FakeBatches(results={key: SimpleNamespace(type=kind)}))
    _queue(client, "a")
    report = client.run_pending_batch()
    assert report.failed == {key: kind}


def test_poll_error_then_rerun_resumes_the_saved_batch_without_paying_again(
    tmp_path: Path,
) -> None:
    from .fakes import api_error

    class Flaky(FakeBatches):
        def __init__(self) -> None:
            super().__init__(polls_until_ended=1)
            self.fail_next = 10  # more failures than max_retries

        def retrieve(self, batch_id: str) -> SimpleNamespace:
            if self.fail_next:
                self.fail_next -= 1
                import anthropic

                raise api_error(anthropic.InternalServerError, 503)
            return super().retrieve(batch_id)

    batches = Flaky()
    client = _client(tmp_path, batches)
    _queue(client, "a", "b")
    import anthropic

    with pytest.raises(anthropic.InternalServerError):
        client.run_pending_batch()
    assert len(list(tmp_path.glob(".batch_*.json"))) == 1  # saved before polling
    batches.fail_next = 0
    report = client.run_pending_batch()
    assert len(batches.created) == 1  # resumed, not resubmitted
    assert report.succeeded == 2
    assert list(tmp_path.glob(".batch_*.json")) == []  # cleared once collected


def test_transient_poll_errors_are_retried(tmp_path: Path) -> None:
    import anthropic

    from .fakes import api_error

    class OneBlip(FakeBatches):
        blipped = False

        def retrieve(self, batch_id: str) -> SimpleNamespace:
            if not self.blipped:
                self.blipped = True
                raise api_error(anthropic.InternalServerError, 529)
            return super().retrieve(batch_id)

    client = _client(tmp_path, OneBlip(polls_until_ended=1))
    _queue(client, "a")
    assert client.run_pending_batch().succeeded == 1


def test_cassetted_keys_are_never_resubmitted(tmp_path: Path) -> None:
    first = _client(tmp_path, FakeBatches())
    _queue(first, "a")
    first.run_pending_batch()
    batches = FakeBatches()
    second = _client(tmp_path, batches)
    _queue(second, "b")
    second._pending.update(first._pending)  # nothing left there; also queue "a" by hand:
    key_a = first.request_key("planner", SYSTEM, [{"role": "user", "content": "a"}], Answer)
    second._pending[key_a] = ("claude-haiku-5-5", {}, Answer)
    second.run_pending_batch()
    assert [r["custom_id"] for r in batches.created[0]] != [] and key_a not in {
        r["custom_id"] for r in batches.created[0]
    }


def test_batch_cost_is_reported_on_first_serve_only(tmp_path: Path) -> None:
    client = _client(tmp_path, FakeBatches())
    _queue(client, "shared")
    client.run_pending_batch()
    _, first = _ask(client, "shared")
    _, second = _ask(client, "shared")
    assert first.cost_usd > 0
    assert second.cost_usd == Decimal(0)


def test_repeated_failures_give_up_instead_of_rebilling(tmp_path: Path) -> None:
    from app.llm.client import BatchGaveUp

    probe = _client(tmp_path, FakeBatches())
    (key,) = _queue(probe, "trunc")
    truncated = SimpleNamespace(type="succeeded", message=make_response(stop_reason="max_tokens"))
    client = _client(tmp_path, FakeBatches(results={key: truncated}))
    for _ in range(2):  # max_batch_attempts default
        _queue(client, "trunc")
        client.run_pending_batch()
    with pytest.raises(BatchGaveUp, match="max_tokens"):
        _ask(client, "trunc")
    assert client.failed_usage_usd == 2 * cost_usd(
        "claude-haiku-5-5", 1200, 3000, 500, 80, batch=True
    )


def test_oversized_queue_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm import client as client_mod

    monkeypatch.setattr(client_mod, "MAX_BATCH_REQUESTS", 1)
    batches = FakeBatches()
    client = _client(tmp_path, batches)
    _queue(client, "a", "b")
    with pytest.raises(ValueError, match="batch limit"):
        client.run_pending_batch()
    assert batches.created == []
