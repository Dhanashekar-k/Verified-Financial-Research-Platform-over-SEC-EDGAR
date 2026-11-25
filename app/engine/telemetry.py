"""OpenTelemetry spans for the engine stages (CLAUDE.md: every stage is a span).

The spans exist now and cost nothing without an exporter: the OTel API returns a no-op tracer until
M8 wires the SDK and the Jaeger exporter. Token and cost attributes go on the stages that call a
model.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from app.llm.types import Usage

_TRACER = trace.get_tracer("edgarqa.engine")


@contextmanager
def stage(name: str, **attrs: Any) -> Iterator[Any]:
    with _TRACER.start_as_current_span(f"engine.{name}") as span:
        for k, v in attrs.items():
            span.set_attribute(k, v)
        yield span


def record_usage(span: Any, usage: Usage) -> None:
    """``model``, ``input_tokens``, ``output_tokens`` and ``cost_usd`` of one model call."""
    span.set_attribute("model", usage.model)
    span.set_attribute(
        "input_tokens", usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens
    )
    span.set_attribute("output_tokens", usage.output_tokens)
    span.set_attribute("cost_usd", float(usage.cost_usd))
