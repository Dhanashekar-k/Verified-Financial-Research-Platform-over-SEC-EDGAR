"""Answer and Claim: the composer's output and the verifier's input (05_data_contracts.md 4.3).

The baseline and the engine both return this schema, so eval scoring is identical for both
(M04 pitfall: no free-text baseline against a JSON engine).

``Refusal`` is ``reason`` / ``found`` / ``missing`` (05_data_contracts.md 4.3, ADR-041): the
same shape for the composer, the M6 verifier's sufficiency refusal and the API response.
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# expr may use only f:<fact_id>, numbers, + - * / and parentheses (4.3 rules). A fact_id is a
# signed xxhash64, so the token carries an optional leading minus (ADR-046). ASCII digits only.
_FACT_RE = re.compile(r"f:(-?[0-9]+)")
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.USub,
    ast.Constant,
    ast.Load,
)


def _check_expr(expr: str, fact_ids: list[int]) -> None:
    """Parse ``expr`` as arithmetic over f:<id> tokens and numbers; the ids it uses must equal
    ``fact_ids`` (as a set). Raises ValueError otherwise (empty, juxtaposed tokens, names, calls,
    other operators, an id outside signed 64 bits)."""
    # Each token becomes a padded literal: the structure is checked on pure arithmetic, so any
    # name left in the text (`f_5`, `abs`, `e3` after `f:1`) is a Name node and is rejected.
    py = _FACT_RE.sub(" 1 ", expr).strip()
    try:
        tree = ast.parse(py, mode="eval")
    except (SyntaxError, ValueError) as err:  # ValueError: an absurdly long number literal
        raise ValueError(f"expr {expr!r} is not an arithmetic expression") from err
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"expr {expr!r} uses {type(node).__name__}")
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, int | float)
        ):
            raise ValueError(f"expr {expr!r} has a non-numeric constant")
    used = {int(i) for i in _FACT_RE.findall(expr)}
    if used != set(fact_ids):
        raise ValueError(f"expr {expr!r} uses facts {sorted(used)} but fact_ids={fact_ids}")
    if any(not _INT64_MIN <= i <= _INT64_MAX for i in used):
        raise ValueError(f"expr {expr!r} cites a fact id outside signed 64 bits")


class ExprProvenance(BaseModel):
    type: Literal["expr"]  # required: a discriminator the model may omit breaks parsing
    expr: str
    fact_ids: list[int]

    @model_validator(mode="after")
    def _expr_grammar(self) -> ExprProvenance:
        _check_expr(self.expr, self.fact_ids)
        return self


class ChunkProvenance(BaseModel):
    type: Literal["chunk"]
    chunk_id: str
    quote: str


Provenance = Annotated[ExprProvenance | ChunkProvenance, Field(discriminator="type")]


class Claim(BaseModel):
    id: str
    kind: Literal["numeric", "numeric_derived", "text"]
    text: str
    value: Decimal | None = None  # CLAUDE.md: values are Decimal, never float
    unit: str | None = None
    display: str | None = None
    primary: bool = False
    provenance: Provenance


class Refusal(BaseModel):
    """ADR-041. ``found``: what the filings support (for a verifier refusal, the text of each
    surviving verified claim; the claims stay in ``Answer.claims``). ``missing``: what was
    needed."""

    reason: str
    found: list[str] = []
    missing: list[str] = []


class Answer(BaseModel):
    answer_markdown: str
    claims: list[Claim] = []
    refusal: Refusal | None = None

    def primary_claim(self) -> Claim | None:
        """The claim numeric scoring reads: the one marked primary, else the first numeric one."""
        for claim in self.claims:
            if claim.primary:
                return claim
        return next((c for c in self.claims if c.kind != "text"), None)
