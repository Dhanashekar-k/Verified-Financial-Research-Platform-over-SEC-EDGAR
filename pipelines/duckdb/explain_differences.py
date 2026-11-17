"""Explain every row on which Spark and DuckDB disagree (M01 section 3.7, ADR-007).

    uv run python -m pipelines.duckdb.explain_differences --label 2q

`pipelines.duckdb.benchmark` compares the two engines' outputs row for row. When they disagree
this module sorts each differing row into the cause it traces to, so the disagreement is
explained and counted instead of being waved through (or tuned away). It reads the Parquet
outputs the benchmark leaves under `warehouse/bench/<label>/{spark,duckdb}` and merges its
result into `docs/milestones/M01-differential-analysis.json`.

Causes, checked in this order:

* `identity_collision`: the differing row's own fact (cik, tag, taxonomy, uom, qtrs, period_end,
  + segments) and accession have several history rows. A derived Q4 takes
  its FY parent's accession, so when that "FY" row is really a trailing-twelve-month value in a
  10-Q (or the filing is an S-1, S-4/A, ...) the derived row gets the same `fact_key`, accession
  and so the same `fact_id` as a real quarter row. The versions step then has to pick between
  rows that are indistinguishable by key, and the two engines pick differently. For
  `facts_dimensional`, which has no history table, a row whose counterpart on the other side has
  the same key but a different `q4_derived` flag counts too.
* `identity_collision_knock_on`: another accession of a fact that has such a collision; the
  versions step picked differently there as a consequence.
* `label_offset_tie`: the row's company has two label offsets (rule 3) in equal numbers, so the
  mode is a tie. The reference breaks it by input order, the SQL toward the smaller offset, and
  every row of the company gets another `fiscal_year` on one side. The tie set is recomputed
  from the source `sub` files with the reference's own `y_end`, not from the SQL under test;
  a row counts only if its counterpart on the other side differs in `fiscal_year` and in
  nothing else.
* `fy_tie`: the differing row is a derived Q4 whose (cik, tag, taxonomy, uom, fiscal_year)
  group has two FY (qtrs 4)
  or two 9M (qtrs 3) rows of one accession and filing date but different period ends (a fiscal
  year change). The rule picks one by input order, which differs between the engines.
* `fy_tie_knock_on`: an ordinary (not derived) row at a fact where either engine's history
  holds a derived Q4, in a group with a fiscal-year tie, whose counterpart differs only in
  the version flags (`is_latest`, `is_original`, `n_versions`, `is_restated`), or which exists on
  one side only because the other side's history holds a derived Q4 at its fact key. The tie
  changed which derived rows exist, so the version count (or the kept row) of the real row differs.
* `unexplained`: neither. A non-zero count here is a bug to chase.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

from pipelines.common.fiscal_ref import FactInput, derive_fiscal, y_end
from pipelines.duckdb.benchmark import (
    HASH_COLUMNS,
    TABLES,
    _git_sha,
    _normalized_sql,
    _parquet,
    compare_outputs,
    export_spark,
)
from pipelines.duckdb.normalize_facts_duckdb import build_inputs

log = logging.getLogger(__name__)

DEFAULT_RESULTS = Path("docs/milestones/M01-differential-analysis.json")
DEFAULT_BENCHMARK = Path("docs/milestones/M01-benchmark.json")
ANNUAL_FORMS = ("10-K", "10-K/A", "10-KT")
FACT_KEY = ["cik", "tag", "taxonomy", "uom", "qtrs", "period_end"]
GROUP_KEY = ["cik", "tag", "taxonomy", "uom", "fiscal_year"]
VERSION_FLAGS = ("is_latest", "is_original", "n_versions", "is_restated")
CAUSES = (
    "identity_collision",
    "identity_collision_knock_on",
    "label_offset_tie",
    "fy_tie",
    "fy_tie_knock_on",
    "unexplained",
)


def _same(keys: list[str], left: str, right: str) -> str:
    return " AND ".join(f'{left}."{k}" IS NOT DISTINCT FROM {right}."{k}"' for k in keys)


def _equal_except(columns: list[str], skip: Collection[str], left: str, right: str) -> str:
    """Every compared column equal on both rows except those in `skip` (null-safe)."""
    parts = [f'{left}."{c}" IS NOT DISTINCT FROM {right}."{c}"' for c in columns if c not in skip]
    return " AND ".join(parts) or "TRUE"


def _cols(keys: list[str]) -> str:
    return ", ".join(f'"{k}"' for k in keys)


def classify_differences(
    dir_a: Path,
    dir_b: Path,
    label_offset_tie_ciks: Collection[int] | Mapping[str, Collection[int]] = (),
    samples: dict[str, list[dict[str, Any]]] | None = None,
    sample_size: int = 10,
) -> dict[str, dict[str, int]]:
    """For each table, the number of differing rows (both sides together) per cause.

    `label_offset_tie_ciks` are the companies whose label-offset mode is a tie (see
    `label_offset_tie_ciks()`), for all tables or per table name. If `samples` is given it is
    filled, per table, with up to `sample_size` unexplained rows (each with its `side`).
    """
    con = duckdb.connect()
    result: dict[str, dict[str, int]] = {}
    try:
        for table in TABLES:
            head = con.execute(f"SELECT * FROM read_parquet('{_parquet(dir_a, table)}') LIMIT 0")
            all_columns = [d[0] for d in head.description]
            columns = [c for c in all_columns if c not in HASH_COLUMNS]
            has_parents = "derived_from" in all_columns
            extra = ["segments"] if "segments" in columns else []
            fact_key, group_key = [*FACT_KEY, *extra], [*GROUP_KEY, *extra]
            compared = [*columns, *(["parents"] if has_parents else [])]
            flag_cols = [c for c in VERSION_FLAGS if c in columns]
            ties = (
                label_offset_tie_ciks.get(table, ())
                if isinstance(label_offset_tie_ciks, Mapping)
                else label_offset_tie_ciks
            )
            con.execute("CREATE OR REPLACE TEMP TABLE tie_ciks (cik INTEGER)")
            if ties:
                con.executemany("INSERT INTO tie_ciks VALUES (?)", [(int(c),) for c in ties])
            a = _normalized_sql(dir_a, table, columns, has_parents)
            b = _normalized_sql(dir_b, table, columns, has_parents)
            con.execute(
                "CREATE OR REPLACE TEMP TABLE diffs AS "
                f"SELECT 'a' AS side, * FROM (SELECT * FROM ({a}) EXCEPT ALL SELECT * FROM ({b})) "
                f"UNION ALL SELECT 'b', * FROM (SELECT * FROM ({b}) EXCEPT ALL SELECT * FROM ({a}))"
            )
            history = "facts_dimensional" if table == "facts_dimensional" else "facts_history"
            for side, directory in (("a", dir_a), ("b", dir_b)):
                src = f"read_parquet('{_parquet(directory, history)}')"
                con.execute(
                    f"CREATE OR REPLACE TEMP TABLE multi_{side} AS SELECT {_cols(fact_key)}, "
                    f"accession FROM {src} GROUP BY {_cols(fact_key)}, accession "
                    "HAVING count(*) > 1"
                )
                con.execute(
                    f"CREATE OR REPLACE TEMP TABLE derived_{side} AS SELECT DISTINCT "
                    f"{_cols(fact_key)} FROM {src} WHERE list_contains(dq_flags, 'q4_derived')"
                )
                con.execute(
                    f"CREATE OR REPLACE TEMP TABLE tie_{side} AS SELECT DISTINCT "
                    f"{_cols(group_key)} FROM (SELECT {_cols(group_key)}, qtrs, filed, accession "
                    f"FROM {src} WHERE qtrs IN (3, 4) GROUP BY {_cols(group_key)}, qtrs, filed, "
                    "accession HAVING count(DISTINCT period_end) > 1)"
                )
            counterpart_keys = [*fact_key, "accession"]
            derived = "list_contains({r}.dq_flags, 'q4_derived')"
            con.execute(
                f"""
