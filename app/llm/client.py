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
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from app.llm.prices import PRICES, cost_usd
from app.llm.stub import stub_response
from app.llm.types import LLMMode, Usage

T = TypeVar("T", bound=BaseModel)

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


class CassetteMissError(Exception):
    """Replay mode found no recorded response for a request."""


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
    ) -> None:
        self.mode = mode if mode is not None else LLMMode(os.environ.get(MODE_ENV, "live"))
        self.cassette_dir = Path(cassette_dir)
        self.registry = {**REGISTRY, **(registry or {})}
        self._sleep = sleep
        self._max_retries = max_retries
        self._sdk = sdk_client
        if self._sdk is None and self.mode in (LLMMode.live, LLMMode.record, LLMMode.batch):
            key = os.environ.get(KEY_ENV, "")
            if not key:
                raise RuntimeError(f"{KEY_ENV} is not set; it is the only API key variable read")
            self._sdk = anthropic.Anthropic(
                api_key=key, base_url=API_BASE_URL, timeout=REQUEST_TIMEOUT_S, max_retries=0
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
            stored = json.loads(path.read_text())
            recorded = Usage.model_validate(stored["usage"])
            # Replay spends nothing; the recorded token counts stay for auditing.
            usage = recorded.model_copy(update={"cost_usd": Decimal(0), "latency_ms": 0})
            return schema.model_validate(stored["output"]), usage

        if self.mode is LLMMode.batch:
            raise NotImplementedError("batch mode goes through submit_batch/collect_batch")

        if model not in PRICES:  # fail before spending, not after an unpriceable response
            raise KeyError(f"no price for model {model!r} in app/llm/prices.py")

        obj, usage = self._call(model, system, messages, schema, max_tokens)
        if self.mode is LLMMode.record:
            self._write_cassette(path, key, model, obj, usage)
        return obj, usage

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

    def _call(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[T],
        max_tokens: int,
    ) -> tuple[T, Usage]:
        output_config: dict[str, Any] = {
            "format": {
                "type": "json_schema",
                "schema": anthropic.transform_schema(schema.model_json_schema()),
            }
        }
        if model in EFFORT:
            output_config["effort"] = EFFORT[model]
        request = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "output_config": output_config,
        }
        attempt = 0
        while True:
            start = time.monotonic()
            try:
                response = self._sdk.messages.create(**request)
                break
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as err:
                if not _retryable(err) or attempt >= self._max_retries:
                    raise
                backoff = min(BACKOFF_BASE_S * 2**attempt, MAX_BACKOFF_S)
                hinted = _retry_after_s(err)
                self._sleep(max(backoff, hinted) if hinted is not None else backoff)
                attempt += 1
        latency_ms = int((time.monotonic() - start) * 1000)

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
            cost_usd=cost_usd(model, input_tokens, cache_write, cache_read, output_tokens),
            latency_ms=latency_ms,
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
