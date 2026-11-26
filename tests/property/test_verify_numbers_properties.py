"""Properties of ``verify/numbers.py``: round trips through the composer's formatter, scale words,
signs, percent and bps, years, labels and the D1 tolerances. Written before the implementation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from app.engine.compose.format import format_value
from app.engine.verify import numbers
from app.engine.verify.numbers import ParsedNumber, matches, parse_numbers, value_tolerance

pytestmark = pytest.mark.xfail(
    condition=not numbers.IMPLEMENTED,
    raises=NotImplementedError,
    strict=True,
    reason="Shekar-owned verify/numbers.py is not implemented yet",
)

D = Decimal


def decimals(limit: int | str, places: int) -> st.SearchStrategy[Decimal]:
    return st.decimals(
        min_value=-D(limit),
        max_value=D(limit),
        places=places,
        allow_nan=False,
        allow_infinity=False,
    )


# (unit, value) pairs over the ranges filings use, with extra weight around the scale steps.
FORMATTED = st.one_of(
    st.tuples(st.sampled_from(["USD", "shares"]), decimals(10**15, 2)),
    st.tuples(st.sampled_from(["USD", "shares"]), decimals(2 * 10**6, 2)),
    st.tuples(st.just("ratio"), decimals(100, 8)),
    st.tuples(st.just("USD/shares"), decimals(10_000, 4)),
)

SCALE_WORDS = {
    "thousand": D(10) ** 3,
    "million": D(10) ** 6,
    "billion": D(10) ** 9,
    "trillion": D(10) ** 12,
}
SCALE_LETTERS = {"K": D(10) ** 3, "M": D(10) ** 6, "B": D(10) ** 9, "T": D(10) ** 12}
MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def shown_precision(display: str, unit: str) -> Decimal:
    """Half of the last place of a ``format_value`` string, read off its documented forms."""
    if unit == "ratio":
        return D("0.0005")
    if unit == "USD/shares":
        return D("0.005")
    for word in ("million", "billion", "trillion"):
        if word in display:
            return D("0.05") * SCALE_WORDS[word]
    return D("0.5")


def values_of(text: str) -> list[ParsedNumber]:
    return [n for n in parse_numbers(text) if n.kind == "value"]


@given(FORMATTED)
def test_composer_display_parses_back_to_a_value_that_matches_its_source(
    pair: tuple[str, Decimal],
) -> None:
    """Whatever ``format_value`` writes parses to exactly one value that matches the source
    Decimal at the precision shown. The composer copies these strings into claims, so any
    failure here is a false drop of a correct claim."""
    unit, value = pair
    display = format_value(value, unit)
    [n] = parse_numbers(display)
    assert n.kind == "value"
    assert n.precision == shown_precision(display, unit)
    assert matches(n, value), (display, value, n)


@given(FORMATTED, st.integers(1990, 2099))
def test_composer_display_inside_a_sentence_keeps_value_and_year_apart(
    pair: tuple[str, Decimal], year: int
) -> None:
    """Surrounding prose, the sentence's period and a fiscal year do not change what the
    display parses to, and the year is never a second value."""
    unit, value = pair
    text = f"In fiscal {year}, the figure was {format_value(value, unit)}."
    found = parse_numbers(text)
    assert [n.kind for n in found] == ["year", "value"]
    assert found[0].value == D(year)
    assert matches(found[1], value)
    assert all(n.raw == text[n.start : n.end] for n in found)
    assert found[0].end <= found[1].start


@given(FORMATTED)
def test_a_target_two_places_away_does_not_match(pair: tuple[str, Decimal]) -> None:
    """The precision is not looser than what is written: a target a full place or more beyond
    the shown figure never matches."""
    unit, value = pair
    [n] = parse_numbers(format_value(value, unit))
    assert not matches(n, n.value + 3 * n.precision)
    assert not matches(n, n.value - 3 * n.precision)


@given(
    st.integers(0, 999_999),
    st.integers(0, 3),
    st.sampled_from(sorted(SCALE_WORDS) + sorted(SCALE_LETTERS)),
    st.booleans(),
)
def test_scale_word_or_letter_multiplies_value_and_precision(
    digits: int, places: int, word: str, negative: bool
) -> None:
    """Every scale word and letter multiplies both the value and the precision, so million and
    billion can never be confused."""
    coefficient = D(digits).scaleb(-places)
    written = f"{'-' if negative else ''}${coefficient:,.{places}f}"
    written += word if word in SCALE_LETTERS else f" {word}"
    scale = SCALE_WORDS.get(word) or SCALE_LETTERS[word]
    [n] = parse_numbers(f"It was {written} overall.")
    assert n.kind == "value"
    assert n.scale == scale
    assert n.value == (-coefficient if negative else coefficient) * scale
    assert n.precision == D("0.5").scaleb(-places) * scale


@given(st.integers(1_000, 10**12))
def test_parenthesized_amount_is_negative(amount: int) -> None:
    """Accounting parentheses mean negative, with the same precision as the bare number."""
    [n] = parse_numbers(f"The change was ({amount:,}) for the period.")
    assert (n.kind, n.value, n.precision) == ("value", D(-amount), D("0.5"))


@given(st.integers(-100_000, 100_000))
@example(2000)  # in the year range: a unit word follows, so it is a value (decision M)
def test_bps_and_percent_of_the_same_rate_are_the_same_fraction(bps: int) -> None:
    """N bps and N/100 percent are one number in base units, at the same precision, and 100
    times smaller than N percent."""
    [as_bps] = parse_numbers(f"The change was {bps} bps.")
    [as_pct] = parse_numbers(f"The change was {D(bps).scaleb(-2):.2f}%.")
    [bare_pct] = parse_numbers(f"The change was {bps}%.")
    assert (as_bps.unit_hint, as_pct.unit_hint) == ("bps", "percent")
    assert as_bps.value == as_pct.value == D(bps).scaleb(-4)
    assert as_bps.precision == as_pct.precision == D("0.00005")
    assert bare_pct.value == as_bps.value * 100


@given(
    st.integers(1990, 2099),
    st.sampled_from(["In {y}, sales rose.", "FY{y} sales rose.", "Sales rose in fiscal {y}."]),
)
def test_year_in_range_is_never_a_value(year: int, template: str) -> None:
    """No year from 1990 to 2099 is ever treated as a quantity."""
    [n] = parse_numbers(template.format(y=year))
    assert (n.kind, n.value) == ("year", D(year))


@given(st.integers(10, 35), st.sampled_from(["FY{y} sales rose.", "Sales rose in '{y}."]))
def test_two_digit_year_expands_to_this_century(short: int, template: str) -> None:
    """FY23 and '23 are the year 2023 (checked on a range with no century ambiguity)."""
    [n] = parse_numbers(template.format(y=short))
    assert (n.kind, n.value) == ("year", D(2000 + short))


