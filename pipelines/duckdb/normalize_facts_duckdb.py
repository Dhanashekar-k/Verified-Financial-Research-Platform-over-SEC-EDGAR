"""DuckDB twin of `pipelines/spark/normalize_facts.py` (M01 section 3.7, the benchmark twin).

    uv run python -m pipelines.duckdb.normalize_facts_duckdb --src data/raw/fsds \
        --quarters 2023q3,2023q4 --out warehouse_duckdb

Reads the gzipped FSDS TSVs directly, applies the same rules as the Spark job and writes Parquet
with the contract columns of `facts_history`, `facts` and `facts_dimensional`. The rules
themselves (fiscal year, period and frame, label offset, Q4 = FY - 9M per provenance tier,
latest/original versions) are written again here in SQL, from the same specification as
`pipelines/common/fiscal_ref.py`; the differential tests hold the three implementations to the
same rows.

`fact_key` / `fact_id` are DuckDB hashes of the same fields Spark hashes with `xxhash64`. The
values differ between engines (DuckDB has no `xxhash64`), so comparisons use the natural key and
resolve `derived_from` back to natural keys (M01 section 3.5).

The contract column lists below are literal copies of `pipelines/spark/curated_schemas.py`, so
this module does not import PySpark; a test asserts they are equal.
"""

from __future__ import annotations

import argparse
import logging
import tempfile
from collections.abc import Sequence
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

_MASK = "CAST(9223372036854775807 AS UBIGINT)"  # keep 63 bits: DuckDB's hash is a UBIGINT
ANNUAL_FORMS = ("10-K", "10-KT", "10-K/A")  # ADR-022: the only forms an FY parent may come from

HISTORY_COLUMNS: list[tuple[str, str]] = [
    ("fact_id", "BIGINT"), ("fact_key", "BIGINT"),
    ("cik", "INTEGER"), ("tag", "VARCHAR"), ("taxonomy", "VARCHAR"), ("is_custom", "BOOLEAN"),
    ("uom", "VARCHAR"), ("qtrs", "INTEGER"),
    ("period_start", "DATE"), ("period_end", "DATE"),
    ("fiscal_year", "INTEGER"), ("fiscal_period", "VARCHAR"), ("calendar_frame", "VARCHAR"),
    ("value", "DECIMAL(28,4)"),
    ("accession", "VARCHAR"), ("form", "VARCHAR"), ("filed", "DATE"), ("is_amendment", "BOOLEAN"),
    ("dq_flags", "VARCHAR[]"), ("source", "VARCHAR"), ("derived_from", "BIGINT[]"),
]  # fmt: skip

FACTS_COLUMNS: list[tuple[str, str]] = [
    ("fact_id", "BIGINT"), ("fact_key", "BIGINT"),
    ("cik", "INTEGER"), ("tag", "VARCHAR"), ("taxonomy", "VARCHAR"), ("is_custom", "BOOLEAN"),
    ("uom", "VARCHAR"), ("qtrs", "INTEGER"),
    ("period_start", "DATE"), ("period_end", "DATE"),
    ("fiscal_year", "INTEGER"), ("fiscal_period", "VARCHAR"), ("calendar_frame", "VARCHAR"),
    ("value", "DECIMAL(28,4)"), ("accession", "VARCHAR"), ("form", "VARCHAR"), ("filed", "DATE"),
    ("is_latest", "BOOLEAN"), ("is_original", "BOOLEAN"), ("n_versions", "INTEGER"),
    ("is_restated", "BOOLEAN"),
    ("dq_flags", "VARCHAR[]"), ("derived_from", "BIGINT[]"), ("anomaly_score", "FLOAT"),
]  # fmt: skip

FACTS_DIMENSIONAL_COLUMNS: list[tuple[str, str]] = [
    ("fact_id", "BIGINT"), ("fact_key", "BIGINT"),
    ("cik", "INTEGER"), ("tag", "VARCHAR"), ("taxonomy", "VARCHAR"), ("is_custom", "BOOLEAN"),
    ("uom", "VARCHAR"), ("qtrs", "INTEGER"),
    ("segments", "VARCHAR"),
    ("period_start", "DATE"), ("period_end", "DATE"),
    ("fiscal_year", "INTEGER"), ("fiscal_period", "VARCHAR"), ("calendar_frame", "VARCHAR"),
    ("value", "DECIMAL(28,4)"), ("accession", "VARCHAR"), ("form", "VARCHAR"), ("filed", "DATE"),
    ("is_latest", "BOOLEAN"), ("is_original", "BOOLEAN"), ("n_versions", "INTEGER"),
    ("is_restated", "BOOLEAN"),
    ("dq_flags", "VARCHAR[]"),
]  # fmt: skip


