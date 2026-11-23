"""Deterministic stand-ins for model output (stub mode, the default in tests).

A test registers a factory for a schema with ``register_stub``. Otherwise a schema whose fields
all have defaults is built from those defaults (the same object every time), and a schema with
required fields raises ``LookupError`` rather than returning a half-built object.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

_STUBS: dict[type[BaseModel], Callable[..., BaseModel]] = {}


def register_stub(schema: type[BaseModel], fn: Callable[..., BaseModel]) -> None:
    """Make stub mode answer ``schema`` requests with ``fn(...)``."""
    _STUBS[schema] = fn


def stub_response[T: BaseModel](schema: type[T], **context: Any) -> T:
    fn = _STUBS.get(schema)
    if fn is not None:
        result = fn(**context)
        if not isinstance(result, schema):
            raise TypeError(f"stub for {schema.__name__} returned {type(result).__name__}")
        return result
    required = [name for name, f in schema.model_fields.items() if f.is_required()]
    if required:
        # model_construct would leave these unset and hand back a half-built object.
        raise LookupError(
            f"no stub registered for {schema.__name__} (required fields: {required}); "
            "call app.llm.stub.register_stub"
        )
    return schema()