CREATE OR REPLACE TEMP TABLE classified AS
SELECT d.*,
  CASE
    -- the row's own fact and accession are shared by several history rows, or its counterpart
    -- on the other side has the same key but is (not) a derived Q4
    WHEN EXISTS (SELECT 1 FROM multi_a m WHERE {_same(counterpart_keys, "m", "d")})
      OR EXISTS (SELECT 1 FROM multi_b m WHERE {_same(counterpart_keys, "m", "d")})
      OR EXISTS (SELECT 1 FROM diffs o WHERE o.side <> d.side
                 AND {_same(counterpart_keys, "o", "d")}
                 AND {derived.format(r="o")} <> {derived.format(r="d")})
      THEN 'identity_collision'
    -- a tie company: the counterpart on the other side differs in fiscal_year and nothing else
    WHEN d.cik IN (SELECT cik FROM tie_ciks)
      AND EXISTS (SELECT 1 FROM diffs o WHERE o.side <> d.side
                  AND {_same(counterpart_keys, "o", "d")}
                  AND o.fiscal_year IS DISTINCT FROM d.fiscal_year
                  AND {_equal_except(compared, {"fiscal_year"}, "o", "d")})
      THEN 'label_offset_tie'
    -- another accession of a fact that has a collision elsewhere
    WHEN EXISTS (SELECT 1 FROM multi_a m WHERE {_same(fact_key, "m", "d")})
      OR EXISTS (SELECT 1 FROM multi_b m WHERE {_same(fact_key, "m", "d")})
      THEN 'identity_collision_knock_on'
    -- a derived Q4 whose FY/9M candidates tie on accession and filing date
    WHEN {derived.format(r="d")}
      AND (EXISTS (SELECT 1 FROM tie_a t WHERE {_same(group_key, "t", "d")})
           OR EXISTS (SELECT 1 FROM tie_b t WHERE {_same(group_key, "t", "d")}))
      THEN 'fy_tie'
    -- an ordinary row at a fact where either engine's history holds a derived Q4, in a group
    -- with a fiscal-year tie: the tie changed which derived rows exist, hence its version flags
    WHEN NOT {derived.format(r="d")}
      AND (EXISTS (SELECT 1 FROM derived_a x WHERE {_same(fact_key, "x", "d")})
           OR EXISTS (SELECT 1 FROM derived_b x WHERE {_same(fact_key, "x", "d")}))
      AND (EXISTS (SELECT 1 FROM tie_a t WHERE {_same(group_key, "t", "d")})
           OR EXISTS (SELECT 1 FROM tie_b t WHERE {_same(group_key, "t", "d")}))
      AND {"TRUE" if flag_cols else "FALSE"}
      AND (EXISTS (SELECT 1 FROM diffs o WHERE o.side <> d.side
                   AND {_same(counterpart_keys, "o", "d")}
                   AND {_equal_except(compared, flag_cols, "o", "d")})
           -- or the row is kept by one engine only, because the OTHER engine's history holds a
           -- derived Q4 at its fact key, which took the slot
           OR (NOT EXISTS (SELECT 1 FROM diffs o WHERE o.side <> d.side
                           AND {_same(counterpart_keys, "o", "d")})
               AND ((d.side = 'a' AND EXISTS (SELECT 1 FROM derived_b x
                                              WHERE {_same(fact_key, "x", "d")}))
                    OR (d.side = 'b' AND EXISTS (SELECT 1 FROM derived_a x
                                                 WHERE {_same(fact_key, "x", "d")})))))
      THEN 'fy_tie_knock_on'
    ELSE 'unexplained'
  END AS cause
