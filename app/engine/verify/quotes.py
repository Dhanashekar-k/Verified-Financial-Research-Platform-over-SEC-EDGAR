"""Deterministic check of a text claim (M06 3.3, ADR-049 D4). **Shekar-owned.**
Stub: set ``IMPLEMENTED = True`` once the three functions are written.

There is no entailment stage (ADR-049, deterministic branch): a claim that passes here is kept.
So a real quote under a claim that adds a cause, widens a scope or draws an invalid inference
**passes**. That is the documented limit, not something to patch with heuristics.

``check_text`` is called for every claim of kind ``text``. Set ``quote_substring``,
``metadata_match`` and ``number_in_text`` to ``pass`` or ``fail`` (``n/a`` when an earlier
failure makes one undecidable); leave ``provenance_exec`` and ``entailment`` at ``n/a``.

quote_substring
  - The provenance is ``chunk`` (a text claim citing facts fails here).
  - The chunk is in ``evidence.chunks``. That holds only passages the composer was shown, so a
    real chunk it never saw fails too.
  - **D4:** after ``normalize``, the quote has at least ``cfg.min_quote_words`` words (tokens
    containing a letter or digit) and is a substring of the normalized chunk text. An empty
    quote fails.

normalize
  Applied identically to quote and chunk (M06 pitfall: asymmetry): case-folded; runs of
  whitespace, including non-breaking spaces, to one space; zero-width spaces removed; curly
  quotes to straight ones; the dash variants (U+2010..U+2015, U+2212) to "-"; stripped. Use an
  explicit character map, **not** NFKC (it would turn a footnote "²" into a "2").

metadata_match
  - When ``ctx.allowed_ciks`` is not empty, the chunk's ``cik`` is in it. A NULL ``cik`` fails
    then (D7: fail closed). When it is empty, there is no company restriction.

number_in_text: the number-in-quote rule
  Applies to **value-bearing numbers only**: those ``numbers.parse_numbers`` marks ``value`` in
  ``claim.text``. Each must be stated by a number in the quote, matched within the claim
  number's own precision.
  - **Exempt:** four-digit years, and structural labels: product names (``787``), section
    references (``12(b)``), day-of-month and quarter tokens. Real dev claims the (uncalibrated)
    judge supported cite quotes without their year or label.
  - **Not exempt:** a number that is the claim's asserted answer. ``fb-01328 c1`` ("... so
    restructuring costs directly outlined there are 0.") quotes only the statement's title:
    its ``0`` must fail.
  - **D4, scale:** a bare number in the quote does not support a scaled claim. "32,502" does not
    support "$32.5 billion", and "6,098" does not support "6,098 billion": when the quote states
    no scale, fail closed. (Measured cost on dev, for your decision: six text claims the judge
    supported write a scale word over a bare table cell, e.g. fb-00517 c1 "$55,893 million".)
"""

from __future__ import annotations

from app.engine.answer import Claim
from app.engine.verify.models import CheckOutcome, Evidence, VerifyConfig, VerifyContext

IMPLEMENTED = False


def normalize(text: str) -> str:
    """The comparison form of a quote or a passage (see the module docstring)."""
    raise NotImplementedError("Shekar-owned: verify/quotes.normalize")


def locate_quote(quote: str, text: str) -> tuple[int, int] | None:
    """``(start, end)`` in the **original** ``text`` of the first span whose normalized form is
    ``normalize(quote)``; None when there is none or the normalized quote is empty."""
    raise NotImplementedError("Shekar-owned: verify/quotes.locate_quote")


def check_text(
    claim: Claim, evidence: Evidence, ctx: VerifyContext, cfg: VerifyConfig
) -> CheckOutcome:
    """Check one text claim against its cited passage."""
    raise NotImplementedError("Shekar-owned: verify/quotes.check_text")
