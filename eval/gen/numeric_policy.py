"""Data-quality policies for numeric gold (M4 catalog review, ADR-044).

Pure functions over fact rows read from the serving ``facts`` view, used by
``eval/gen/make_numeric_set.py`` before any value becomes gold:

- **Scope.** Only consolidated totals: rows come from ``facts`` (consolidated by contract:
  ``coreg IS NULL`` and no segments, 05 section 2.2), never ``facts_dimensional``, and only the
  ``us-gaap`` taxonomy (a custom tag that happens to share a name is a different concept).
- **Period.** A duration metric needs ``qtrs = 4`` for FY and ``qtrs = 1`` for Q1..Q4; a
  year-to-date amount (H1 / 9M, ``qtrs`` 2 or 3) is never used as a quarter. ``qtrs = 0`` on a
  duration metric is QUARANTINED (excluded and logged), never silently admitted. An instant
  metric needs ``qtrs = 0``. A derived Q4 (``derived_from`` set: FY minus 9M) is excluded
  unless explicitly allowed, because it is computed, not reported.
- **Unit.** The fact's ``uom`` must equal the metric's unit exactly (an EPS fact in ``USD``
  instead of ``USD/shares`` is quarantined: the denominator may have been dropped).
- **Versions.** One declared policy, ``as_filed`` (ADR-017): gold is the value in the filing
  that first reported the period (``is_original``); the latest restated value is recorded next
  to it. ``latest_restated`` is the alternative; a set uses one policy for every row.
- **Tolerances.** Metric-aware, rounding- and scale-aware (``tolerance_for``), not a flat 1%.
- **Denominators.** Ratios and growth are undefined (abstain) when the denominator is <= 0 or
  near zero; margins are never clamped.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

VersionPolicy = Literal["as_filed", "latest_restated"]
DEFAULT_VERSION_POLICY: VersionPolicy = "as_filed"
TAXONOMY = "us-gaap"
QUARTERS = ("Q1", "Q2", "Q3", "Q4")
PERIODS = ("FY", *QUARTERS)


@dataclass(frozen=True, slots=True)
class Fact:
    """One row of the serving ``facts`` view (05 section 3), the columns the generator needs."""

    fact_id: int
    cik: int
    tag: str
    taxonomy: str
    uom: str
    qtrs: int
    period_start: date | None
    period_end: date
    fiscal_year: int
    fiscal_period: str
    value: Decimal
    accession: str
    form: str
    filed: date
    is_latest: bool
    is_original: bool
    is_restated: bool
    derived_from: tuple[int, ...] | None = None


@dataclass(frozen=True)
class Quarantined:
    fact_id: int
    cik: int
    tag: str
    fiscal_year: int
    fiscal_period: str
    qtrs: int
    uom: str
    reason: str


@dataclass(frozen=True)
class Versioned:
    """A fact's value under the declared version policy, with the other version recorded."""

    fact: Fact  # the row the policy selects (its value is gold)
    other: Fact | None  # the other version when the value was restated (None otherwise)
    policy: VersionPolicy

    @property
    def value(self) -> Decimal:
        return self.fact.value

    @property
    def latest_value(self) -> Decimal | None:
        if self.other is None:
            return None
        return self.other.value if self.policy == "as_filed" else self.fact.value

    @property
    def original_value(self) -> Decimal:
        if self.other is None or self.policy == "as_filed":
            return self.fact.value
        return self.other.value

    @property
    def is_restated(self) -> bool:
        return self.other is not None and self.other.value != self.fact.value


@dataclass
class ScreenLog:
    quarantined: list[Quarantined] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def quarantine(self, f: Fact, reason: str) -> None:
        self.quarantined.append(
            Quarantined(
                f.fact_id, f.cik, f.tag, f.fiscal_year, f.fiscal_period, f.qtrs, f.uom, reason
            )
        )

    def exclude(self, reason: str) -> None:
        self.excluded[reason] += 1


# --- screening ----------------------------------------------------------------------------------


def expected_qtrs(period_type: str, fiscal_period: str) -> int | None:
    """The qtrs a fact must carry for (period_type, fiscal_period); None = never admissible."""
    if fiscal_period not in PERIODS:
        return None  # H1 / 9M are year-to-date amounts, never a gold period
    if period_type == "instant":
        return 0
    return 4 if fiscal_period == "FY" else 1


