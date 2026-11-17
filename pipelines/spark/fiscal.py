"""Spark implementation of the fiscal-period rules (M01 section 3.5, rules 1-6).

OWNED BY SHEKAR. Claude created this stub once and will not edit it again (CLAUDE.md rule 3).

`pipelines/common/fiscal_ref.py` is the pure-Python reference of the same rules; the
differential test (`tests/integration/test_differential.py`) asserts the two agree row for row,
so write the reference first and use it as the oracle.

Avoid Python UDFs (`.claude/rules/data-pipelines.md`): this runs over every fact since 2009.
Use Spark SQL functions, window functions and `xxhash64`.
"""

from __future__ import annotations

from collections.abc import Sequence

from pyspark.sql import DataFrame


def derive_fiscal_columns(df: DataFrame, extra_key_cols: Sequence[str] = ()) -> DataFrame:
    """Add fiscal columns to consolidated facts and append derived Q4 rows.

    Input columns (one row per fact version, already filtered to consolidated rows):
        cik int, adsh string, tag string, taxonomy string, uom string, qtrs int,
        period_end date, value decimal(28,4), form string, filed date,
        fye_mmdd string      -- sub.fye of the SAME submission (rule 1)
        sub_fy int           -- sub.fy of that submission
        sub_period date      -- sub.period of that submission
    plus any pass-through columns, which must be preserved.

    Output: every input column, plus
        period_start date          -- approximate: period_end - qtrs quarters + 1 day; null for instants
        fiscal_year int            -- y_end(period_end, fye_mmdd) + label_offset(cik)   (rules 2-3)
        fiscal_period string       -- FY | Q1..Q4 | H1 | 9M | INSTANT                   (rule 4)
        calendar_frame string      -- CY2023 | CY2023Q3 | CY2023Q3I | null              (rule 6)
        dq_flags array<string>     -- empty array (not null) when there are no flags
        fact_key bigint            -- xxhash64(cik, tag, taxonomy, period_end, qtrs, uom, *extra_key_cols)
        fact_id bigint             -- xxhash64(fact_key, adsh)
        derived_from array<bigint> -- null, except on derived Q4 rows: [FY fact_id, 9M fact_id]

    Rule 5 appends one row per derivable Q4 (flow concepts only; never instants or per-share and
    ratio concepts): fiscal_period 'Q4', qtrs 1, period_end = the FY end, form '10-K', the FY
    row's adsh, value = FY - 9M, dq_flags containing 'q4_derived'.

    `extra_key_cols` names extra columns that identify a series and enter fact_key and the Q4
    pairing (for example ['segments'] for facts_dimensional). The default is the consolidated
    case.
    """
    raise NotImplementedError
