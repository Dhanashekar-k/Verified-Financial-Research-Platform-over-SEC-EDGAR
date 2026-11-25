"""Guarded text-to-SQL fallback: validate a model-written SELECT before it can run.

STUB created once by Claude (M5 step 2). OWNED BY SHEKAR: he implements it; Claude only reads,
tests and reviews (`tests/unit/engine/test_sql_guard.py`). M05 section 3.5: exactly one SELECT;
tables limited to ``{facts, companies}``; functions limited to an allow-list; no comments, no
chained statements, no writing CTEs; LIMIT injected or clamped to 200. The fallback is off by
default (``ENGINE_SQL_FALLBACK``) and never on the main path (CLAUDE.md rule 6).

Execution (role ``edgarqa_ro``, ``SET LOCAL statement_timeout = '2s'``) is a separate step: this
module only decides whether a string may be executed at all.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

MAX_LIMIT = 200
ALLOWED_TABLES: frozenset[str] = frozenset({"facts", "companies"})
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {"sum", "avg", "min", "max", "count", "round", "abs", "coalesce"}
)

RejectionCode = Literal[
    "empty",
    "parse_error",
    "not_single_statement",
    "not_select",
    "table_not_allowed",
    "function_not_allowed",
    "comment",
    "writing_cte",
    "system_catalog",
    "other",
]


class SafeSQL(BaseModel):
    model_config = ConfigDict(frozen=True)

    sql: str  # the normalized statement, with LIMIT <= MAX_LIMIT


class Rejection(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: RejectionCode
    detail: str


def validate(sql: str) -> SafeSQL | Rejection:
    """``SafeSQL`` when ``sql`` is one allowed SELECT, else a ``Rejection`` (never raises on
    hostile input)."""
    raise NotImplementedError("Shekar implements the guard (M05 section 3.5)")
