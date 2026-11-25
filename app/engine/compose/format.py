"""Display strings for NumericResults (the only place a value is formatted, M05 section 3.4).

Values stay in base units (USD, shares) everywhere else. The composer receives the display string
ready-made and copies it, so the model never scales, rounds or converts a number. Decimal only.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_SCALES: tuple[tuple[Decimal, str], ...] = (
    (Decimal(10) ** 6, "million"),
    (Decimal(10) ** 9, "billion"),
    (Decimal(10) ** 12, "trillion"),
)
_TENTH = Decimal("0.1")


def _scaled(value: Decimal, noun: str, prefix: str) -> str:
    magnitude = abs(value)
    sign = "-" if value < 0 else ""
    fitting = [i for i, (factor, _) in enumerate(_SCALES) if magnitude >= factor]
    if not fitting:
        whole = magnitude.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return f"{sign if whole else ''}{prefix}{whole:,}{noun}"
    i = fitting[-1]
    scaled = (magnitude / _SCALES[i][0]).quantize(_TENTH, rounding=ROUND_HALF_UP)
    if scaled >= 1000 and i + 1 < len(_SCALES):  # 999.96 million is 1.0 billion once rounded
        i += 1
        scaled = (magnitude / _SCALES[i][0]).quantize(_TENTH, rounding=ROUND_HALF_UP)
    return f"{sign}{prefix}{scaled:,} {_SCALES[i][1]}{noun}"


def format_value(value: Decimal, unit: str) -> str:
    """``$383.3 billion``, ``-$1.2 million``, ``29.8%``, ``$6.13 per share``,
    ``15.7 billion shares``

    A ratio or growth rate (a fraction) is shown as a percentage with one decimal.
    """
    if unit == "USD":
        return _scaled(value, "", "$")
    if unit == "shares":
        return _scaled(value, " shares", "")
    if unit == "USD/shares":
        cents = abs(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{'-' if value < 0 and cents else ''}${cents:,} per share"
    if unit == "ratio":
        pct = (value * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return f"{abs(pct) if not pct else pct}%"  # never "-0.0%"
    raise ValueError(f"unknown unit {unit!r}")
