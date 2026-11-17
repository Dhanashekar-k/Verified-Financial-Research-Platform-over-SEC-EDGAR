"""Shared scenarios and a tie-heavy generator for the two rules of ADR-022.

The rules (oracle: `pipelines.common.fiscal_ref.derive_fiscal`):

1. DETERMINISM. Every tie the reference breaks by list position is resolved as if the input were
   sorted by `stable_key` (accession, value ascending NULLs last, period_end, qtrs, cik, tag,
   taxonomy, uom). The output must not depend on input row order.
2. ELIGIBILITY. A Q4 is derived only from an FY (qtrs 4) row whose form is 10-K, 10-KT or 10-K/A.
   An FY row of any other form is never a parent. The 9M parent may have any form.
3. UNIQUENESS (twins only). A derived Q4's `fact_id` incorporates the 9M accession, so it never
   equals the `fact_id` of a real fact, even one with the same `fact_key` and accession.

Every number in the scenarios below was derived by hand from those rules and the fiscal-period
rules of M01 section 3.5, not by running an implementation. The comment next to each expectation
says why. The scenarios reuse the `Sub` / `scenario` / `ex` / `row` machinery and the checkers of
`tests/fiscal_cases.py`, so the reference and both twins are held to the same expectations.

`tie_heavy(seed)` is a seeded random generator whose data is full of the ties above.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from pipelines.common.fiscal_ref import (
    FactInput,
    FiscalFact,
    SubmissionPeriod,
    label_offsets,
    y_end,
)
from tests.fiscal_cases import FiscalScenario, Sub, acc, ex, row, scenario

# --------------------------------------------------------------------------------------
# Input orders
# --------------------------------------------------------------------------------------

ORDERS = ("given", "reversed", "shuffle_1", "shuffle_2", "shuffle_3")


def signature(out: Sequence[Any]) -> list[str]:
    """An order-free, comparable form of reference output."""
    return sorted(
        repr((ff.fact, ff.period_start, ff.fiscal_year, ff.fiscal_period, ff.calendar_frame,
              sorted(ff.dq_flags), ff.derived_from))
        for ff in out
    )  # fmt: skip


def reorder(facts: Sequence[FactInput], order: str) -> list[FactInput]:
    """The facts in the named order: as given, reversed, or one of three seeded shuffles."""
    rows = list(facts)
    if order == "reversed":
        rows.reverse()
    elif order.startswith("shuffle_"):
        random.Random(int(order.rsplit("_", 1)[1]) * 7919).shuffle(rows)
    elif order != "given":
        raise ValueError(f"unknown order {order!r}")
    return rows


# --------------------------------------------------------------------------------------
# Normalising and comparing twin output
# --------------------------------------------------------------------------------------

NaturalKey = tuple[Any, ...]


def normalise_output(raw: list[dict[str, Any]], *, full_keys: bool = False) -> list[dict[str, Any]]:
    """Twin output rows (dicts) with `dq_flags` as a frozenset and `derived_from` fact_ids resolved
    through the output's OWN `fact_id` column.

    The parents resolve to (period_end, qtrs, adsh) by default, or with `full_keys` to the full
    natural key (cik, adsh, tag, taxonomy, uom, qtrs, period_end). `fact_id` must be unique.
    """
    by_id = {r["fact_id"]: r for r in raw}
    assert len(by_id) == len(raw), "fact_id must be unique across the output"
    rows = []
    for r in raw:
        assert r["dq_flags"] is not None, "dq_flags must be an empty list, never NULL"
        resolved = None
        if r["derived_from"] is not None:
            parents = []
            for i in r["derived_from"]:
                assert i in by_id, f"derived_from names fact_id {i}, which is not in the output"
                p = by_id[i]
                parents.append(
                    (
                        p["cik"],
                        p["adsh"],
                        p["tag"],
                        p["taxonomy"],
                        p["uom"],
                        p["qtrs"],
                        p["period_end"],
                    )
                    if full_keys
                    else (p["period_end"], p["qtrs"], p["adsh"])
                )
            resolved = tuple(parents)
        rows.append({**r, "dq_flags": frozenset(r["dq_flags"]), "derived_from": resolved})
    return rows


def id_signature(raw: list[dict[str, Any]]) -> frozenset[tuple[Any, ...]]:
    """(natural key, is_derived, fact_key, fact_id) of every output row: what must not change
    with the order the input rows were given in (the hash is a pure function of the row)."""
    sig = frozenset(
        (
            r["cik"],
            r["adsh"],
            r["tag"],
            r["taxonomy"],
            r["uom"],
            r["qtrs"],
            r["period_end"],
            r["derived_from"] is not None,
            r["fact_key"],
            r["fact_id"],
        )  # fmt: skip
        for r in raw
    )
    assert len(sig) == len(raw), "two output rows share a natural key, derived flag and fact_id"
    return sig


def _row_key(r: dict[str, Any]) -> NaturalKey:
    return (
        r["cik"], r["adsh"], r["tag"], r["taxonomy"], r["uom"], r["qtrs"], r["period_end"],
        r["derived_from"] is not None,
    )  # fmt: skip


def compare_to_reference(
    rows: list[dict[str, Any]], expected: Sequence[FiscalFact], limit: int = 10
) -> list[str]:
    """Row-set and column comparison of normalised twin rows (`full_keys=True`) with reference
    output. Rows are matched on (natural key, is_derived). Returns up to `limit` problems."""
    got: dict[NaturalKey, dict[str, Any]] = {}
    for r in rows:
        key = _row_key(r)
        assert key not in got, f"the twin emitted two rows for {key}"
        got[key] = r
    want: dict[NaturalKey, FiscalFact] = {}
    for ff in expected:
        f = ff.fact
        key = (
            f.cik,
            f.adsh,
            f.tag,
            f.taxonomy,
            f.uom,
            f.qtrs,
            f.period_end,
            ff.derived_from is not None,
        )
        assert key not in want, f"the reference emitted two rows for {key}"
        want[key] = ff
    problems = [f"only in the twin: {k}" for k in sorted(set(got) - set(want), key=repr)]
    problems += [f"only in the reference: {k}" for k in sorted(set(want) - set(got), key=repr)]
    for key in sorted(set(got) & set(want), key=repr):
        r, ff = got[key], want[key]
        f = ff.fact
        parents = (
            tuple((f.cik, a, f.tag, f.taxonomy, f.uom, q, pe) for pe, q, a in ff.derived_from)
            if ff.derived_from is not None
            else None
        )
        checks = {
            "period_start": (r["period_start"], ff.period_start),
            "fiscal_year": (r["fiscal_year"], ff.fiscal_year),
            "fiscal_period": (r["fiscal_period"], ff.fiscal_period),
            "calendar_frame": (r["calendar_frame"], ff.calendar_frame),
            "dq_flags": (sorted(r["dq_flags"]), sorted(ff.dq_flags)),
            "value": (r["value"], f.value),
            "derived_from": (r["derived_from"], parents),
        }
        problems += [
            f"{key} {name}: twin={got_v!r} reference={want_v!r}"
            for name, (got_v, want_v) in checks.items()
            if got_v != want_v
        ]
    return problems[:limit]


# --------------------------------------------------------------------------------------
# Hand-derived scenarios S1 to S5
# --------------------------------------------------------------------------------------

REV = "Revenues"
FY_END = date(2023, 9, 30)  # S1 and S5 fiscal year end, FYE '0930'
NM_END = date(2023, 7, 1)  # the 9M period end of S1: 91 days before FY_END


def _s1(form: str, derives: bool) -> FiscalScenario:
    """S1 eligibility, one FY row from a `form` filing and one 9M row from a 10-Q.

    FY 100, 9M 60. Derived Q4 = 100 - 60 = 40 iff the FY form is in {10-K, 10-KT, 10-K/A}
    (rule 2), otherwise the FY row is not a parent and nothing is derived. fiscal_year is
    y_end(2023-09-30, '0930') + 0 = 2023, period_start 2023-09-30 + 1 day - 3 months = 2023-07-01.
    """
    cik = 3_000_001
    fy_sub = Sub(cik, acc(cik, 23, 1), form, date(2023, 11, 3), "0930", 2023, FY_END)
    q_sub = Sub(cik, acc(cik, 23, 2), "10-Q", date(2023, 8, 4), "0930", 2023, NM_END)
    name = f"s1_fy_from_{form.replace('/', '_')}_" + ("derives" if derives else "is_never_a_parent")
    derived = (
        [
            ex(
                fy_sub,
                REV,
                FY_END,
                1,
                2023,
                "Q4",
                "CY2023Q3",
                flags=["q4_derived"],
                value=Decimal("40.0000"),
                period_start=date(2023, 7, 1),
                derived_from=((FY_END, 4, fy_sub.adsh), (NM_END, 3, q_sub.adsh)),
            )  # fmt: skip
        ]
        if derives
        else []
    )
    return scenario(
        name,
        [
            row(fy_sub, REV, FY_END, 4, 2023, "FY", None, value="100.0000"),
            row(q_sub, REV, NM_END, 3, 2023, "9M", None, value="60.0000"),
        ],
        derived,
    )


def _s1_mixed() -> FiscalScenario:
    """S1 mixed: a 10-Q FY row (999) filed AFTER the 10-K, the 10-K FY row (100), a 9M (60).

    Only the 10-K row is an eligible parent, so exactly ONE Q4 is derived: 100 - 60 = 40, from the
    10-K row and the 9M row. The 10-Q row (a comparative annual figure in a later 10-Q) is kept as
    an ordinary row but is never a parent: no 999 - 60 = 939 Q4 and no row stamped with the
    10-Q's accession.
    """
    cik = 3_000_002
    k = Sub(cik, acc(cik, 23, 1), "10-K", date(2023, 11, 3), "0930", 2023, FY_END)
    n = Sub(cik, acc(cik, 23, 2), "10-Q", date(2023, 8, 4), "0930", 2023, NM_END)
    t = Sub(cik, acc(cik, 24, 1), "10-Q", date(2024, 2, 2), "0930", 2024, date(2023, 12, 30))
    return scenario(
        "s1_ten_q_fy_row_and_ten_k_fy_row_derive_exactly_one_q4",
        [
            row(k, REV, FY_END, 4, 2023, "FY", None, value="100.0000"),
            row(n, REV, NM_END, 3, 2023, "9M", None, value="60.0000"),
            row(t, REV, FY_END, 4, 2023, "FY", None, value="999.0000"),
        ],
        [
            ex(
                k,
                REV,
                FY_END,
                1,
                2023,
                "Q4",
                "CY2023Q3",
                flags=["q4_derived"],
                value=Decimal("40.0000"),
                period_start=date(2023, 7, 1),
                derived_from=((FY_END, 4, k.adsh), (NM_END, 3, n.adsh)),
            )  # fmt: skip
        ],
    )


def _s2() -> FiscalScenario:
    """S2 fiscal-year change: one 10-K, one filing date, two FY rows in the same fiscal year.

    FYE '1231'; the 10-K's sub.period is 2023-03-31 (a transition filing), so neither FY row is
    native (sub_period != period_end) and both have the same (filed, accession). Both rows and the
    9M row (2022-09-30, a 10-Q) have y_end 2022 and offset 2023 - y_end(2023-03-31) = 0, so they
    share one group. The tie goes to the row that sorts first, the SMALLER value (100, period_end
    2022-06-30): Q4 = 100 - 50 = 50 with period_end 2022-06-30, period_start 2022-06-30 + 1 day -
    3 months = 2022-04-01, in every input order. (Never 200 - 50 = 150 at 2022-12-31.)
    """
    cik = 3_000_003
    k = Sub(cik, acc(cik, 23, 1), "10-K", date(2023, 4, 14), "1231", 2023, date(2023, 3, 31))
    q = Sub(cik, acc(cik, 22, 3), "10-Q", date(2022, 11, 4), "1231", 2022, date(2022, 9, 30))
    jun, dec, sep = date(2022, 6, 30), date(2022, 12, 31), date(2022, 9, 30)
    return scenario(
        "s2_fiscal_year_change_two_fy_rows_smaller_value_wins",
        [
            row(k, REV, jun, 4, 2022, "FY", None, value="100.0000"),
            row(q, REV, sep, 3, 2022, "9M", None, value="50.0000"),
            row(k, REV, dec, 4, 2022, "FY", "CY2022", value="200.0000"),
        ],
        [
            ex(
                k,
                REV,
                jun,
                1,
                2022,
                "Q4",
                "CY2022Q2",
                flags=["q4_derived"],
                value=Decimal("50.0000"),
                period_start=date(2022, 4, 1),
                derived_from=((jun, 4, k.adsh), (sep, 3, q.adsh)),
            )  # fmt: skip
        ],
    )


def _s3(name: str, small_is_offset_zero: bool) -> FiscalScenario:
    """S3 label-offset tie: two 10-Ks, one fact each, offsets 0 and -1 once each (a 1-1 tie).

    The offset of the submission with the SMALLER accession wins. With the offset-0 submission
    first: fiscal_year = y_end + 0 for both facts (2021 and 2023). With the offset -1 submission
    first (the mirror): y_end - 1 for both (2020 and 2022). Both carry label_offset_varies.
    """
    cik = 3_000_004 if small_is_offset_zero else 3_000_005
    small, large = acc(cik, 22, 1), acc(cik, 23, 1)
    zero_adsh, minus_adsh = (small, large) if small_is_offset_zero else (large, small)
    # offset 0: fye 1231, sub_fy 2021, sub_period 2021-12-31 -> 2021 - y_end(2021-12-31) = 0
    zero = Sub(cik, zero_adsh, "10-K", date(2022, 2, 15), "1231", 2021, date(2021, 12, 31))
    # offset -1: fye 0331, sub_fy 2022, sub_period 2023-03-31 -> 2022 - y_end(2023-03-31) = -1
    minus = Sub(cik, minus_adsh, "10-K", date(2023, 5, 20), "0331", 2022, date(2023, 3, 31))
    wins = 0 if small_is_offset_zero else -1
    flag = ["label_offset_varies"]
    # y_end is 2021 for the first instant and 2023 for the second; both are the fiscal year end
    # (quarter index 4, qtrs 0), so the period is 'FY' and the frames are CY2021Q4I / CY2023Q1I.
    zero_fact = row(
        zero, "Assets", date(2021, 12, 31), 0, 2021 + wins, "FY", "CY2021Q4I", flags=flag
    )
    minus_fact = row(
        minus, "Assets", date(2023, 3, 31), 0, 2023 + wins, "FY", "CY2023Q1I", flags=flag
    )
    return scenario(name, [zero_fact, minus_fact])


S3_SMALLEST_IS_ZERO = _s3(
    "s3_label_offset_tie_smallest_accession_has_offset_0",
    small_is_offset_zero=True,
)
S3_SMALLEST_IS_MINUS_ONE = _s3(
    "s3_label_offset_tie_smallest_accession_has_offset_minus_1",
    small_is_offset_zero=False,
)


def _s4() -> FiscalScenario:
    """S4 9M tie across period ends: two 9M rows of one accession, one filing date.

    9M ends 2022-09-30 (value 70) and 2022-06-30 (value 90); FY (2022-12-31) is 200; FYE '1231'.
    Both 9M period-end groups retain one row each, tied on (filed, accession). The group whose
    first row sorts first is the one with the smaller value (70): Q4 = 200 - 70 = 130, with
    parents (2022-12-31, 4, K) and (2022-09-30, 3, Q); period_start = 2022-12-31 + 1 day - 3 months
    = 2022-10-01. (Never 200 - 90 = 110.)
    """
    cik = 3_000_006
    k = Sub(cik, acc(cik, 23, 1), "10-K", date(2023, 2, 20), "1231", 2022, date(2022, 12, 31))
    q = Sub(cik, acc(cik, 22, 2), "10-Q", date(2022, 11, 4), "1231", 2022, date(2022, 9, 30))
    fy, sep, jun = date(2022, 12, 31), date(2022, 9, 30), date(2022, 6, 30)
    return scenario(
        "s4_nine_month_tie_across_period_ends_smaller_value_wins",
        [
            row(q, REV, jun, 3, 2022, "9M", None, value="90.0000"),
            row(k, REV, fy, 4, 2022, "FY", "CY2022", value="200.0000"),
            row(q, REV, sep, 3, 2022, "9M", None, value="70.0000"),
        ],
        [
            ex(
                k,
                REV,
                fy,
                1,
                2022,
                "Q4",
                "CY2022Q4",
                flags=["q4_derived"],
                value=Decimal("130.0000"),
                period_start=date(2022, 10, 1),
                derived_from=((fy, 4, k.adsh), (sep, 3, q.adsh)),
            )  # fmt: skip
        ],
    )


S5_CIK = 3_000_007
S5_K = Sub(S5_CIK, acc(S5_CIK, 23, 1), "10-K", date(2023, 11, 3), "0930", 2023, FY_END)
S5_N = Sub(S5_CIK, acc(S5_CIK, 23, 2), "10-Q", date(2023, 8, 4), "0930", 2023, date(2023, 6, 30))
S5_END = date(2022, 12, 31)  # the FY row's period end: 1 quarter after the fiscal year start


def _s5() -> FiscalScenario:
    """S5 identity: a derived Q4 and a real row with the same fact_key and the same accession.

    FYE '0930'. The 10-K carries an FY row ending 2022-12-31 (value 100) AND a real qtrs-1 row
    ending 2022-12-31 (value 7). Under FYE '0930', 2022-12-31 is quarter 1 (the nominal Q1 end is
    2022-12-30, within the 7-day window), so the real row is labelled Q1 and the direct-Q4 guard
    does not fire. Both rows have y_end 2023 (as has the 9M ending 2023-06-30, value 60), so
    Q4 = 100 - 60 = 40 is derived with the SAME natural key as the real row: (cik, K, tag,
    us-gaap, USD, qtrs 1, 2022-12-31). Only the fact_id can tell them apart.
    period_start of both = 2022-12-31 + 1 day - 3 months = 2022-10-01; frame CY2022Q4.
    """
    nm = date(2023, 6, 30)
    return scenario(
        "s5_derived_q4_shares_fact_key_and_accession_with_a_real_row",
        [
            row(S5_K, REV, S5_END, 4, 2023, "FY", "CY2022", value="100.0000"),
            row(S5_K, REV, S5_END, 1, 2023, "Q1", "CY2022Q4", value="7.0000"),
            row(S5_N, REV, nm, 3, 2023, "9M", None, value="60.0000"),
        ],
        [
            ex(
                S5_K,
                REV,
                S5_END,
                1,
                2023,
                "Q4",
                "CY2022Q4",
                flags=["q4_derived"],
                value=Decimal("40.0000"),
                period_start=date(2022, 10, 1),
                derived_from=((S5_END, 4, S5_K.adsh), (nm, 3, S5_N.adsh)),
            )  # fmt: skip
        ],
    )


def nine_month_accession_variants() -> tuple[list[FactInput], list[FactInput]]:
    """Two inputs identical except for the 9M row's accession (and so its filing).

    Same FY row (100, in one 10-K), same 9M period and value (60). The derived Q4 has the same
    fact_key and the same accession (the 10-K's) in both, so only the 9M accession can make its
    fact_id differ (rule 3).
    """
    cik = 3_000_008
    k = Sub(cik, acc(cik, 23, 1), "10-K", date(2023, 11, 3), "0930", 2023, FY_END)
    out = []
    for n_adsh in (acc(cik, 23, 2), acc(cik, 23, 9)):
        n = Sub(cik, n_adsh, "10-Q", date(2023, 8, 4), "0930", 2023, NM_END)
        out.append([k.fact(REV, FY_END, 4, "100.0000"), n.fact(REV, NM_END, 3, "60.0000")])
    return out[0], out[1]


S1_ELIGIBLE = [_s1(form, True) for form in ("10-K", "10-KT", "10-K/A")]
# 10-KT/A is a real EDGAR form but is not in the literal set {10-K, 10-KT, 10-K/A} of rule 2.
S1_INELIGIBLE = [_s1(form, False) for form in ("10-Q", "10-Q/A", "S-1", "10-KT/A")]
S1_MIXED = _s1_mixed()
S2 = _s2()
S4 = _s4()
S5 = _s5()

# All hand-derived scenarios, for the twins.
ALL_SCENARIOS: list[FiscalScenario] = [
    *S1_ELIGIBLE,
    *S1_INELIGIBLE,
    S1_MIXED,
    S2,
    S3_SMALLEST_IS_ZERO,
    S3_SMALLEST_IS_MINUS_ONE,
    S4,
    S5,
]
# The scenarios the UNMODIFIED reference gets wrong in at least one input order.
FAILS_ON_UNMODIFIED_REFERENCE: list[FiscalScenario] = [
    *S1_INELIGIBLE,
    S1_MIXED,
    S2,
    S3_SMALLEST_IS_ZERO,
    S3_SMALLEST_IS_MINUS_ONE,
    S4,
]


# --------------------------------------------------------------------------------------
# tie_heavy: seeded random data full of ties
# --------------------------------------------------------------------------------------

FLOW_TAGS = ("Revenues", "NetIncomeLoss")
VALUES = tuple(Decimal(v) for v in ("100.0000", "200.0000", "300.0000"))  # few: value ties
FYES = ("1231", "0930", "0630", "0131")
K_FORMS = ("10-K", "10-K", "10-K/A", "10-KT")
Q_FORMS = ("10-Q", "10-Q", "10-Q/A", "S-1")
YEARS = (2021, 2022)  # the calendar year in which each fiscal year ends


def month_end(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def add_months(d: date, months: int) -> date:
    """Month-end arithmetic: `d` is a month end and so is the result."""
    total = d.year * 12 + (d.month - 1) + months
    return month_end(total // 12, total % 12 + 1)


class _Company:
    """Accumulates the facts of one company, keeping natural keys and accessions unique."""

    def __init__(self, rng: random.Random, cik: int) -> None:
        self.rng = rng
        self.cik = cik
        self.facts: list[FactInput] = []
        self._keys: set[tuple[Any, ...]] = set()
        self._accessions: set[str] = set()

    def accession(self, yy: int) -> str:
        while True:
            adsh = acc(self.cik, yy, self.rng.randint(1, 30))
            if adsh not in self._accessions:
                self._accessions.add(adsh)
                return adsh

    def spellings(self, d: date, lo: int = 2, hi: int = 3) -> list[date]:
        """2-3 spellings of one period end: the date and 1-2 days before it."""
        return [d - timedelta(days=j) for j in range(self.rng.randint(lo, hi))]

    def add(
        self,
        sub: Sub,
        tag: str,
        qtrs: int,
        period_end: date,
        value: Decimal | None,
        uom: str = "USD",
    ) -> None:
        key = (sub.adsh, tag, "us-gaap", uom, qtrs, period_end)
        if key in self._keys:
            return
        self._keys.add(key)
        fact = sub.fact(tag, period_end, qtrs, Decimal(0), uom=uom)
        self.facts.append(replace(fact, value=value))

    def value(self) -> Decimal:
        return self.rng.choice(VALUES)


def _company(rng: random.Random, cik: int, two_offsets: bool) -> list[FactInput]:
    c = _Company(rng, cik)
    fye = rng.choice(FYES)
    month = int(fye[:2])
    base_offset = rng.choice((0, 0, -1))
    year_offsets = [0, -1]
    rng.shuffle(year_offsets)
    for yi, ye in enumerate(YEARS):
        # two-offset companies have exactly one 10-K/10-KT per year, with offsets 0 and -1: a tie
        off = year_offsets[yi] if two_offsets else base_offset
        fy_end = month_end(ye, month)
        nm_end = add_months(fy_end, -3)
        q_ends = (add_months(fy_end, -9), add_months(fy_end, -6), nm_end)

        # Annual accessions (10-K family): FY rows at 2-3 spellings of the FY end.
        if two_offsets:
            forms = [rng.choice(("10-K", "10-KT"))] + (["10-K/A"] if rng.random() < 0.5 else [])
        else:
            forms = [rng.choice(K_FORMS) for _ in range(rng.choice((1, 2, 3)))]
        for form in forms:
            sub = Sub(
                cik,
                c.accession(ye % 100),
                form,
                fy_end + timedelta(days=rng.choice((60, 60, 75))),
                fye,
                ye + off,
                fy_end - timedelta(days=rng.choice((0, 0, 1))),
            )
            for tag in FLOW_TAGS:
                for pe in c.spellings(fy_end):
                    if rng.random() < 0.9:
                        c.add(sub, tag, 4, pe, c.value())
                if rng.random() < 0.25:  # a 9M inside a 10-K (a transition report)
                    for pe in c.spellings(nm_end):
                        c.add(sub, tag, 3, pe, c.value())
                if rng.random() < 0.12:  # a directly reported Q4: blocks the derivation
                    c.add(sub, tag, 1, rng.choice(c.spellings(fy_end)), c.value())
            c.add(sub, "EarningsPerShareDiluted", 4, fy_end, c.value(), "USD/shares")
            c.add(sub, "Assets", 0, fy_end, None if rng.random() < 0.1 else c.value())

        # Quarterly accessions (10-Q, 10-Q/A, S-1): 9M rows, trailing-twelve-month FY rows,
        # comparatives of the FY in the next year's first quarter, quarters and instants.
        for _ in range(rng.choice((1, 2, 3))):
            kind = rng.choice((0, 1, 2, 2, 2, 3, 3))  # 2: Q3 (9M), 3: next year's Q1
            qe = add_months(fy_end, 3) if kind == 3 else q_ends[kind]
            sub = Sub(
                cik,
                c.accession(ye % 100),
                rng.choice(Q_FORMS),
                fy_end + timedelta(days=rng.choice((120, 135)))
                if kind == 3
                else qe + timedelta(days=rng.choice((35, 50))),
                fye,
                ye + off + (1 if kind == 3 else 0),
                qe,
            )
            for tag in FLOW_TAGS:
                if rng.random() < 0.7:
                    for pe in c.spellings(qe, 1, 2):
                        c.add(sub, tag, 1, pe, c.value())
                if kind == 2:
                    if rng.random() < 0.9:
                        for pe in c.spellings(nm_end):
                            c.add(sub, tag, 3, pe, c.value())
                    if rng.random() < 0.5:  # trailing twelve months, ending at the quarter end
                        for pe in c.spellings(nm_end):
                            c.add(sub, tag, 4, pe, c.value())
                if kind == 3 and rng.random() < 0.6:  # the FY as a comparative
                    for pe in c.spellings(fy_end):
                        c.add(sub, tag, 4, pe, c.value())
            if kind == 2:
                c.add(sub, "EarningsPerShareDiluted", 3, nm_end, c.value(), "USD/shares")
            c.add(sub, "Assets", 0, qe, None if rng.random() < 0.1 else c.value())
    return c.facts


def tie_heavy(seed: int, n_companies: int = 12) -> list[FactInput]:
    """A seeded, deterministic fact set in which ties are the rule, not the exception.

    Per company: 2 fiscal years; per year 1-3 annual accessions (forms 10-K, 10-K/A, 10-KT) with
    FY rows at 2-3 period-end spellings, 1-3 quarterly accessions (10-Q, 10-Q/A, S-1) with 9M rows
    (2-3 spellings), trailing-twelve-month and comparative FY (qtrs 4) rows, quarters, per-share
    and instant rows (some with NULL values). Few distinct filing dates and values, so ties on
    (filed, accession) and on value are common. Every fourth company has exactly two 10-K/10-KT
    submissions with label offsets 0 and -1 (a tie). All facts of one accession share
    form / sub_fy / sub_period / fye. Natural keys are unique. Rows come in generation order.
    """
    rng = random.Random(seed)
    facts: list[FactInput] = []
    for i in range(n_companies):
        facts.extend(_company(rng, 2_000_000 + i + 1, two_offsets=i % 4 == 3))
    keys = Counter((f.cik, f.adsh, f.tag, f.taxonomy, f.uom, f.qtrs, f.period_end) for f in facts)
    assert max(keys.values()) == 1, "tie_heavy produced a duplicate natural key"
    return facts


def offsets_tie(facts: Sequence[FactInput]) -> set[int]:
    """The ciks whose label offsets occur equally often and are not all equal (a mode tie)."""
    subs = {
        (f.cik, f.adsh): SubmissionPeriod(f.cik, f.adsh, f.form, f.sub_fy, f.sub_period, f.fye_mmdd)
        for f in facts
    }
    per_cik = {cik: lo for cik, lo in label_offsets(subs.values()).items() if not lo.is_constant}
    tied = set()
    for cik in per_cik:
        counts = Counter(
            s.sub_fy - _y_end(s)
            for s in subs.values()
            if s.cik == cik and s.form in ("10-K", "10-KT") and s.sub_fy is not None
        )
        top = counts.most_common()
        if len(top) > 1 and top[0][1] == top[1][1]:
            tied.add(cik)
    return tied


def _y_end(s: SubmissionPeriod) -> int:
    return y_end(s.sub_period, s.fye_mmdd)
