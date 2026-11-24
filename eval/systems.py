"""Systems the harness can score, behind one interface: ``answer(question, cfg) -> Answer``.

The baseline and the engine return the same ``Answer`` schema, so scoring is identical. Usage
and retrieval are observed around the call, not returned by it:

- ``AccountingLLM`` wraps the run's ``LLMClient`` and records every call's ``Usage`` (including
  billed-but-failed responses), so per-question cost comes from accounting, not estimates.
- ``record_retrieval(ids)`` lets a system report the chunk ids it retrieved, in rank order, for
  recall@k; it writes into a context variable that ``eval.run`` sets per question.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from app.engine.answer import Answer, Refusal
from app.llm.client import LLMClient, LLMResponseError
from app.llm.types import Usage


class AccountingLLM:
    """Delegates to an LLMClient and keeps a ledger of every call's Usage."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client
        self.ledger: list[Usage] = []

    @property
    def mode(self) -> Any:
        return self.client.mode

    def structured(
        self,
        role_or_model: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        *,
        max_tokens: int = 2048,
    ) -> tuple[Any, Usage]:
        try:
            obj, usage = self.client.structured(
                role_or_model, system, messages, schema, max_tokens=max_tokens
            )
        except LLMResponseError as err:
            self.ledger.append(err.usage)
            raise
        self.ledger.append(usage)
        return obj, usage


_RETRIEVED: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "eval_retrieved", default=None
)


def record_retrieval(chunk_ids: list[str]) -> None:
    """Called by a system with the chunk ids it retrieved, best first."""
    sink = _RETRIEVED.get()
    if sink is not None:
        sink.extend(chunk_ids)


def retrieval_sink() -> tuple[list[str], contextvars.Token[list[str] | None]]:
    sink: list[str] = []
    return sink, _RETRIEVED.set(sink)


def reset_retrieval(token: contextvars.Token[list[str] | None]) -> None:
    _RETRIEVED.reset(token)


_ROUTE: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "eval_route", default=None
)


def record_route(route: str) -> None:
    """Called by a system with the route it took (numeric | narrative | hybrid | dense ...)."""
    sink = _ROUTE.get()
    if sink is not None:
        sink["route"] = route


def route_sink_set() -> tuple[dict[str, str], contextvars.Token[dict[str, str] | None]]:
    sink: dict[str, str] = {}
    return sink, _ROUTE.set(sink)


def reset_route(token: contextvars.Token[dict[str, str] | None]) -> None:
    _ROUTE.reset(token)


@dataclass
class EvalCfg:
    """Runtime configuration handed to a system (the results file records a copy of it)."""

    llm: AccountingLLM
    composer: str = "composer"
    ef_search: int = 800  # ADR-040 default; pinned and recorded in every results file
    top_k: int = 8
    embedder: str | None = None
    corpus_version: int | None = None
    dsn: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class System(Protocol):
    name: str

    def answer(self, question: str, cfg: EvalCfg) -> Answer: ...


class StubSystem:
    """Refuses everything with no LLM call: exercises the harness end to end at $0."""

    name = "stub"

    def answer(self, question: str, cfg: EvalCfg) -> Answer:
        return Answer(
            answer_markdown="(stub system)",
            claims=[],
            refusal=Refusal(reason="stub system answers nothing"),
        )


def _baseline() -> System:
    from app.engine.baseline import BaselineSystem

    return BaselineSystem()


def _longctx() -> System:
    from app.engine.longctx import LongContextSystem

    return LongContextSystem()


def _engine() -> System:
    raise SystemExit("the engine arrives in M5")


SYSTEMS: dict[str, Callable[[], System]] = {
    "stub": StubSystem,
    "baseline": _baseline,
    "longctx": _longctx,
    "engine": _engine,
}
