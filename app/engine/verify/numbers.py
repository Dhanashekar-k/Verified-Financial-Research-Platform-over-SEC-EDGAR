"""Numbers in claim text and display strings (M06 3.1). **Shekar-owned (ADR-049, option A):**
parsing, non-value classification, precision and matching. Its callers are ``numeric.py``,
``quotes.py`` and ``policy.py`` (D6: numbers in a composer's refusal).
Stub: set ``IMPLEMENTED = True`` once the bodies are written.

Parsing
  ``parse_numbers`` returns **every** number in the text, in order, each with a ``kind``:

  - ``value``: a quantity the sentence asserts. Written forms: ``$383.3 billion``, ``383,285
    million``, ``$1.2B`` (K, M, B, T and thousand .. trillion), ``29.8%``, ``25 bps``, ``1.5x``,
    ``(1,234)`` (negative), ``-3.1%``, ``-$1.2 million``, ``$6.13 per share``, ``15.7 billion
    shares``, and a bare ``0``.
  - ``year``: a calendar or fiscal year. A bare four-digit integer from 1990 to 2099 with no
    currency sign, comma, decimal point, percent or scale word (``2018``, ``FY2023``), and the
    two-digit year of ``FY23`` / ``'23`` (value 2023). ``$2,022`` and ``2,000`` are values.
    (M06 3.1 says 2019-2030 near "FY"; the corpus has older filings, so that is too narrow.)
  - ``label``: digits that name something and assert no quantity: form names (``10-K``,
    ``8-K``), ``Item 7`` / ``Item 1A``, ``Section 12(b)``, ``401(k)``, a quarter (``Q2``), a day
    of the month in a date (``December 31, 2018``), a citation marker (``[1]``), a product or
    index name (``the 787 program``, ``S&P 500``).

  A number the claim asserts as its answer is a ``value`` even when it looks small or odd:
  in "restructuring costs ... are 0", the ``0`` is a value, never a label.

  The sign is as written. "decreased 36.7%" parses as +0.367: reading the direction word is
  ``numeric.py``'s job.

Units
  ``value`` is in base units: scale words applied, percent and bps turned into fractions
  (``29.8%`` -> 0.298, ``25 bps`` -> 0.0025). ``precision`` is half of the last place shown, in
  the same units: ``$383.3 billion`` -> 0.05e9; ``383,285 million`` -> 0.5e6; ``29.8%`` ->
  0.0005; ``0`` -> 0.5. Everything is ``Decimal``.

Tolerance (D1)
  - the claim's ``value`` and ``display``: ``max(rel * |target|, precision of the display)``;
  - a number written in claim **text**: its own precision only (so ``$385.0 billion`` does not
    match 383,285,000,000, although 0.5% of it would be 1.9 billion).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

IMPLEMENTED = False

NumberKind = Literal["value", "year", "label"]
UnitHint = Literal["currency", "percent", "bps", "multiple", "per_share", "shares"]


@dataclass(frozen=True)
class ParsedNumber:
    raw: str  # text[start:end]
    start: int
    end: int
    kind: NumberKind
    value: Decimal  # base units, signed as written
    precision: Decimal  # half of the last place shown, in base units
    unit_hint: UnitHint | None = None
    scale: Decimal = Decimal(1)  # the scale word's multiplier (1e9 for "billion"); 1 when none


def parse_numbers(text: str) -> list[ParsedNumber]:
    """Every number in ``text``, left to right, classified (see the module docstring)."""
    raise NotImplementedError("Shekar-owned: verify/numbers.parse_numbers")


def matches(number: ParsedNumber, target: Decimal) -> bool:
    """The text rule of D1: ``|number.value - target| <= number.precision``. Sign counts."""
    raise NotImplementedError("Shekar-owned: verify/numbers.matches")


def value_tolerance(target: Decimal, display: str | None, *, rel: Decimal) -> Decimal:
    """D1 for ``claim.value`` and ``claim.display``: ``max(rel * |target|, precision of the
    value written in display)``; ``rel * |target|`` when ``display`` is None or has no value."""
    raise NotImplementedError("Shekar-owned: verify/numbers.value_tolerance")
