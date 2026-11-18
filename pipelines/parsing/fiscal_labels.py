"""Fiscal labels for a filing (ADR-023; contract section 2.2, `curated.filings`).

The labels use the same functions as `curated.facts`, so a filing and its numbers agree:

    fiscal_year   = y_end(period_of_report, fye_mmdd) + label_offset(cik)
    fiscal_period = 'FY' for 10-K / 10-K/A; the quarter label for 10-Q / 10-Q/A

If the derivation is impossible (no period, no fiscal year end, or a year end `y_end` rejects)
the labels come from `fsds_sub.fy` / `fp` of the same accession and `fiscal_from_fsds_sub` is
flagged; with neither source they are NULL and `fiscal_unresolved` is flagged. A company with no
10-K in `fsds_sub` gets offset 0 (as in facts) and `label_offset_default`, which can mislabel an
off-calendar filer: that is the flag's job to expose, not this function's to solve.

Pure Python over `pipelines.common.fiscal_ref` (stdlib only), so it runs in Spark workers and in
Lambda.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pipelines.common.fiscal_ref import LabelOffset, fiscal_period_label, y_end


@dataclass(frozen=True, slots=True)
class FiscalLabels:
    fiscal_year: int | None
    fiscal_period: str | None
    dq_flags: tuple[str, ...]


def filing_fiscal_labels(
    *,
    form: str,
    period_of_report: date | None,
    fye_mmdd: str | None,
    fye_from_companies: bool,
    offset: LabelOffset | None,
    sub_fy: int | None,
    sub_fp: str | None,
) -> FiscalLabels:
    if form.startswith("10-K"):
        qtrs = 4
    elif form.startswith("10-Q"):
        qtrs = 1
    else:
        raise ValueError(f"unsupported form {form!r}: expected a 10-K or 10-Q family form")

    derived: tuple[int, str] | None = None
    flags: list[str] = []
    if period_of_report is not None and fye_mmdd:
        try:
            year = y_end(period_of_report, fye_mmdd)
            period = fiscal_period_label(period_of_report, qtrs, fye_mmdd)
        except ValueError:
            derived = None  # an impossible fiscal year end: fall back below
        else:
            if offset is None:
                flags.append("label_offset_default")
            elif not offset.is_constant:
                flags.append("label_offset_varies")
            if fye_from_companies:
                flags.append("fye_from_companies")
            derived = (year + (offset.offset if offset else 0), period)

    have_sub = sub_fy is not None and bool(sub_fp)
    if derived is not None:
        if have_sub and (sub_fy, sub_fp) != derived:
            flags.append("fiscal_differs_from_fsds_sub")
        return FiscalLabels(derived[0], derived[1], tuple(flags))
    if have_sub:
        return FiscalLabels(sub_fy, sub_fp, ("fiscal_from_fsds_sub",))
    return FiscalLabels(None, None, ("fiscal_unresolved",))
