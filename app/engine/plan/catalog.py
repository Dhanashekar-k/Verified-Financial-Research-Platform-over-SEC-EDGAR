"""The metric catalog model and loader (05_data_contracts.md 4.2).

`app/engine/plan/metric_catalog.yaml` is parsed into `MetricDef` objects here. The engine (planner,
compiler) and the eval tooling (`eval/gen`) share this one definition; `eval/gen/catalog_review.py`
re-exports these names.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

CATALOG_PATH = Path(__file__).resolve().parent / "metric_catalog.yaml"  # cwd-independent
SHA_LEN = 12


FallbackCondition = Literal[
    "same_entity",  # every input fact has the same cik
    "same_period",  # same period_end, and same qtrs for durations
    "same_currency",  # same uom (USD)
    "same_scope",  # consolidated totals only (the facts view; never facts_dimensional)
    "inputs_verified",  # every input metric is on the verified allowlist and resolved cleanly
]
FALLBACK_CONDITIONS: tuple[str, ...] = get_args(FallbackCondition)


class MetricDef(BaseModel):
    """One metric_catalog.yaml entry (05_data_contracts.md 4.2)."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    unit: Literal["USD", "ratio", "USD/shares", "shares"]
    period_type: Literal["duration", "instant"] | None = None
    tags: list[str] | None = None
    formula: str | None = None
    fallback_formula: str | None = None
    synonyms: list[str] = []
    # ADR-042 (M4 catalog review). A distinct presentation basis makes a metric its own concept,
    # never a fallback of a generic one (bank revenue, tax-inclusive revenue).
    presentation_basis: Literal["net_of_interest_expense", "including_assessed_tax"] | None = None
    # Conditions under which fallback_formula may be used; a consumer that can't check every
    # listed condition must not use the fallback. A fallback without only_if is never used by
    # the numeric generator.
    only_if: list[FallbackCondition] | None = None
    # How a fallback_formula value is labelled: "derived" (computed), never "reported".
    fallback_result: Literal["derived"] | None = None

    @model_validator(mode="after")
    def _tags_xor_formula(self) -> MetricDef:
        if (self.only_if is not None or self.fallback_result is not None) and (
            self.fallback_formula is None
        ):
            raise ValueError("`only_if` / `fallback_result` need a `fallback_formula`")
        if self.only_if is not None and not self.only_if:
            raise ValueError("`only_if` must list at least one condition (omit it instead)")
        if self.only_if is not None and len(set(self.only_if)) != len(self.only_if):
            raise ValueError(f"duplicate condition in only_if {self.only_if}")
        if (self.tags is None) == (self.formula is None):
            raise ValueError("a metric needs exactly one of `tags` or `formula`")
        if self.tags is not None:
            if not self.tags:
                raise ValueError("`tags` must not be empty")
            if len(set(self.tags)) != len(self.tags):
                raise ValueError(f"duplicate tag in chain {self.tags}")
            if self.period_type is None:
                raise ValueError("a tag-based metric needs `period_type`")
        if self.fallback_formula is not None and self.tags is None:
            raise ValueError("`fallback_formula` only makes sense next to `tags`")
        return self


def definition_sha(m: MetricDef) -> str:
    """12-hex sha of the parts of a definition that change VALUES: unit, period_type, tags (in
    order), formula and fallback_formula. Label and synonyms are excluded, so a wording change
    doesn't invalidate a verification, while any chain change does."""
    payload = m.model_dump(
        mode="json",
        exclude={"label", "synonyms", "presentation_basis", "only_if", "fallback_result"},
    )
    # ADR-042 fields enter the payload only when set, so adding them to the model left the sha
    # of every metric that doesn't use them unchanged (no verification is invalidated by it).
    for key in ("presentation_basis", "only_if", "fallback_result"):
        value = getattr(m, key)
        if value is not None:
            payload[key] = value
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:SHA_LEN]


def formula_refs(formula: str) -> set[str]:
    """Catalog keys a formula references.

    The formula must be an arithmetic expression over names and numbers using only
    + - * / and parentheses (no **, %, calls, attributes); anything else raises ValueError.
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as err:
        raise ValueError(f"bad token in formula {formula!r}: {err.msg}") from err
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            refs.add(node.id)
        elif isinstance(node, ast.Constant):
            if not isinstance(node.value, int | float) or isinstance(node.value, bool):
                raise ValueError(f"bad token in formula {formula!r}: {node.value!r}")
        elif isinstance(node, ast.BinOp):
            if not isinstance(node.op, ast.Add | ast.Sub | ast.Mult | ast.Div):
                raise ValueError(f"bad token in formula {formula!r}: {type(node.op).__name__}")
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, ast.USub):
                raise ValueError(f"bad token in formula {formula!r}: {type(node.op).__name__}")
        elif not isinstance(node, ast.Expression | ast.Load | ast.operator | ast.unaryop):
            raise ValueError(f"bad token in formula {formula!r}: {type(node).__name__}")
    return refs


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, MetricDef]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{path}: expected a non-empty mapping of metric -> definition")
    catalog = {str(name): MetricDef.model_validate(body) for name, body in raw.items()}
    for name, m in catalog.items():
        for expr in (m.formula, m.fallback_formula):
            if expr is None:
                continue
            unknown = formula_refs(expr) - catalog.keys()
            if unknown:
                raise ValueError(f"{name}: formula {expr!r} references unknown {sorted(unknown)}")
            if name in formula_refs(expr):
                raise ValueError(f"{name}: formula references itself")
    return catalog
