"""Types shared across the LLM boundary."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel


class LLMMode(StrEnum):
    """How the client reaches the model.

    live: call the API. stub: deterministic fakes, no network (the test default).
    record: call the API and store the response in a cassette. replay: serve cassettes only,
    $0. batch: Message Batches (submit, poll, collect; 50% off).
    """

    live = "live"
    stub = "stub"
    record = "record"
    replay = "replay"
    batch = "batch"


class Usage(BaseModel):
    """Token accounting for one call. ``cost_usd`` is what this call actually spent."""

    model: str
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: int
    batch: bool = False
