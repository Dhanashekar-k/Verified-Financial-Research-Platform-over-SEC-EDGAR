"""Differential tests: the Spark functions must agree with the pure-Python reference.

`derive_fiscal_columns` vs `derive_fiscal`, and `select_versions` vs `select_versions_ref`, on a
seeded synthetic data set of about 10,000 fact rows. The data is sampled by WHOLE COMPANY, not
by row: label offsets are a per-company statistic, and Q4 derivation needs a fact's FY and 9M
rows together, so row sampling would break exactly the logic under test.

These stay RED until Shekar implements the three owned modules. The generator itself is tested
(and green) so a red run means the implementations disagree, not that the data is empty.
Spark-vs-DuckDB (M01 section 3.7) is a separate test, written with the DuckDB twin.
"""

from __future__ import annotations

import calendar
import random
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pyspark.sql import SparkSession
from pyspark.sql.types import (
    BooleanType,
    DateType,
    DecimalType,
    IntegerType,
    LongType,
    StringType,
    StructField,
    StructType,
)

from pipelines.common.fiscal_ref import (
    FactInput,
    FactVersion,
    derive_fiscal,
    select_versions_ref,
)
from pipelines.spark.fiscal import derive_fiscal_columns
from pipelines.spark.versions import select_versions

TARGET_ROWS = 10_000
FYE_CHOICES = ["0930", "0630", "1231", "0131", "0331", "0531", "1231", "0630"]
FLOW_TAGS = ["Revenues", "NetIncomeLoss", "CostOfRevenue", "OperatingIncomeLoss"]


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def add_months(d: date, months: int) -> date:
    """Month-end arithmetic: d is a month end; the result is a month end."""
    total = d.year * 12 + (d.month - 1) + months
    return month_end(total // 12, total % 12 + 1)


# --------------------------------------------------------------------------------------
# Generator: companies -> filings -> facts (with FSDS-style month-end dates and some
# 52/53-week-style jitter so the +-7 day tolerance is exercised)
# --------------------------------------------------------------------------------------


@dataclass
class Generated:
    facts: list[FactInput]
    n_fy_9m_pairs: int
    n_eps_pairs: int
    n_instants: int
    n_late_jan_offset_minus_one: int
    n_direct_q4: int


def generate(seed: int = 2026, target_rows: int = TARGET_ROWS) -> Generated:
    rng = random.Random(seed)
    facts: list[FactInput] = []
    fy_9m = eps_pairs = instants = late_jan_m1 = direct_q4 = 0
    cik = 1000
    while len(facts) < target_rows:
        cik += 1
        fye = rng.choice(FYE_CHOICES)
        fye_month = int(fye[:2])
        offset = -1 if fye == "0131" and rng.random() < 0.5 else 0
        jitter_company = rng.random() < 0.3  # 52/53-week style: exact dates off month end
        late_jan_m1 += 1 if offset == -1 else 0
        for ye in range(2019, 2025):  # calendar year in which the fiscal year ends
            fy_end = month_end(ye, fye_month)
            filings = [("10-K", fy_end, 4, 0)] + [
                ("10-Q", add_months(fy_end, -3 * (4 - q)), q, 0) for q in (1, 2, 3)
            ]
            for form, period, q, _ in filings:
                shift = rng.randint(-6, 6) if jitter_company else 0
                period_exact = period + timedelta(days=shift)
                adsh = f"{cik:010d}-{ye % 100:02d}-{q:06d}"
                filed = period_exact + timedelta(days=rng.randint(25, 60))
                sub_fy = ye + offset
                common = {
                    "cik": cik,
                    "adsh": adsh,
                    "taxonomy": "us-gaap",
                    "form": form,
                    "filed": filed,
                    "fye_mmdd": fye,
                    "sub_fy": sub_fy,
                    "sub_period": period_exact,
                }
                durations = {1: [1], 2: [1, 2], 3: [1, 3], 4: [4]}[q]
                for tag in FLOW_TAGS:
                    for qtrs in durations:
                        if rng.random() < 0.1:
                            continue  # a missing fact: some FY or 9M rows have no partner
                        facts.append(
                            FactInput(
                                **common,
                                tag=tag,
                                uom="USD",
                                qtrs=qtrs,
                                period_end=period_exact,
                                value=Decimal(rng.randint(1, 10**9)) + Decimal("0.0000"),
                            )
                        )
                if q == 4:
                    for tag in FLOW_TAGS:
                        if rng.random() < 0.15:
                            direct_q4 += 1
                            facts.append(  # reported directly in the 10-K: no Q4 may be derived
                                FactInput(
                                    **common,
                                    tag=tag,
                                    uom="USD",
                                    qtrs=1,
                                    period_end=period_exact,
                                    value=Decimal(rng.randint(1, 10**9)),
                                )
                            )
                for qtrs in (3, 4) if q in (3, 4) else ():
                    facts.append(  # per-share: never derivable
                        FactInput(
                            **common,
                            tag="EarningsPerShareDiluted",
                            uom="USD/shares",
                            qtrs=qtrs,
                            period_end=period_exact,
                            value=Decimal(rng.randint(1, 5000)) / 100,
                        )
                    )
                facts.append(  # an instant
                    FactInput(
                        **common,
                        tag="Assets",
                        uom="USD",
                        qtrs=0,
                        period_end=period_exact,
                        value=Decimal(rng.randint(1, 10**10)),
                    )
                )
                instants += 1
        by_key = {(f.cik, f.tag, f.qtrs, f.period_end.year): f for f in facts if f.cik == cik}
        for f in [f for f in facts if f.cik == cik and f.qtrs == 4 and f.uom == "USD"]:
            if any(
                g.qtrs == 3 and g.tag == f.tag and 80 <= (f.period_end - g.period_end).days <= 100
                for g in facts
                if g.cik == cik
            ):
                fy_9m += 1
        eps_pairs += sum(1 for k in by_key if k[1] == "EarningsPerShareDiluted" and k[2] == 4)
    return Generated(facts, fy_9m, eps_pairs, instants, late_jan_m1, direct_q4)


def test_generator_produces_the_cases_the_differential_needs():
    gen = generate()

    assert len(gen.facts) >= TARGET_ROWS
    assert gen.n_fy_9m_pairs > 100  # Q4 derivation has real work to do
    assert gen.n_eps_pairs > 50  # and per-share facts to refuse
    assert gen.n_instants > 100
    assert gen.n_late_jan_offset_minus_one > 0  # the label-offset case is present
    assert gen.n_direct_q4 > 20  # the 'a directly reported Q4 wins' rule has cases
    assert len({f.cik for f in gen.facts}) >= 10
    assert Counter(f.qtrs for f in gen.facts).keys() >= {0, 1, 2, 3, 4}
    assert len({f.fye_mmdd for f in gen.facts}) >= 4
    off_month_end = [
        f for f in gen.facts if f.period_end != month_end(f.period_end.year, f.period_end.month)
    ]
    assert off_month_end, "no 52/53-week style dates were generated"
    assert generate() == generate(), "the generator must be deterministic"


# --------------------------------------------------------------------------------------
# fiscal: Spark vs reference
# --------------------------------------------------------------------------------------

FISCAL_SCHEMA = StructType(
    [
        StructField("cik", IntegerType()),
        StructField("adsh", StringType()),
        StructField("tag", StringType()),
        StructField("taxonomy", StringType()),
        StructField("is_custom", BooleanType()),
        StructField("uom", StringType()),
        StructField("qtrs", IntegerType()),
        StructField("period_end", DateType()),
        StructField("value", DecimalType(28, 4)),
        StructField("form", StringType()),
        StructField("filed", DateType()),
        StructField("fye_mmdd", StringType()),
        StructField("sub_fy", IntegerType()),
        StructField("sub_period", DateType()),
    ]
)


def to_spark(spark: SparkSession, facts: list[FactInput]):
    rows = [
        (f.cik, f.adsh, f.tag, f.taxonomy, False, f.uom, f.qtrs, f.period_end, f.value, f.form,
         f.filed, f.fye_mmdd, f.sub_fy, f.sub_period)
        for f in facts
    ]  # fmt: skip
    return spark.createDataFrame(rows, FISCAL_SCHEMA)


Key = tuple[int, str, str, str, str, int, date]  # cik, adsh, tag, taxonomy, uom, qtrs, period_end


def test_derive_fiscal_columns_agrees_with_the_python_reference(spark: SparkSession):
    gen = generate()
    expected = derive_fiscal(gen.facts)
    out = derive_fiscal_columns(to_spark(spark, gen.facts)).collect()

    # Row identity: the natural key (cik, adsh, tag, taxonomy, uom, qtrs, period_end).
    actual = {(r.cik, r.adsh, r.tag, r.taxonomy, r.uom, r.qtrs, r.period_end): r for r in out}
    assert len(actual) == len(out), "duplicate natural keys in the Spark output"
    ref = {
        (x.fact.cik, x.fact.adsh, x.fact.tag, x.fact.taxonomy, x.fact.uom, x.fact.qtrs,
         x.fact.period_end): x
        for x in expected
    }  # fmt: skip
    assert len(ref) == len(expected), "duplicate natural keys in the reference output"
    assert set(actual) == set(ref), "the two implementations produce different row sets"

    fact_id_to_key = {r.fact_id: k for k, r in actual.items()}
    mismatches: list[str] = []
    for key, x in ref.items():
        r = actual[key]
        got_parents = (
            tuple(
                (fact_id_to_key[i][6], fact_id_to_key[i][5], fact_id_to_key[i][1])
                for i in r.derived_from
            )
            if r.derived_from is not None
            else None
        )
        checks = {
            "period_start": (r.period_start, x.period_start),
            "fiscal_year": (r.fiscal_year, x.fiscal_year),
            "fiscal_period": (r.fiscal_period, x.fiscal_period),
            "calendar_frame": (r.calendar_frame, x.calendar_frame),
            "dq_flags": (tuple(sorted(r.dq_flags)), tuple(sorted(x.dq_flags))),
            "value": (r.value, x.fact.value),
            "derived_from": (got_parents, x.derived_from),
        }
        for name, (got, want) in checks.items():
            if got != want:
                mismatches.append(f"{key} {name}: spark={got!r} reference={want!r}")
    assert not mismatches, f"{len(mismatches)} mismatches, first 10:\n" + "\n".join(mismatches[:10])


# --------------------------------------------------------------------------------------
# versions: Spark vs reference
# --------------------------------------------------------------------------------------

VERSIONS_SCHEMA = StructType(
    [
        StructField("fact_key", LongType()),
        StructField("fact_id", LongType()),
        StructField("period_end", DateType()),
        StructField("value", DecimalType(28, 4)),
        StructField("accession", StringType()),
        StructField("form", StringType()),
        StructField("filed", DateType()),
        StructField("is_amendment", BooleanType()),
        StructField("filing_period", DateType()),
    ]
)


def generate_versions(seed: int = 7, n_keys: int = 2500) -> list[FactVersion]:
    rng = random.Random(seed)
    out: list[FactVersion] = []
    next_id = 1
    for key in range(1, n_keys + 1):
        period_end = month_end(2018 + key % 6, 1 + key % 12)
        n = rng.choice([1, 1, 2, 2, 3, 4])
        base = Decimal(rng.randint(1, 10**6))
        for v in range(n):
            # ties on `filed` are deliberate: they exercise the accession tie-break
            filed = period_end + timedelta(days=30 + 30 * rng.randint(0, 3) + 365 * (v // 2))
            # Several versions can cover the period (a 10-K and its 10-K/A); sometimes none do.
            covers = rng.random() < (0.8 if v == 0 else 0.4)
            out.append(
                FactVersion(
                    fact_key=key,
                    fact_id=next_id,
                    period_end=period_end,
                    value=base if rng.random() < 0.5 else base + rng.randint(1, 3),
                    # the leading part is random so accession order differs from version order
                    accession=f"{key:010d}-{rng.randint(0, 99):02d}-{v:06d}",
                    form="10-K/A" if rng.random() < 0.15 else "10-K",
                    filed=filed,
                    is_amendment=rng.random() < 0.15,
                    filing_period=period_end if covers else period_end + timedelta(days=366),
                )
            )
            next_id += 1
    return out


def test_version_generator_has_ties_restatements_and_single_versions():
    versions = generate_versions()
    by_key = Counter(v.fact_key for v in versions)

    assert len(versions) > 4000
    assert {1, 2, 3, 4} <= set(by_key.values())
    covering = Counter(v.fact_key for v in versions if v.filing_period == v.period_end)
    assert any(n >= 2 for n in covering.values()), "no group has several covering versions"
    ties = Counter((v.fact_key, v.filed) for v in versions)
    assert any(n >= 2 for n in ties.values()), "no group has a tie on filed"
    assert generate_versions() == generate_versions()


def test_select_versions_agrees_with_the_python_reference(spark: SparkSession):
    versions = generate_versions()
    expected = select_versions_ref(versions)
    rows = [
        (v.fact_key, v.fact_id, v.period_end, v.value, v.accession, v.form, v.filed,
         v.is_amendment, v.filing_period)
        for v in versions
    ]  # fmt: skip
    out = select_versions(spark.createDataFrame(rows, VERSIONS_SCHEMA)).collect()

    got = Counter(
        (r.fact_key, r.accession, r.is_latest, r.is_original, r.n_versions, r.is_restated)
        for r in out
    )
    want = Counter(
        (
            s.version.fact_key,
            s.version.accession,
            s.is_latest,
            s.is_original,
            s.n_versions,
            s.is_restated,
        )
        for s in expected
    )
    assert got == want, (
        f"{sum((got - want).values())} rows only in Spark, "
        f"{sum((want - got).values())} only in the reference"
    )


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_fiscal_agreement_holds_for_other_seeds(spark: SparkSession, seed: int):
    """Different companies, fiscal year ends and gaps: not one lucky data set."""
    gen = generate(seed=seed, target_rows=2_000)
    expected = {
        (x.fact.cik, x.fact.adsh, x.fact.tag, x.fact.uom, x.fact.qtrs, x.fact.period_end): (
            x.fiscal_year,
            x.fiscal_period,
        )
        for x in derive_fiscal(gen.facts)
    }
    out = derive_fiscal_columns(to_spark(spark, gen.facts)).collect()
    actual = {
        (r.cik, r.adsh, r.tag, r.uom, r.qtrs, r.period_end): (r.fiscal_year, r.fiscal_period)
        for r in out
    }
    assert len(actual) == len(out), "Spark emitted duplicate rows"
    assert len(expected) == len(derive_fiscal(gen.facts))
    assert actual == expected