def screen(
    f: Fact,
    *,
    unit: str,
    period_type: str,
    log: ScreenLog,
    allow_derived_q4: bool = False,
) -> bool:
    """True when the fact may become (part of) gold. Every rejection is counted; the ones the
    catalog review asked to inspect (qtrs = 0 on a duration metric, unit mismatch, an instant
    with qtrs != 0) are quarantined row by row."""
    if f.taxonomy != TAXONOMY:
        log.exclude("non_us_gaap_taxonomy")
        return False
    if f.uom != unit:
        log.quarantine(f, "uom_mismatch")
        return False
    if period_type == "duration" and f.qtrs == 0:
        log.quarantine(f, "qtrs0_on_duration_metric")
        return False
    if period_type == "instant" and f.qtrs != 0:
        log.quarantine(f, "nonzero_qtrs_on_instant_metric")
        return False
    want = expected_qtrs(period_type, f.fiscal_period)
    if want is None:
        log.exclude("not_a_gold_period")
        return False
    if f.qtrs != want:
        log.exclude("ytd_or_period_length_mismatch")
        return False
    if f.derived_from and not allow_derived_q4:
        log.exclude("derived_q4")
        return False
    return True


# --- versions -----------------------------------------------------------------------------------


def select_versions(
    facts: Iterable[Fact], policy: VersionPolicy = DEFAULT_VERSION_POLICY
) -> list[Versioned]:
    """Collapse facts to one Versioned per fact version group (cik, tag, uom, qtrs, period_end).

    ``facts`` holds the latest row and, when the value was restated, the original row too
    (05 section 2.2); a single row carries both flags. as_filed picks the original row,
    latest_restated the latest one; the other row is kept for the record."""
    groups: dict[tuple[int, str, str, int, date], list[Fact]] = defaultdict(list)
    for f in facts:
        groups[(f.cik, f.tag, f.uom, f.qtrs, f.period_end)].append(f)
    out: list[Versioned] = []
    for rows in groups.values():
        flag = "is_original" if policy == "as_filed" else "is_latest"
        chosen = [r for r in rows if getattr(r, flag)]
        if len(chosen) != 1:
            continue  # malformed group (no or several rows for the policy): never gold
        pick = chosen[0]
        others = [r for r in rows if r is not pick]
        out.append(Versioned(pick, others[0] if len(others) == 1 else None, policy))
    return out


# --- tolerances ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tolerance:
    """An answer v is correct when |v - gold| <= max(abs_, rel * |gold|), in the gold unit."""

    rel: Decimal
    abs_: Decimal
    basis: str

    def accepts(self, value: Decimal, gold: Decimal) -> bool:
        return abs(value - gold) <= max(self.abs_, self.rel * abs(gold))


# 4 significant figures: the largest relative rounding error is 0.5 / 1000 (a leading 1000).
SIG_FIG_REL = Decimal("0.0005")
PER_SHARE_ABS = Decimal("0.005")  # per-share amounts are reported to the cent
RATIO_ABS = Decimal("0.0005")  # margins and growth as fractions: 0.05 percentage points
AMOUNT_ABS = Decimal("0.5")  # half a base unit: makes gold == 0 well defined (exact zero)


def tolerance_for(unit: str, gold: Decimal) -> Tolerance:
    """Metric-aware tolerance (ADR-044).

    - USD and share amounts: 4 significant figures (rel 0.0005), the rounding of a stated
      "$383.3 billion"; absolute floor half a base unit, so a gold of 0 needs an exact 0.
      A flat 1% would accept an error of billions on a large company. (No reporting-scale
      inference: guessing the filer's unit from trailing zeros overstates it for round
      values, e.g. a gold of 0 or 3,000,000, and would loosen gold.)
    - per-share amounts: half a cent, no relative slack (1% of $0.05 would accept $0.0505).
    - ratios / growth (fractions): 0.05 percentage points absolute.
    """
    if unit in ("USD", "shares"):
        return Tolerance(SIG_FIG_REL, AMOUNT_ABS, f"{unit}: 4 significant figures (abs 0.5)")
    if unit == "USD/shares":
        return Tolerance(Decimal(0), PER_SHARE_ABS, "per share: half a cent")
    if unit == "ratio":
        return Tolerance(Decimal(0), RATIO_ABS, "fraction: 0.05 percentage points")
    raise ValueError(f"no tolerance rule for unit {unit!r}")


def agree(a: Decimal, b: Decimal, unit: str) -> bool:
    """Two candidate values are the same number when either one's tolerance accepts the other
    (used to decide whether candidate tags disagree MATERIALLY)."""
    return tolerance_for(unit, a).accepts(b, a) or tolerance_for(unit, b).accepts(a, b)


# --- denominators -------------------------------------------------------------------------------

MIN_DENOMINATOR_USD = Decimal(1_000_000)


def denominator_ok(value: Decimal, unit: str = "USD") -> str | None:
    """None when a ratio/growth denominator is usable, else the abstain reason. No clamping:
    a margin of -1,399% on tiny revenue is a denominator problem, not a value to clip."""
    if value <= 0:
        return "nonpositive_denominator"
    if unit == "USD" and value < MIN_DENOMINATOR_USD:
        return "near_zero_denominator"
    return None


def distinct_period_ends(facts: Sequence[Fact]) -> set[date]:
    return {f.period_end for f in facts}