# --------------------------------------------------------------------------------------
# SQL building blocks (each takes column names, returns an expression)
# --------------------------------------------------------------------------------------


def _id(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _fye_date(year: str, fye: str) -> str:
    """Rule 1: the fiscal-year-end date in calendar `year`; Feb 29 becomes Feb 28."""
    month = f"CAST(substr({fye}, 1, 2) AS INTEGER)"
    day = (
        f"CASE WHEN substr({fye}, 1, 4) = '0229' THEN 28 "
        f"ELSE CAST(substr({fye}, 3, 2) AS INTEGER) END"
    )
    return f"make_date({year}, {month}, {day})"


def _y_end(period_end: str, fye: str) -> str:
    """Rule 2: the smallest year y in (year-1, year, year+1) with period_end <= fye_date(y) + 7."""
    y = f"year({period_end})"
    return (
        f"CASE WHEN {period_end} <= {_fye_date(f'({y} - 1)', fye)} + 7 THEN {y} - 1 "
        f"WHEN {period_end} <= {_fye_date(y, fye)} + 7 THEN {y} "
        f"ELSE {y} + 1 END"
    )


def _quarter_index(period_end: str, fye: str, y_end: str) -> str:
    """Rule 4 helper: 1..3 by a 7-day tolerance on each quarter end, else 4."""
    fy_end = _fye_date(y_end, fye)

    def q_end(months_back: int) -> str:
        return f"CAST({fy_end} - to_months({months_back}) AS DATE) + 7"

    return (
        f"CASE WHEN {period_end} <= {q_end(9)} THEN 1 "
        f"WHEN {period_end} <= {q_end(6)} THEN 2 "
        f"WHEN {period_end} <= {q_end(3)} THEN 3 ELSE 4 END"
    )


def _period_start(period_end: str, qtrs: str) -> str:
    start = f"CAST(({period_end} + 1) - to_months(3 * {qtrs}) AS DATE)"
    return f"CASE WHEN {qtrs} = 0 THEN NULL ELSE {start} END"


def _calendar_frame(period_end: str, qtrs: str) -> str:
    """Rule 6."""
    y = f"year({period_end})"
    prev_dec, this_dec = f"make_date({y} - 1, 12, 31)", f"make_date({y}, 12, 31)"
    d_prev, d_this = f"abs({period_end} - {prev_dec})", f"abs({period_end} - {this_dec})"
    return (
        f"CASE WHEN {qtrs} = 4 THEN "
        f"CASE WHEN least({d_prev}, {d_this}) <= 30 THEN "
        f"'CY' || CAST(CASE WHEN {d_prev} <= {d_this} THEN {y} - 1 ELSE {y} END AS VARCHAR) END "
        f"WHEN {qtrs} IN (0, 1) THEN "
        f"'CY' || CAST({y} AS VARCHAR) || 'Q' || CAST(quarter({period_end}) AS VARCHAR) "
        f"|| CASE WHEN {qtrs} = 0 THEN 'I' ELSE '' END END"
    )


def _hash63(*exprs: str) -> str:
    return f"CAST(hash({', '.join(exprs)}) & {_MASK} AS BIGINT)"


def _null_safe_eq(left: str, right: str, cols: Sequence[str]) -> str:
    return " AND ".join(f"{left}.{_id(c)} IS NOT DISTINCT FROM {right}.{_id(c)}" for c in cols)


# --------------------------------------------------------------------------------------
# derive_fiscal
# --------------------------------------------------------------------------------------


def _derive_fiscal_sql(columns: Sequence[str], source: str, extra: Sequence[str]) -> str:
    src = _id(source)
    cols = ", ".join(_id(c) for c in columns)
    group = ["cik", "tag", "taxonomy", "uom", "fiscal_year", *extra]
    g = ", ".join(_id(c) for c in group)
    g_pe = f"{g}, period_end"
    key_cols = ["cik", "tag", "taxonomy", "period_end", "qtrs", "uom", *extra]
    m_keys = [f"s.{_id(c)}" for c in key_cols]
    d_keys = ["1" if c == "qtrs" else f"fyr.{_id(c)}" for c in key_cols]
    sub_y = _y_end("sub_period", "fye_mmdd")
    additive = "NOT (uom LIKE '%/%' OR uom IN ('pure', 'shares'))"
    native = "coalesce(sub_period = period_end, false)"
    tie = "value ASC NULLS LAST, period_end ASC, qtrs ASC, fact_id ASC"
    orig_order = f"CASE WHEN _hn AND NOT _nat THEN 1 ELSE 0 END, filed, adsh, {tie}"
    annual = ", ".join(_lit(f) for f in ANNUAL_FORMS)
    join_on = _null_safe_eq("a", "b", group)

    return f"""
WITH src_y AS (
  SELECT *, {_y_end("period_end", "fye_mmdd")} AS _y FROM {src}
),
src_q AS (
  SELECT *, {_quarter_index("period_end", "fye_mmdd", "_y")} AS _qi FROM src_y
),
subs AS (
  SELECT DISTINCT cik, adsh, form, sub_fy, sub_period, fye_mmdd FROM {src}
),
offs AS (
  SELECT cik, adsh, sub_fy - ({sub_y}) AS off
  FROM subs WHERE form IN ('10-K', '10-KT') AND sub_fy IS NOT NULL
),
off_count AS (SELECT cik, off, count(*) AS c, min(adsh) AS first_adsh FROM offs GROUP BY cik, off),
off_pick AS (
  SELECT cik, off AS _off, k = 1 AS _const FROM (
    SELECT cik, off, count(*) OVER (PARTITION BY cik) AS k,
           row_number() OVER (PARTITION BY cik ORDER BY c DESC, first_adsh ASC) AS rn
    FROM off_count
  ) WHERE rn = 1
),
m AS MATERIALIZED (
  SELECT s.* EXCLUDE (_y, _qi),
    {_period_start("s.period_end", "s.qtrs")} AS period_start,
    CAST(s._y + coalesce(o._off, 0) AS INTEGER) AS fiscal_year,
    CASE WHEN s.qtrs = 4 THEN 'FY' WHEN s.qtrs = 3 THEN '9M' WHEN s.qtrs = 2 THEN 'H1'
         WHEN s.qtrs = 0 AND s._qi = 4 THEN 'FY'
         ELSE 'Q' || CAST(s._qi AS VARCHAR) END AS fiscal_period,
    {_calendar_frame("s.period_end", "s.qtrs")} AS calendar_frame,
    CASE WHEN o._off IS NOT NULL AND NOT o._const THEN ['label_offset_varies']
         ELSE CAST([] AS VARCHAR[]) END AS dq_flags,
    {_hash63(*m_keys)} AS fact_key
  FROM src_q s LEFT JOIN off_pick o ON s.cik = o.cik
),
m2 AS MATERIALIZED (
  SELECT m.*, {_hash63("fact_key", "adsh")} AS fact_id,
         CAST(NULL AS BIGINT[]) AS derived_from
  FROM m
),
e AS MATERIALIZED (
  SELECT * EXCLUDE (_direct, _hasfy, _has9) FROM (
    SELECT m2.*,
      bool_or(qtrs = 1 AND fiscal_period = 'Q4') OVER w AS _direct,
      bool_or(qtrs = 4 AND form IN ({annual})) OVER w AS _hasfy,
      bool_or(qtrs = 3) OVER w AS _has9
    FROM m2 WHERE {additive}
    WINDOW w AS (PARTITION BY {g})
  ) WHERE NOT _direct AND _hasfy AND _has9
),
fy1 AS (SELECT e.*, {native} AS _nat FROM e WHERE qtrs = 4 AND form IN ({annual})),
fy2 AS (SELECT *, bool_or(_nat) OVER (PARTITION BY {g}) AS _hn FROM fy1),
fy3 AS (
  SELECT *,
    row_number() OVER (PARTITION BY {g} ORDER BY filed DESC, adsh DESC, {tie}) AS _rl,
    row_number() OVER (PARTITION BY {g} ORDER BY {orig_order}) AS _ro
  FROM fy2
),
fy_pick AS (
  SELECT {g},
    max(fact_id) FILTER (WHERE _ro = 1) AS o_id, max(adsh) FILTER (WHERE _ro = 1) AS o_adsh,
    max(value) FILTER (WHERE _ro = 1) AS o_value,
    max(fact_id) FILTER (WHERE _rl = 1) AS l_id0, max(adsh) FILTER (WHERE _rl = 1) AS l_adsh0,
    max(value) FILTER (WHERE _rl = 1) AS l_value
  FROM fy3 GROUP BY {g}
),
fy_tiers AS (
  SELECT {g}, o_id, o_adsh,
    CASE WHEN l_value = o_value THEN o_id ELSE l_id0 END AS l_id,
    CASE WHEN l_value = o_value THEN o_adsh ELSE l_adsh0 END AS l_adsh
  FROM fy_pick
),
nm1 AS (SELECT e.*, {native} AS _nat FROM e WHERE qtrs = 3),
nm2 AS (SELECT *, bool_or(_nat) OVER (PARTITION BY {g_pe}) AS _hn FROM nm1),
nm3 AS (
  SELECT *,
    row_number() OVER (PARTITION BY {g_pe} ORDER BY filed DESC, adsh DESC, {tie}) AS _rl,
    row_number() OVER (PARTITION BY {g_pe} ORDER BY {orig_order}) AS _ro
  FROM nm2
),
nm_pe AS (
  SELECT {g_pe},
    max(fact_id) FILTER (WHERE _ro = 1) AS o_id, max(filed) FILTER (WHERE _ro = 1) AS o_filed,
    max(adsh) FILTER (WHERE _ro = 1) AS o_adsh, max(value) FILTER (WHERE _ro = 1) AS o_value,
    max(fact_id) FILTER (WHERE _rl = 1) AS l_id0, max(filed) FILTER (WHERE _rl = 1) AS l_filed0,
    max(adsh) FILTER (WHERE _rl = 1) AS l_adsh0, max(value) FILTER (WHERE _rl = 1) AS l_value,
    min(struct_pack(a := adsh, vn := (value IS NULL), v := coalesce(value, 0), p := period_end))
      AS first_key
  FROM nm3 GROUP BY {g_pe}
),
nm_pe2 AS (
  SELECT {g}, first_key, o_id, o_filed, o_adsh,
    CASE WHEN l_value = o_value THEN o_id ELSE l_id0 END AS l_id,
    CASE WHEN l_value = o_value THEN o_filed ELSE l_filed0 END AS l_filed,
    CASE WHEN l_value = o_value THEN o_adsh ELSE l_adsh0 END AS l_adsh
  FROM nm_pe
),
nm_rank AS (
  SELECT *,
    row_number() OVER (PARTITION BY {g} ORDER BY o_filed ASC, o_adsh ASC, first_key ASC) AS _ro,
    row_number() OVER (PARTITION BY {g} ORDER BY l_filed DESC, l_adsh DESC, first_key ASC) AS _rl
  FROM nm_pe2
),
nm_pick AS (
  SELECT {g},
    struct_pack(id := max(o_id) FILTER (WHERE _ro = 1),
                filed := max(o_filed) FILTER (WHERE _ro = 1),
                adsh := max(o_adsh) FILTER (WHERE _ro = 1)) AS nmo,
    struct_pack(id := max(l_id) FILTER (WHERE _rl = 1),
                filed := max(l_filed) FILTER (WHERE _rl = 1),
                adsh := max(l_adsh) FILTER (WHERE _rl = 1)) AS nml
  FROM nm_rank GROUP BY {g}
),
pairs AS (
  SELECT {", ".join("a." + _id(c) for c in group)},
    a.o_id AS fy_o_id, a.l_id AS fy_l_id, a.o_adsh AS fy_o_adsh, a.l_adsh AS fy_l_adsh,
    b.nmo, b.nml,
    (a.o_adsh = a.l_adsh) AS _same,
    (a.o_adsh = a.l_adsh) AND (b.nml.filed > b.nmo.filed
       OR (b.nml.filed = b.nmo.filed AND b.nml.adsh > b.nmo.adsh)) AS _b_wins
  FROM fy_tiers a JOIN nm_pick b ON {join_on}
),
chosen AS (
  SELECT fy_o_id AS fy_id, nmo.id AS nm_id FROM pairs WHERE NOT _same OR NOT _b_wins
  UNION ALL
  SELECT fy_l_id AS fy_id, nml.id AS nm_id FROM pairs WHERE NOT _same OR _b_wins
),
d0 AS (
  SELECT fyr.* REPLACE (
      1 AS qtrs,
      CAST(fyr.value - nmr.value AS DECIMAL(28,4)) AS value,
      {_period_start("fyr.period_end", "1")} AS period_start,
      'Q4' AS fiscal_period,
      {_calendar_frame("fyr.period_end", "1")} AS calendar_frame,
      list_append(fyr.dq_flags, 'q4_derived') AS dq_flags,
      {_hash63(*d_keys)} AS fact_key
    ),
    fyr.fact_id AS _fy_id, nmr.fact_id AS _nm_id, nmr.adsh AS _nm_adsh
  FROM chosen c
  JOIN m2 fyr ON fyr.fact_id = c.fy_id
  JOIN m2 nmr ON nmr.fact_id = c.nm_id
),
derived AS (
  SELECT d0.* EXCLUDE (_fy_id, _nm_id, _nm_adsh) REPLACE (
      {_hash63("fact_key", "adsh", "_nm_adsh")} AS fact_id,
      [_fy_id, _nm_id] AS derived_from)
  FROM d0
)
SELECT {cols}, period_start, fiscal_year, fiscal_period, calendar_frame, dq_flags,
       fact_key, fact_id, derived_from
FROM (SELECT * FROM m2 UNION ALL SELECT * FROM derived)
"""


def derive_fiscal(
    con: duckdb.DuckDBPyConnection, source: str, extra_key_cols: Sequence[str] = ()
) -> duckdb.DuckDBPyRelation:
    """Add fiscal columns to consolidated facts and append derived Q4 rows (rules 1-6).

    Same contract as `pipelines.spark.fiscal.derive_fiscal_columns`; `source` names a table or
    view of `con`. See the module docstring about `fact_key` / `fact_id`.
    """
    columns = con.table(source).columns
    return con.sql(_derive_fiscal_sql(columns, source, list(extra_key_cols)))


# --------------------------------------------------------------------------------------
# select_versions
# --------------------------------------------------------------------------------------


def _select_versions_sql(columns: Sequence[str], source: str) -> str:
    src = _id(source)
    cols = ", ".join(_id(c) for c in columns)
    # Keep the order total so reruns cannot differ: the real row (no `derived_from`) comes first,
    # then the value. Since ADR-022 a derived Q4's fact_id includes the 9M accession, so it no
    # longer collides with a real row; this is defence in depth for any input that still ties.
    tie = (
        ", (derived_from IS NOT NULL), value NULLS LAST"
        if "derived_from" in columns
        else ", value NULLS LAST"
    )
    latest = f"PARTITION BY fact_key ORDER BY filed DESC, accession DESC, fact_id DESC{tie}"
    original = (
        "PARTITION BY fact_key ORDER BY "
        f"CASE WHEN _has_native AND NOT _native THEN 1 ELSE 0 END, filed, accession, fact_id{tie}"
    )
    return f"""
WITH s AS (
  SELECT *, coalesce(filing_period = period_end, false) AS _native FROM {src}
),
agg AS (
  SELECT fact_key, count(*) AS n_versions,
         count(DISTINCT value) + max(CAST(value IS NULL AS INTEGER)) AS _distinct,
         bool_or(_native) AS _has_native
  FROM s GROUP BY fact_key
),
r AS (
  SELECT s.*, agg.n_versions, agg._distinct > 1 AS is_restated, agg._has_native,
    row_number() OVER ({latest}) = 1 AS _is_latest,
    row_number() OVER ({original}) = 1 AS _is_original,
    first_value(value) OVER ({latest}) AS _latest_value,
    first_value(value) OVER ({original}) AS _original_value
  FROM s JOIN agg USING (fact_key)
),
c AS (
  SELECT *, _latest_value IS NOT DISTINCT FROM _original_value AS _collapsed FROM r
)
SELECT {cols},
  (_is_latest OR (_collapsed AND _is_original)) AS is_latest,
  _is_original AS is_original,
  CAST(n_versions AS INTEGER) AS n_versions,
  is_restated
FROM c
WHERE _is_original OR (_is_latest AND NOT _collapsed)
"""


def select_versions(con: duckdb.DuckDBPyConnection, source: str) -> duckdb.DuckDBPyRelation:
    """Latest and original version per `fact_key` (same contract as the Spark function)."""
    columns = con.table(source).columns
    return con.sql(_select_versions_sql(columns, source))


# --------------------------------------------------------------------------------------
# the whole job
# --------------------------------------------------------------------------------------

_FISCAL_INPUT = (
    "cik, adsh, tag, taxonomy, is_custom, uom, qtrs, period_end, value, form, filed, "
    "fye_mmdd, sub_fy, sub_period"
)


def _read_sql(files: Sequence[Path], columns: Sequence[str]) -> str:
    file_list = ", ".join(_lit(str(f)) for f in files)
    select = ", ".join(columns)
    return (
        f"SELECT {select}, "
        "regexp_extract(filename, '/(\\d{4}q[1-4])/[^/]+$', 1) AS src_quarter "
        f"FROM read_csv([{file_list}], delim='\\t', quote='\"', escape='\"', header=true, "
        "all_varchar=true, compression='gzip', filename=true)"
    )


def _conform(columns: list[tuple[str, str]], source: str) -> str:
    """Select exactly the contract columns, in order, with their types."""
    select = ", ".join(
        "CAST(NULL AS FLOAT) AS anomaly_score"
        if name == "anomaly_score"
        else f"CAST({_id(name)} AS {dtype}) AS {_id(name)}"
        for name, dtype in columns
    )
    return f"SELECT {select} FROM {_id(source)}"


def _with_history_columns(source: str) -> str:
    return (
        f"SELECT *, adsh AS accession, form LIKE '%/A' AS is_amendment, 'fsds' AS source, "
        f"sub_period AS filing_period FROM {_id(source)}"
    )


def run(
    src: Path,
    quarters: Sequence[str],
    out_dir: Path,
    *,
    threads: int | None = None,
    memory_limit: str | None = None,
) -> dict[str, int]:
    """Rebuild the three curated tables as Parquet under `out_dir` from the FSDS quarters."""
    quarters = [q.strip().lower() for q in quarters]  # as the Spark loader does
    out_dir.mkdir(parents=True, exist_ok=True)
    # spill files go to a temp directory that is removed afterwards, never into `out_dir`
    with tempfile.TemporaryDirectory(prefix="duckdb_spill_") as tmp:
        con = duckdb.connect()
        try:
            con.execute(f"SET temp_directory = {_lit(tmp)}")
            if threads is not None:
                con.execute(f"SET threads = {int(threads)}")
            if memory_limit is not None:
                con.execute(f"SET memory_limit = {_lit(memory_limit)}")
            return _run(con, src, quarters, out_dir)
        finally:
            con.close()


def build_inputs(con: duckdb.DuckDBPyConnection, src: Path, quarters: Sequence[str]) -> None:
    """Create the temp tables `consolidated` and `dimensional` (the fiscal step's inputs) from
    the FSDS files: every filter and dedupe of `normalize_facts.prepare_facts`."""
    sub_files = [src / q / "sub.txt.gz" for q in quarters]
    num_files = [src / q / "num.txt.gz" for q in quarters]
    to_date = "CAST(try_strptime(CAST({c} AS VARCHAR), '%Y%m%d') AS DATE)"

    sub_raw = _read_sql(sub_files, ["adsh", "cik", "fye", "form", "period", "fy", "filed"])
    con.execute(
        f"""
CREATE TEMP TABLE subs AS
SELECT adsh, TRY_CAST(cik AS INTEGER) AS cik, fye AS fye_mmdd, form,
       {to_date.format(c="filed")} AS filed, TRY_CAST(fy AS INTEGER) AS sub_fy,
       {to_date.format(c="period")} AS sub_period
FROM (SELECT *, row_number() OVER (PARTITION BY adsh ORDER BY src_quarter DESC) AS rn
      FROM ({sub_raw}))
WHERE rn = 1 AND NULLIF(fye, '') IS NOT NULL
"""
    )

    num_raw = _read_sql(
        num_files, ["adsh", "tag", "version", "ddate", "qtrs", "uom", "segments", "coreg", "value"]
    )
    con.execute(
        f"""
CREATE TEMP TABLE num_clean AS
SELECT adsh, tag, version, ddate, TRY_CAST(qtrs AS INTEGER) AS qtrs, uom,
       NULLIF(segments, '') AS segments, NULLIF(coreg, '') AS coreg,
       TRY_CAST(value AS DECIMAL(28,4)) AS value, src_quarter,
       {to_date.format(c="ddate")} AS period_end,
       version = adsh AS is_custom,
       CASE WHEN version = adsh THEN 'custom'
            ELSE regexp_extract(version, '^([^/]+)', 1) END AS taxonomy
FROM ({num_raw})
WHERE TRY_CAST(value AS DECIMAL(28,4)) IS NOT NULL AND {to_date.format(c="ddate")} IS NOT NULL
"""
    )
    con.execute(
        """
CREATE TEMP TABLE joined AS
SELECT n.adsh, s.cik, n.tag, n.taxonomy, n.is_custom, n.uom, n.qtrs, n.period_end, n.value,
       s.form, s.filed, s.fye_mmdd, s.sub_fy, s.sub_period, n.segments, n.coreg
FROM (
  SELECT *, row_number() OVER (
    PARTITION BY adsh, tag, taxonomy, ddate, qtrs, uom, segments, coreg
    ORDER BY version DESC, src_quarter DESC) AS rn
  FROM num_clean
) n JOIN subs s ON n.adsh = s.adsh
WHERE n.rn = 1
"""
    )
    con.execute(
        f"CREATE TEMP TABLE consolidated AS SELECT {_FISCAL_INPUT} FROM joined "
        "WHERE coreg IS NULL AND segments IS NULL"
    )
    con.execute(
        f"CREATE TEMP TABLE dimensional AS SELECT {_FISCAL_INPUT}, segments FROM joined "
        "WHERE coreg IS NULL AND segments IS NOT NULL"
    )


def _run(
    con: duckdb.DuckDBPyConnection, src: Path, quarters: Sequence[str], out_dir: Path
) -> dict[str, int]:
    build_inputs(con, src, quarters)
    counts: dict[str, int] = {}

    def write(name: str, columns: list[tuple[str, str]], source: str) -> None:
        path = out_dir / f"{name}.parquet"
        con.execute(f"COPY ({_conform(columns, source)}) TO {_lit(str(path))} (FORMAT parquet)")
        counts[name] = int(
            con.execute(f"SELECT count(*) FROM read_parquet({_lit(str(path))})").fetchone()[0]  # type: ignore[index]
        )

    con.execute(f"CREATE TEMP TABLE fiscal AS {derive_fiscal(con, 'consolidated').sql_query()}")
    con.execute(f"CREATE TEMP TABLE history AS {_with_history_columns('fiscal')}")
    write("facts_history", HISTORY_COLUMNS, "history")
    con.execute(f"CREATE TEMP TABLE facts AS {select_versions(con, 'history').sql_query()}")
    write("facts", FACTS_COLUMNS, "facts")

    con.execute(
        "CREATE TEMP TABLE fiscal_dim AS "
        f"{derive_fiscal(con, 'dimensional', ['segments']).sql_query()}"
    )
    con.execute(f"CREATE TEMP TABLE history_dim AS {_with_history_columns('fiscal_dim')}")
    con.execute(f"CREATE TEMP TABLE facts_dim AS {select_versions(con, 'history_dim').sql_query()}")
    write("facts_dimensional", FACTS_DIMENSIONAL_COLUMNS, "facts_dim")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize FSDS quarters with DuckDB.")
    parser.add_argument("--src", type=Path, default=Path("data/raw/fsds"))
    parser.add_argument("--quarters", required=True, help="comma-separated, e.g. 2023q3,2023q4")
    parser.add_argument("--out", type=Path, default=Path("warehouse_duckdb"))
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--memory-limit", default=None, help="e.g. 8GB")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    counts = run(
        args.src,
        [q.strip() for q in args.quarters.split(",")],
        args.out,
        threads=args.threads,
        memory_limit=args.memory_limit,
    )
    for name, n in counts.items():
        log.info("%s: %d rows", name, n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