FROM diffs d
"""  # noqa: E501
            )
            rows = con.execute("SELECT cause, count(*) FROM classified GROUP BY cause").fetchall()
            if samples is not None:
                cur = con.execute(
                    "SELECT * EXCLUDE (cause) FROM classified WHERE cause = 'unexplained' "
                    f"ORDER BY cik, tag, period_end, side LIMIT {int(sample_size)}"
                )
                names = [d[0] for d in cur.description]
                samples[table] = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
            counts = dict.fromkeys(CAUSES, 0)
            counts.update({cause: int(n) for cause, n in rows})
            result[table] = {"total": sum(counts.values()), **counts}
    finally:
        con.close()
    return result


def label_offset_tie_ciks(
    src: Path, quarters: Sequence[str], accessions: Collection[str]
) -> set[int]:
    """Companies whose rule-3 label offsets tie, recomputed from the FSDS `sub` files.

    Mirrors the pipeline's inputs: the latest `sub` row of each accession (by source quarter),
    dropped when its `fye` is blank; only 10-K and 10-KT submissions with a `fy`, and only those
    in `accessions` (the submissions that have facts in the table at hand). The offset is
    `fy - y_end(period, fye)`, with the reference's own `y_end`. A tie means the two most common
    offsets of a company occur equally often.
    """
    files = ", ".join("'" + str(src / q / "sub.txt.gz").replace("'", "''") + "'" for q in quarters)
    con = duckdb.connect()
    try:
        con.execute("CREATE TEMP TABLE accs (adsh VARCHAR)")
        if accessions:
            con.executemany("INSERT INTO accs VALUES (?)", [(a,) for a in accessions])
        rows = con.execute(
            f"""
