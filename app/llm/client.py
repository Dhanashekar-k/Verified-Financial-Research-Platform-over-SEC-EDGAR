"""The one LLM client: model registry, structured outputs, prompt caching, retries, accounting,
and the stub / record / replay modes (M04 section 3.1).

Structured outputs use ``output_config={"format": {"type": "json_schema", "schema": ...}}``.
The schema sent is ``anthropic.transform_schema`` of the Pydantic schema (what the SDK's own
``messages.parse`` sends: ``additionalProperties: false`` on every object, unsupported
constraints moved into descriptions); Pydantic validation of the reply is the final check.
The system prompt goes first as a block with a cache breakpoint, so the static instructions
are cached and the varying question comes after it. Haiku 5.5 and Sonnet 5.5 reject a
non-default ``temperature``, so none is sent. Effort is pinned per model (``EFFORT``) so cost
and caching don't drift with API defaults.

The API key comes from ``EDGARQA_ANTHROPIC_API_KEY`` only (CLAUDE.md rule 5). It is passed to
the SDK explicitly, which stops the SDK falling back to its own default key or token variables;
``base_url`` is pinned too, so a stray base-URL environment variable can't redirect the key.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from app.llm.prices import PRICES, cost_usd
from app.llm.stub import stub_response
from app.llm.types import LLMMode, Usage

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

KEY_ENV = "EDGARQA_ANTHROPIC_API_KEY"
MODE_ENV = "EDGARQA_LLM_MODE"
API_BASE_URL = "https://api.anthropic.com"

REGISTRY: dict[str, str] = {
    "planner": "claude-haiku-5-5",
    "composer": "claude-sonnet-5-5",
    "verifier": "claude-haiku-5-5",
    "judge": "claude-sonnet-5-5",
}

# Each model's documented default, made explicit (tuning effort is an M5 decision, measured).
EFFORT: dict[str, str] = {
    "claude-haiku-5-5": "medium",
    "claude-sonnet-5-5": "high",
    "claude-opus-5-5": "medium",
}

# Bump when the request shape changes, so cassettes recorded with an old shape miss.
REQUEST_SHAPE_VERSION = 2

DEFAULT_MAX_RETRIES = 4
BACKOFF_BASE_S = 1.0
MAX_BACKOFF_S = 60.0
REQUEST_TIMEOUT_S = 120.0
MAX_BATCH_REQUESTS = 100_000  # Message Batches limit (256 MB also applies)


class CassetteMissError(Exception):
    """Replay mode found no recorded response for a request."""


class BatchPending(Exception):
    """Batch mode queued this request; rerun the caller after run_pending_batch()."""

    def __init__(self, key: str) -> None:
        super().__init__(f"request {key} queued for the next batch")
        self.key = key


@dataclass(frozen=True)
class BatchReport:
    batch_id: str | None
    submitted: int
    succeeded: int
    failed: dict[str, str]  # request key -> reason (errored, expired, truncated, invalid...)


class BatchGaveUp(RuntimeError):
    """A request failed in ``max_batch_attempts`` batches; it is not queued (and billed) again."""

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"request {key} failed in batch repeatedly: {reason}")
        self.key = key
        self.reason = reason


class LLMResponseError(RuntimeError):
    """The response was truncated, refused, or did not match the schema.

    The API billed it anyway, so ``usage`` carries its cost for the harness to account.
    """

    def __init__(self, message: str, usage: Usage) -> None:
        super().__init__(message)
        self.usage = usage


def _retryable(err: Exception) -> bool:
    if isinstance(err, anthropic.APIConnectionError):  # includes APITimeoutError
        return True
    if isinstance(err, anthropic.APIStatusError):
        return err.status_code in (408, 409, 429) or err.status_code >= 500
    return False


def _retry_after_s(err: Exception) -> float | None:
    response = getattr(err, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class LLMClient:
    def __init__(
        self,
        mode: LLMMode | None = None,
        cassette_dir: Path = Path("eval/cassettes"),
        registry: dict[str, str] | None = None,
        sdk_client: Any = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_batch_attempts: int = 2,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
    ) -> None:
        self.mode = mode if mode is not None else LLMMode(os.environ.get(MODE_ENV, "live"))
        self.cassette_dir = Path(cassette_dir)
        self.registry = {**REGISTRY, **(registry or {})}
        self._sleep = sleep
        self._max_retries = max_retries
        self.request_timeout_s = request_timeout_s
        self._sdk = sdk_client
        self._pending: dict[str, tuple[str, dict[str, Any], type[BaseModel]]] = {}
        self._spent_keys: set[str] = set()
        self._reported_keys: set[str] = set()
        self._batch_failures: dict[str, int] = {}
        self._batch_failure_reason: dict[str, str] = {}
        self._max_batch_attempts = max_batch_attempts
        # Spend on batch results that came back unusable (truncated, schema-invalid).
        self.failed_usage_usd = Decimal(0)
        if self._sdk is None and self.mode in (LLMMode.live, LLMMode.record, LLMMode.batch):
            key = os.environ.get(KEY_ENV, "")
            if not key:
                raise RuntimeError(f"{KEY_ENV} is not set; it is the only API key variable read")
            self._sdk = anthropic.Anthropic(
                api_key=key, base_url=API_BASE_URL, timeout=request_timeout_s, max_retries=0
            )

    # --- request identity --------------------------------------------------------------

    def resolve(self, role_or_model: str) -> str:
        return self.registry.get(role_or_model, role_or_model)

    def request_key(
        self,
        role_or_model: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        *,
        max_tokens: int = 2048,
    ) -> str:
        model = self.resolve(role_or_model)
        canonical = json.dumps(
            {
                "shape": REQUEST_SHAPE_VERSION,
                "model": model,
                "effort": EFFORT.get(model),
                "system": system,
                "messages": messages,
                "schema": schema.model_json_schema(),
                "max_tokens": max_tokens,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    # --- the call ----------------------------------------------------------------------

    def structured(
        self,
        role_or_model: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        max_tokens: int = 2048,
    ) -> tuple[T, Usage]:
        model = self.resolve(role_or_model)

        if self.mode is LLMMode.stub:
            return stub_response(schema), Usage(
                model=model,
                input_tokens=0,
                cache_write_tokens=0,
                cache_read_tokens=0,
                output_tokens=0,
                cost_usd=Decimal(0),
                latency_ms=0,
            )

        key = self.request_key(model, system, messages, schema, max_tokens=max_tokens)
        path = self.cassette_dir / f"{key}.json"

        if self.mode is LLMMode.replay:
            if not path.exists():
                raise CassetteMissError(f"no cassette for request {key} in {self.cassette_dir}")
            return self._replay(path, schema, spent=False)

        if self.mode is LLMMode.batch:
            if path.exists():
                # Cost is reported on the first serve of a key collected by this client's
                # batches and 0 afterwards, so summing every call's Usage counts it once.
                spent = key in self._spent_keys and key not in self._reported_keys
                self._reported_keys.add(key)
                return self._replay(path, schema, spent=spent)
            if self._batch_failures.get(key, 0) >= self._max_batch_attempts:
                raise BatchGaveUp(key, self._batch_failure_reason.get(key, "unknown"))
            self._check_priced(model)
            self._pending[key] = (
                model,
                self._build_request(model, system, messages, schema, max_tokens),
                schema,
            )
            raise BatchPending(key)

        self._check_priced(model)
        request = self._build_request(model, system, messages, schema, max_tokens)
        obj, usage = self._call(model, request, schema)
        if self.mode is LLMMode.record:
            self._write_cassette(path, key, model, obj, usage)
        return obj, usage

    # --- batch mode ----------------------------------------------------------------------
    #
    # Batch mode runs a pipeline in rounds: structured() serves cassettes that exist and
    # queues every miss (raising BatchPending), the harness calls run_pending_batch() to
    # submit, poll and collect the queue into cassettes, then reruns the pending questions.
    # A multi-stage engine (plan, then compose) simply takes one round per stage. Requests
    # are keyed by their request key, which is also the batch custom_id (64 hex characters
    # fits the API's 1-64 character custom_id rule), so results are matched by key, never by
    # position.

    @property
    def pending(self) -> int:
        return len(self._pending)

    def run_pending_batch(
        self, *, poll_interval_s: float = 30.0, timeout_s: float = 24 * 3600.0
    ) -> BatchReport:
        """Submit the queued requests as one Message Batch, wait for it, write cassettes.

        Idempotent across crashes: the batch id and its keys are saved to
        ``<cassette_dir>/.batch_<id>.json`` before polling, and a later call resumes an
        unfinished saved batch whose keys are still queued instead of paying for a new one.
        Keys whose cassette already exists are never resubmitted. Polling and the results
        stream retry transient errors with the same policy as single calls. On timeout the
        batch is left running and saved, so the next call collects it.
        """
        if self.mode is not LLMMode.batch:
            raise RuntimeError("run_pending_batch needs batch mode")
        queued = {
            k: v for k, v in self._pending.items() if not (self.cassette_dir / f"{k}.json").exists()
        }
        if not queued:
            self._pending.clear()
            return BatchReport(batch_id=None, submitted=0, succeeded=0, failed={})
        if len(queued) > MAX_BATCH_REQUESTS:
            raise ValueError(f"{len(queued)} requests exceed the {MAX_BATCH_REQUESTS} batch limit")

        batch_id = self._resumable_batch(set(queued))
        if batch_id is None:
            batch = self._with_retries(
                lambda: self._sdk.messages.batches.create(
                    requests=[
                        {"custom_id": key, "params": req} for key, (_, req, _) in queued.items()
                    ]
                )
            )
            batch_id = str(batch.id)
            self._save_batch_state(batch_id, set(queued))

        waited = 0.0
        while (
            self._with_retries(lambda: self._sdk.messages.batches.retrieve(batch_id))
        ).processing_status != "ended":
            if waited >= timeout_s:
                raise TimeoutError(
                    f"batch {batch_id} not ended after {timeout_s:.0f}s; it keeps running and "
                    "the next run_pending_batch() call collects it"
                )
            self._sleep(poll_interval_s)
            waited += poll_interval_s

        entries = self._with_retries(lambda: list(self._sdk.messages.batches.results(batch_id)))
        succeeded: set[str] = set()
        failed: dict[str, str] = {}
        for entry in entries:
            key = entry.custom_id
            if key not in queued:
                continue  # resumed batch containing requests no longer queued
            model, _, schema = queued[key]
            result = entry.result
            if result.type != "succeeded":
                failed[key] = str(result.type)
                continue
            try:
                obj, usage = self._parse(model, result.message, schema, latency_ms=0, batch=True)
            except LLMResponseError as err:
                failed[key] = str(err)
                self.failed_usage_usd += err.usage.cost_usd  # billed, but no usable output
                continue
            self._write_cassette(self.cassette_dir / f"{key}.json", key, model, obj, usage)
            self._spent_keys.add(key)
            succeeded.add(key)
        for key in set(queued) - succeeded - set(failed):
            failed[key] = "no result returned"
        for key, reason in failed.items():
            self._batch_failures[key] = self._batch_failures.get(key, 0) + 1
            self._batch_failure_reason[key] = reason
        self._batch_state_path(batch_id).unlink(missing_ok=True)
        self._pending.clear()
        return BatchReport(
            batch_id=batch_id, submitted=len(queued), succeeded=len(succeeded), failed=failed
        )

    def _batch_state_path(self, batch_id: str) -> Path:
        return self.cassette_dir / f".batch_{batch_id}.json"

    def _save_batch_state(self, batch_id: str, keys: set[str]) -> None:
        self.cassette_dir.mkdir(parents=True, exist_ok=True)
        self._batch_state_path(batch_id).write_text(
            json.dumps({"batch_id": batch_id, "keys": sorted(keys)}, indent=2)
        )

    def _resumable_batch(self, keys: set[str]) -> str | None:
        """A saved, uncollected batch whose keys are all still queued, if any."""
        if not self.cassette_dir.exists():
            return None
        for state in sorted(self.cassette_dir.glob(".batch_*.json")):
            saved = json.loads(state.read_text())
            if set(saved["keys"]) <= keys:
                return str(saved["batch_id"])
        return None

    def _with_retries(self, fn: Callable[[], R]) -> R:
        attempt = 0
        while True:
            try:
                return fn()
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as err:
                if not _retryable(err) or attempt >= self._max_retries:
                    raise
                backoff = min(BACKOFF_BASE_S * 2**attempt, MAX_BACKOFF_S)
                hinted = _retry_after_s(err)
                self._sleep(max(backoff, hinted) if hinted is not None else backoff)
                attempt += 1

    # --- internals -----------------------------------------------------------------------

    def _check_priced(self, model: str) -> None:
        if model not in PRICES:  # fail before spending, not after an unpriceable response
            raise KeyError(f"no price for model {model!r} in app/llm/prices.py")

    def _replay(self, path: Path, schema: type[T], *, spent: bool) -> tuple[T, Usage]:
        stored = json.loads(path.read_text())
        recorded = Usage.model_validate(stored["usage"])
        if not spent:
            # Replay spends nothing; the recorded token counts stay for auditing.
            recorded = recorded.model_copy(update={"cost_usd": Decimal(0), "latency_ms": 0})
        return schema.model_validate(stored["output"]), recorded

    def _write_cassette(
        self, path: Path, key: str, model: str, obj: BaseModel, usage: Usage
    ) -> None:
        self.cassette_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "request_key": key,
            "model": model,
            "output": obj.model_dump(mode="json"),
            "usage": usage.model_dump(mode="json"),
        }
        # Unique temp name: concurrent identical requests must not share a temp file.
        with tempfile.NamedTemporaryFile(
            "w", dir=self.cassette_dir, prefix=f".{key}.", suffix=".tmp", delete=False
        ) as tmp:
            tmp.write(json.dumps(payload, indent=2, sort_keys=True))
        Path(tmp.name).replace(path)

    def _build_request(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        max_tokens: int,
    ) -> dict[str, Any]:
        output_config: dict[str, Any] = {
            "format": {
                "type": "json_schema",
                "schema": anthropic.transform_schema(schema.model_json_schema()),
            }
        }
        if model in EFFORT:
            output_config["effort"] = EFFORT[model]
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "output_config": output_config,
        }

    def _call(self, model: str, request: dict[str, Any], schema: type[T]) -> tuple[T, Usage]:
        timing: dict[str, float] = {}

        def attempt() -> Any:
            timing["start"] = time.monotonic()  # latency of the successful attempt only
            return self._sdk.messages.create(**request)

        response = self._with_retries(attempt)
        latency_ms = int((time.monotonic() - timing["start"]) * 1000)
        return self._parse(model, response, schema, latency_ms=latency_ms, batch=False)

    def _parse(
        self, model: str, response: Any, schema: type[T], *, latency_ms: int, batch: bool
    ) -> tuple[T, Usage]:
        u = response.usage
        input_tokens = int(u.input_tokens or 0)
        cache_write = int(getattr(u, "cache_creation_input_tokens", None) or 0)
        cache_read = int(getattr(u, "cache_read_input_tokens", None) or 0)
        output_tokens = int(u.output_tokens or 0)
        usage = Usage(
            model=model,
            input_tokens=input_tokens,
            cache_write_tokens=cache_write,
            cache_read_tokens=cache_read,
            output_tokens=output_tokens,
            cost_usd=cost_usd(
                model, input_tokens, cache_write, cache_read, output_tokens, batch=batch
            ),
            latency_ms=latency_ms,
            batch=batch,
        )
        if response.stop_reason != "end_turn":
            raise LLMResponseError(f"{model} stopped with {response.stop_reason!r}", usage)
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        try:
            obj = schema.model_validate_json(text)
        except ValidationError as err:
            raise LLMResponseError(
                f"{model} output does not match {schema.__name__}", usage
            ) from err
        return obj, usage
