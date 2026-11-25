"""Plan compiler: QueryPlan -> typed, parameterized SQL over `facts` -> NumericResults.

STUB created once by Claude (M5 step 2). OWNED BY SHEKAR: he implements it; Claude only reads,
tests and reviews. The contract is 05_data_contracts.md section 4.5 and ADR-046; the tests are
`tests/integration/test_compiler_pg.py` and `tests/unit/engine/test_compiler_*.py`.

The LLM never writes SQL and never does arithmetic: values are Decimal, every result carries a
provenance `expr` over fact ids.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

import psycopg

from app.engine.plan.catalog import MetricDef
from app.engine.plan.schema import CompileOutcome, QueryPlan


def compile_plan(
    plan: QueryPlan,
    conn: psycopg.Connection[Any],
    *,
    catalog: Mapping[str, MetricDef] | None = None,
    universe: Collection[int] | None = None,
) -> CompileOutcome:
    """Compile ``plan`` against the ``facts`` view on ``conn`` (read-only use).

    ``catalog`` defaults to the shipped metric_catalog.yaml; ``universe`` is the CIK set a
    ``rank`` over "S&P 500" ranges over (the engine passes the tracked CIKs).
    Raises ``UnsupportedPlan`` when the plan cannot be compiled at all; any missing number is a
    ``NotFound`` inside the outcome.
    """
    raise NotImplementedError("Shekar implements the compiler (M05 section 3.4, ADR-046)")