SELECT cik, fye, fy, period FROM (
  SELECT adsh, TRY_CAST(cik AS INTEGER) AS cik, form, NULLIF(fye, '') AS fye,
         TRY_CAST(fy AS INTEGER) AS fy, period,
         row_number() OVER (PARTITION BY adsh
           ORDER BY regexp_extract(filename, '/(\\d{{4}}q[1-4])/[^/]+$', 1) DESC) AS rn
  FROM read_csv([{files}], delim='\t', quote='"', escape='"', header=true, all_varchar=true,
                compression='gzip', filename=true)
) WHERE rn = 1 AND fye IS NOT NULL AND form IN ('10-K', '10-KT') AND fy IS NOT NULL
  AND adsh IN (SELECT adsh FROM accs)
"""
        ).fetchall()
    finally:
        con.close()
    offsets: dict[int, Counter[int]] = {}
    for cik, fye, fy, period in rows:
        end = date(int(period[:4]), int(period[4:6]), int(period[6:8]))
        offsets.setdefault(int(cik), Counter())[int(fy) - y_end(end, fye)] += 1
    ties: set[int] = set()
    for cik, counter in offsets.items():
        top = counter.most_common(2)
        if len(top) == 2 and top[0][1] == top[1][1]:
            ties.add(cik)
    return ties


Signature = tuple[tuple[str, str, str], ...]  # sorted (period_end, accession, value) of derived Q4s


def reference_outputs_under_shuffles(
    inputs: Sequence[FactInput],
    in_series: Callable[[FactInput], bool],
    n: int = 60,
    seed: int = 1,
) -> Counter[Signature]:
    """The derived Q4 rows the REFERENCE gives for one series, over `n` random orders of `inputs`.

    One distinct signature means the reference's answer does not depend on the row order; several
    mean it does (a tie it breaks by position), and each engine's own answer may legitimately be
    any of them.
    """
    rng = random.Random(seed)
    outputs: Counter[Signature] = Counter()
    for _ in range(n):
        order = list(inputs)
        rng.shuffle(order)
        signature = tuple(
            sorted(
                (str(f.fact.period_end), f.fact.adsh, str(f.fact.value))
                for f in derive_fiscal(order)
                if f.derived_from is not None and in_series(f.fact)
            )
        )
        outputs[signature] += 1
    return outputs


def derived_signature(directory: Path, cik: int, tag: str, segments: str) -> Signature:
    """The derived Q4 rows of one dimensional series in an engine's `facts_dimensional`."""
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT period_end, accession, value FROM read_parquet(?) WHERE cik = ? AND tag = ? "
            "AND segments = ? AND list_contains(dq_flags, 'q4_derived')",
            [_parquet(directory, "facts_dimensional"), cik, tag, segments],
        ).fetchall()
    finally:
        con.close()
    return tuple(sorted((str(pe), str(acc), str(v)) for pe, acc, v in rows))


def check_unexplained_series(
    src: Path,
    quarters: Sequence[str],
    dir_a: Path,
    dir_b: Path,
    unexplained: Sequence[Mapping[str, Any]],
    n: int = 60,
) -> list[dict[str, Any]]:
    """For each dimensional series with an unexplained row: does the reference itself give
    different answers for different input orders, and are both engines' answers among them?"""
    series = sorted({(int(r["cik"]), str(r["tag"]), str(r["segments"])) for r in unexplained})
    if not series:
        return []
    con = duckdb.connect()
    try:
        build_inputs(con, src, quarters)
        results = []
        for cik, tag, segments in series:
            rows = con.execute(
                "SELECT cik, adsh, tag, taxonomy, uom, qtrs, period_end, value, form, filed, "
                "fye_mmdd, sub_fy, sub_period, segments FROM dimensional WHERE cik = ?",
                [cik],
            ).fetchall()
            # the same encoding the Spark twin uses: the series is part of the tag
            inputs = [
                FactInput(
                    r[0],
                    r[1],
                    f"{r[2]}\x1f{r[13]}",
                    r[3],
                    r[4],
                    r[5],
                    r[6],
                    r[7],
                    r[8],
                    r[9],
                    r[10],
                    r[11],
                    r[12],
                )  # fmt: skip
                for r in rows
            ]
            key = f"{tag}\x1f{segments}"
            outputs = reference_outputs_under_shuffles(inputs, lambda f, k=key: f.tag == k, n)
            a_sig = derived_signature(dir_a, cik, tag, segments)
            b_sig = derived_signature(dir_b, cik, tag, segments)
            results.append(
                {
                    "cik": cik,
                    "tag": tag,
                    "segments": segments,
                    "shuffles": n,
                    "distinct_reference_outputs": len(outputs),
                    "a_output_is_a_reference_output": a_sig in outputs,
                    "b_output_is_a_reference_output": b_sig in outputs,
                    "a_output": [list(x) for x in a_sig],
                    "b_output": [list(x) for x in b_sig],
                }
            )
    finally:
        con.close()
    return results


