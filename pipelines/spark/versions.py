"""Spark implementation of fact version selection (M01 section 3.5, "Dedup and restatements").

OWNED BY SHEKAR. Claude created this stub once and will not edit it again (CLAUDE.md rule 3).

The pure-Python twin is `select_versions_ref` in `pipelines/common/fiscal_ref.py`; the
differential test asserts the two agree.
"""

from __future__ import annotations

from pyspark.sql import DataFrame


def select_versions(df: DataFrame) -> DataFrame:
    """Turn `facts_history` rows into `curated.facts` rows (latest + original per fact_key).

    Input: one row per (fact_key, accession), with at least these columns, plus any
    pass-through columns (which must be preserved):
        fact_key bigint, fact_id bigint, period_end date, value decimal(28,4),
        accession string, form string, filed date, is_amendment boolean,
        filing_period date   -- sub.period of the accession: the filing's own reporting period

    Output: the input columns plus
        is_latest boolean, is_original boolean, n_versions int, is_restated boolean

    Latest = max(filed), ties by max(accession).
    Original = among rows whose filing_period == period_end, the earliest filed (ties: smallest
    accession); if none match, the earliest filed overall (ties: smallest accession).
    One row per fact_key, with is_latest and is_original both true, when the original and the
    latest have the same value. Otherwise two rows: the latest (is_latest only) and the
    original (is_original only). n_versions counts the rows of the group in the input;
    is_restated is true when the group has more than one distinct value.
    """
    raise NotImplementedError
