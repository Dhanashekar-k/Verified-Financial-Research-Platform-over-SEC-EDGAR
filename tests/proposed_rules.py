"""The two rules of ADR-022, applied on top of the UNMODIFIED reference.

`pipelines/common/fiscal_ref.py` is Shekar's file and is not edited here. The two rules are:

1. Determinism. Every tie the reference breaks by list position is broken by sorting its input
   first, by a stable total key: accession, then value ascending with NULLs last, then period end,
   quarters, and the series identity (cik, tag, taxonomy, uom). Python's `min` and `max` return the
   first of equal keys, so after the sort the winner of any tie is the row that sorts first.
   This covers the label-offset mode tie (the offset of the submission with the smallest accession
   wins) and a fiscal-year change (two FY rows of one filing: the smaller value wins).
2. Eligibility. A Q4 is derived only from an FY (qtrs 4) row of a 10-K-family filing: 10-K, 10-KT or
   10-K/A. An FY row from any other form (a 10-Q's trailing-twelve-month figure, a registration
   statement) is never a parent. The 9M parent may come from any form.

`reference_with_proposed_rules` is what the Spark and DuckDB twins must reproduce. Once the diff
in ADR-022 is applied to `fiscal_ref.py`, it is the same as `derive_fiscal` itself and this helper
can be deleted.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from pipelines.common.fiscal_ref import FactInput, FiscalFact, derive_fiscal

ANNUAL_FORMS = ("10-K", "10-KT", "10-K/A")


def stable_key(f: FactInput) -> tuple[Any, ...]:
    """A total order over the facts of a company: accession, value (NULLs last), period end, ..."""
    return (
        f.adsh,
        f.value is None,
        f.value if f.value is not None else Decimal(0),
        f.period_end,
        f.qtrs,
        f.cik,
        f.tag,
        f.taxonomy,
        f.uom,
    )


def reference_with_proposed_rules(facts: Sequence[FactInput]) -> list[FiscalFact]:
    """`derive_fiscal` with the sort and the eligibility rule of ADR-022 applied around it."""
    ordered = sorted(facts, key=stable_key)
    originals = [ff for ff in derive_fiscal(ordered) if ff.derived_from is None]
    eligible = [f for f in ordered if not (f.qtrs == 4 and f.form not in ANNUAL_FORMS)]
    derived = [ff for ff in derive_fiscal(eligible) if ff.derived_from is not None]
    return originals + derived