def identity_collisions(directory: Path) -> dict[str, Any]:
    """How often `facts_history` gives one `fact_id` to several rows, and where derived Q4
    rows come from (their form is the FY parent's form)."""
    con = duckdb.connect()
    try:
        src = f"read_parquet('{_parquet(directory, 'facts_history')}')"
        shared = con.execute(
            f"SELECT count(*), coalesce(sum(c), 0) FROM "
            f"(SELECT fact_id, count(*) AS c FROM {src} GROUP BY fact_id HAVING count(*) > 1)"
        ).fetchone()
        derived = con.execute(
            f"SELECT form, count(*) FROM {src} WHERE derived_from IS NOT NULL GROUP BY form"
        ).fetchall()
    finally:
        con.close()
    assert shared is not None
    by_form = {str(form): int(n) for form, n in derived}
    return {
        "fact_ids_shared_by_several_rows": int(shared[0]),
        "rows_with_a_shared_fact_id": int(shared[1]),
        "derived_q4_rows": sum(by_form.values()),
        "derived_q4_rows_by_form": dict(sorted(by_form.items())),
        "derived_q4_rows_from_other_than_10_k_forms": sum(
            n for f, n in by_form.items() if f not in ANNUAL_FORMS
        ),
    }


def _accessions(directory: Path, table: str) -> set[str]:
    """The accessions that have rows in the table the offsets are computed over."""
    history = "facts_dimensional" if table == "facts_dimensional" else "facts_history"
    con = duckdb.connect()
    try:
        src = f"read_parquet('{_parquet(directory, history)}')"
        return {r[0] for r in con.execute(f"SELECT DISTINCT accession FROM {src}").fetchall()}
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explain Spark vs DuckDB differences.")
    parser.add_argument("--label", required=True, help="the benchmark cell, e.g. 2q")
    parser.add_argument("--dir", type=Path, default=None, help="default warehouse/bench/<label>")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--src", type=Path, default=Path("data/raw/fsds"))
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument(
        "--repeat-against",
        default=None,
        metavar="NAMESPACE",
        help="also compare the Spark run of this cell with another Spark run of the same quarters "
        "(Iceberg namespace, e.g. lake.curated) to measure Spark's repeatability",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    base = args.dir or Path("warehouse/bench") / args.label
    spark_dir, duck_dir = base / "spark", base / "duckdb"  # a = Spark, b = DuckDB
    quarters = next(
        r["quarters"]
        for r in json.loads(args.benchmark.read_text())["runs"]
        if r["label"] == args.label
    )
    ties = {
        table: label_offset_tie_ciks(args.src, quarters, _accessions(spark_dir, table))
        for table in TABLES
    }
    samples: dict[str, list[dict[str, Any]]] = {}
    cell: dict[str, Any] = {
        "quarters": quarters,
        "label_offset_tie_ciks": {t: sorted(c) for t, c in ties.items()},
        "classification": classify_differences(spark_dir, duck_dir, ties, samples=samples),
        "identity_collisions": {
            "spark": identity_collisions(spark_dir),
            "duckdb": identity_collisions(duck_dir),
        },
    }
    if args.repeat_against:
        other = base / "repeat"
        export_spark("lake.raw", args.repeat_against, other, drop=False)
        cell["spark_repeatability"] = {
            "this_run": f"the Spark run of benchmark cell {args.label}",
            "other_run": (
                f"Iceberg namespace {args.repeat_against}, built earlier from the same raw quarters"
            ),
            "differences": compare_outputs(spark_dir, other),
        }
    cell["unexplained_rows"] = {t: rows for t, rows in samples.items() if rows}
    cell["unexplained_order_check"] = check_unexplained_series(
        args.src, quarters, spark_dir, duck_dir, samples.get("facts_dimensional", [])
    )
    existing: dict[str, Any] = (
        json.loads(args.results.read_text()) if args.results.is_file() else {}
    )
    cells = {k: v for k, v in existing.get("cells", {}).items() if k != args.label}
    cells[args.label] = cell
    out = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "note": "a = Spark, b = DuckDB. See the module docstring for the causes.",
        "cells": cells,
    }
    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.write_text(
        json.dumps(out, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    for table, c in cell["classification"].items():
        log.info("%s: %s", table, c)
    return 1 if any(c["unexplained"] for c in cell["classification"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