@given(
    st.integers(1990, 2099),
    st.sampled_from(["${y}", "{y} million", "{y}.0", "{y}%"]),
)
def test_a_marked_four_digit_number_is_a_value_even_in_the_year_range(
    number: int, template: str
) -> None:
    """A currency sign, scale word, decimal point or percent overrides the year reading, so no
    amount escapes the checks by looking like a year."""
    assert [n.kind for n in parse_numbers(f"It was {template.format(y=number)} here.")] == ["value"]


LABEL_TEXTS = st.one_of(
    st.sampled_from(["10-K", "10-Q", "8-K", "401(k)", "S&P 500", "the 787 program"]).map(
        lambda s: f"This concerns {s} only."
    ),
    st.integers(1, 16).map(lambda i: f"See Item {i} for detail."),
    st.tuples(st.integers(1, 16), st.sampled_from("abg")).map(
        lambda p: f"Listed under Section {p[0]}({p[1]}) of the Act."
    ),
    st.integers(1, 4).map(lambda q: f"Sales rose in Q{q}."),
    st.integers(1, 40).map(lambda i: f"Management said so [{i}]."),
)


@given(LABEL_TEXTS)
def test_labels_are_never_values_or_years(text: str) -> None:
    """Digits that name something are never classified as a quantity."""
    found = parse_numbers(text)
    assert found and all(n.kind == "label" for n in found), (text, found)


@given(st.sampled_from(MONTHS), st.integers(1, 28), st.integers(1990, 2099))
def test_a_date_holds_no_value(month: str, day: int, year: int) -> None:
    """In "<month> <day>, <year>" the day and the year are labels, for every date (decision
    C)."""
    found = parse_numbers(f"As of {month} {day}, {year}, nothing changed.")
    assert [n.kind for n in found] == ["label", "label"]


@given(decimals(10**12, 4), decimals(10**6, 4), decimals(10**12, 4))
def test_matches_is_exactly_the_precision_interval(
    value: Decimal, precision: Decimal, target: Decimal
) -> None:
    """``matches`` is the closed interval value +/- precision, for any sign."""
    precision = abs(precision)
    n = ParsedNumber(raw="x", start=0, end=1, kind="value", value=value, precision=precision)
    assert matches(n, target) is (abs(value - target) <= precision)
    assert matches(n, value + precision) and matches(n, value - precision)
    assert not matches(n, value + precision + D("0.0001"))


@given(FORMATTED, st.sampled_from([D(0), D("0.001"), D("0.005"), D("0.05")]))
def test_value_tolerance_is_max_of_relative_and_shown_precision(
    pair: tuple[str, Decimal], rel: Decimal
) -> None:
    """D1 on composer displays: never tighter than the display's rounding (so a correct claim
    passes) and never looser than the larger of the two terms."""
    unit, value = pair
    display = format_value(value, unit)
    expected = max(rel * abs(value), shown_precision(display, unit))
    assert value_tolerance(value, display, rel=rel) == expected
    assert value_tolerance(value, None, rel=rel) == rel * abs(value)
