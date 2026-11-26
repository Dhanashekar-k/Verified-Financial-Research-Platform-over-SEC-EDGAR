"""``verify/numbers.py``: parsing, classification, precision, matching and D1 tolerance
(M06 3.1; ADR-049 and its spec decisions C, F and M, which win where the stub's docstring
disagrees). Written before the implementation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.verify import numbers
from app.engine.verify.numbers import ParsedNumber, matches, parse_numbers, value_tolerance
from tests.unit.engine.verify import verify_cases as vc

pytestmark = pytest.mark.xfail(
    condition=not numbers.IMPLEMENTED,
    raises=NotImplementedError,
    strict=True,
    reason="Shekar-owned verify/numbers.py is not implemented yet",
)

D = Decimal
REL = D("0.005")


def only(text: str) -> ParsedNumber:
    found = parse_numbers(text)
    assert len(found) == 1, f"expected one number in {text!r}, got {found}"
    return found[0]


def kinds(text: str) -> list[str]:
    return [n.kind for n in parse_numbers(text)]


# --- written forms ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "value", "precision", "scale", "hint"),
    [
        ("$383.3 billion", "383300000000", "50000000", "1000000000", "currency"),
        ("383,285 million", "383285000000", "500000", "1000000", None),
        ("$383,285,000,000", "383285000000", "0.5", "1", "currency"),
        ("$1.2B", "1200000000", "50000000", "1000000000", "currency"),
        ("$450K", "450000", "500", "1000", "currency"),
        ("$3.5M", "3500000", "50000", "1000000", "currency"),
        ("$2T", "2000000000000", "500000000000", "1000000000000", "currency"),
        ("12 thousand", "12000", "500", "1000", None),
        ("$1.5 trillion", "1500000000000", "50000000000", "1000000000000", "currency"),
        ("29.8%", "0.298", "0.0005", "1", "percent"),
        ("25 bps", "0.0025", "0.00005", "1", "bps"),
        ("1.5x", "1.5", "0.05", "1", "multiple"),
        ("(1,234)", "-1234", "0.5", "1", None),
        ("-3.1%", "-0.031", "0.0005", "1", "percent"),
        ("-$1.2 million", "-1200000", "50000", "1000000", "currency"),
        ("$6.13 per share", "6.13", "0.005", "1", "per_share"),
        ("15.7 billion shares", "15700000000", "50000000", "1000000000", "shares"),
        ("0", "0", "0.5", "1", None),
    ],
)
def test_written_form_is_a_value_in_base_units(
    written: str, value: str, precision: str, scale: str, hint: str | None
) -> None:
    """Every written form the spec lists parses to one value in base units, with the precision
    of the last place shown, its scale multiplier and its unit hint: the checks compare these
    numbers to XBRL facts, which are stored unscaled."""
    n = only(f"The figure was {written}.")
    assert n.kind == "value"
    assert (n.value, n.precision, n.scale) == (D(value), D(precision), D(scale))
    assert n.unit_hint == hint
    assert isinstance(n.value, Decimal) and isinstance(n.precision, Decimal)


def test_numbers_come_in_order_with_their_spans() -> None:
    """Every number is returned left to right, ``raw`` is exactly ``text[start:end]`` and spans
    do not overlap: callers look at the words around a number."""
    text = "Revenue was $383.3 billion in fiscal 2023, with a 29.8% margin per Item 7."
    found = parse_numbers(text)
    assert [n.kind for n in found] == ["value", "year", "value", "label"]
    assert all(n.raw == text[n.start : n.end] and n.start < n.end for n in found)
    assert all(a.end <= b.start for a, b in zip(found, found[1:], strict=False))
    for n, digits in zip(found, ["383.3", "2023", "29.8", "7"], strict=True):
        assert digits in n.raw


def test_text_without_digits_has_no_numbers() -> None:
    """No digits, no numbers (a spelled-out quarter is not a parsed number). The second half
    keeps this from passing on a parser that never finds anything."""
    assert parse_numbers("Management cited demand in the second quarter.") == []
    found = parse_numbers("Management cited 5 reasons in the second quarter.")
    assert [n.value for n in found if n.kind == "value"] == [D(5)]


def test_direction_word_does_not_change_the_parsed_sign() -> None:
    """ "decreased 36.7%" parses as +0.367: reading the direction word is numeric.py's job, so
    the parser must not sign it a second time."""
    assert only("Revenue decreased 36.7%.").value == D("0.367")


@pytest.mark.parametrize(
    "text",
    ["EPS guidance is $9.90 - $10.00.", "Growth of 6.2% - 7.2% is expected."],
)
def test_spaced_hyphen_in_a_range_is_not_a_minus_sign(text: str) -> None:
    """Both ends of a written range are positive. The text comes from the dev passages; reading
    the separator as a sign would drop true claims about the upper end."""
    found = parse_numbers(text)
    assert [n.kind for n in found] == ["value", "value"]
    assert all(n.value > 0 for n in found)


# --- years --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "year"),
    [
        ("In 2018 revenue rose.", 2018),
        ("In 2021, revenue rose.", 2021),
        ("Revenue rose in 2023.", 2023),
        ("FY2023 revenue rose.", 2023),
        ("Revenue rose in fiscal 1998.", 1998),
        ("It began in 1990.", 1990),
        ("The notes mature in 2099.", 2099),
        ("FY23 revenue rose.", 2023),
        ("Revenue rose in '23.", 2023),
    ],
)
def test_year_is_not_a_value(text: str, year: int) -> None:
    """A bare 1990..2099 integer, or the two-digit year of FY23 / '23, is a year. A year read
    as a value would drop every true claim that names its period (M06 pitfall). The comma that
    ends a clause is punctuation, not a thousands separator."""
    n = only(text)
    assert (n.kind, n.value) == ("year", D(year))


@pytest.mark.parametrize(
    "written",
    ["$2,022", "2,000", "$2020", "2020 million", "2020.5", "2020%", "1989", "2100"],
)
def test_four_digits_that_are_not_a_year_are_a_value(written: str) -> None:
    """A currency sign, comma, decimal point, percent or scale word makes four digits a value,
    and so does falling outside 1990..2099. Otherwise a wrong amount could hide as a year and
    never be checked."""
    assert only(f"The amount was {written} in total.").kind == "value"


# --- labels -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The 10-K describes the risk.",
        "The 10-Q describes the risk.",
        "An 8-K announced the change.",
        "See Item 7 for the discussion.",
        "See Item 1A for the risks.",
        "Registered under Section 12(b) of the Act.",
        "Employees participate in a 401(k) plan.",
        "Sales rose in Q2.",
        "Management said so [1].",
        "The 787 program is in production.",
        "The company is in the S&P 500.",
        "Demand for COVID-19 vaccines fell.",
    ],
)
def test_digits_that_name_something_are_labels(text: str) -> None:
    """Form names, item and section references, quarters, citation markers and product or index
    names assert no quantity, so no check may demand support for them."""
    assert kinds(text) == ["label"]


@pytest.mark.parametrize(
    ("text", "year"),
    [
        ("As of December 31, 2018, debt was unchanged.", 2018),
        ("As of December 31, 2021, debt was unchanged.", 2021),
        ("For the year ended September 30, 2023.", 2023),
    ],
)
def test_a_date_is_all_labels(text: str, year: int) -> None:
    """Decision C: the day and the year inside a date are labels. Neither is a value, and a
    date's year is not held to a fiscal year (a fiscal 2023 may end in January 2024)."""
    assert str(year) in text
    assert kinds(text) == ["label", "label"]


def test_asserted_zero_is_a_value_not_a_label() -> None:
    """A number the claim asserts as its answer is a value even when it looks odd: the "0" of
    fb-01328 must be checked against the quote."""
    found = parse_numbers("Restructuring costs directly outlined there are 0.")
    assert [(n.kind, n.value, n.precision) for n in found] == [("value", D(0), D("0.5"))]


def test_asserted_zero_before_a_citation_marker_stays_a_value() -> None:
    """The marker after the answer is a label and does not absorb the answer."""
    found = parse_numbers("Restructuring costs are 0 [1].")
    assert [n.kind for n in found] == ["value", "label"]


def test_small_count_asserted_by_the_sentence_is_a_value() -> None:
    """A small integer that states a quantity is not a label."""
    n = only("The company reports 5 operating segments.")
    assert (n.kind, n.value) == ("value", D(5))


# --- real dev claim texts -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question_id", "claim_id", "expected"),
    [
        # 787, 5 per month, "October 2023" (month and year: either kind, not pinned)
        ("fb-00494", "c2", ["label", "value", None]),
        ("fb-00746", "c1", ["label"]),  # 12(b)
        ("fb-00651", "c1", ["year", "value"]),  # 2023, 12.5%
        ("fb-00651", "c2", ["label", "year", "value", "value"]),  # Q2 2023, $2.80, 8.1%
        ("fb-00724", "c1", ["label", "year"]),  # Q2 2023
        ("fb-03282", "c1", ["label", "label", "value"]),  # December 31, 2017, 5,466,312
        ("fb-00723", "c1", ["value", "year"]),  # 24 percent, 2022
        ("fb-01328", "c1", ["year", "value"]),  # FY2022, the asserted 0
        ("fb-01328", "c2", ["value", "year"]),  # 411, 2022
    ],
)
def test_dev_claim_tokens_are_classified(
    question_id: str, claim_id: str, expected: list[str]
) -> None:
    """The numbers of real dev claims get the kinds the number-in-quote rule depends on: only
    the values need support in the quote. ``None``: "year" or "label", never "value"."""
    got = kinds(vc.dev_claim(question_id, claim_id).text)
    assert len(got) == len(expected), got
    for have, want in zip(got, expected, strict=True):
        assert have == want if want is not None else have in ("year", "label"), got


# --- matches ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "target", "expected"),
    [
        ("$383.3 billion", "383285000000", True),
        ("$385.0 billion", "383285000000", False),  # within 0.5%, still wrong as text
        ("$383.4 billion", "383285000000", False),
        ("383,285 million", "383285000000", True),
        ("383,286 million", "383285000000", False),
        ("$383.3 billion", "383350000000", True),  # exactly half a place: inclusive
        ("$383.3 billion", "383350000001", False),
        ("29.8%", "0.2982", True),
        ("29.9%", "0.2982", False),
        ("29.8%", "-0.298", False),  # sign counts
        ("(1,234)", "-1234", True),
        ("(1,234)", "1234", False),
        ("$383.3 million", "383285000000", False),  # million for billion
        ("0", "0.4", True),
        ("0", "1", False),
    ],
)
def test_text_match_uses_only_the_precision_shown(
    written: str, target: str, expected: bool
) -> None:
    """D1 for text: a number matches when it is within half of its own last place, signed. No
    relative slack, so "close but wrong" rounding fails."""
    assert matches(only(f"It was {written}."), D(target)) is expected


def test_matches_reads_only_value_and_precision() -> None:
    """``matches`` is ``|value - target| <= precision`` on the parsed fields, inclusive."""
    n = ParsedNumber(raw="10", start=0, end=2, kind="value", value=D(10), precision=D("0.5"))
    assert matches(n, D("10.5")) is True
    assert matches(n, D("9.5")) is True
    assert matches(n, D("10.5000001")) is False
    assert matches(n, D(-10)) is False


# --- D1 value tolerance -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "display", "expected"),
    [
        # the relative term wins
        ("383285000000", "$383.3 billion", "1916425000"),
        # the display's precision wins: 0.5% of 0.0123 is smaller than half of 0.1%
        ("0.0123", "1.2%", "0.0005"),
        ("6.13", "$6.13 per share", "0.03065"),
        # a zero target is held to the display's precision alone
        ("0", "$0", "0.5"),
        # negative targets use the magnitude
        ("-383285000000", "-$383.3 billion", "1916425000"),
    ],
)
def test_value_tolerance_is_the_larger_of_relative_and_display_precision(
    target: str, display: str, expected: str
) -> None:
    """D1 for ``claim.value`` and ``claim.display``: max(rel x |target|, display precision)."""
    assert value_tolerance(D(target), display, rel=REL) == D(expected)


@pytest.mark.parametrize("display", [None, "", "not available", "FY2023"])
def test_value_tolerance_without_a_displayed_value_is_relative_only(
    display: str | None,
) -> None:
    """With no display, or a display that writes no value (a year is not one), only the
    relative term applies: a year must not contribute a precision of half a unit."""
    assert value_tolerance(D("0.0123"), display, rel=REL) == D("0.0000615")


def test_value_tolerance_of_zero_without_display_demands_equality() -> None:
    """A zero target with nothing displayed has no slack at all."""
    assert value_tolerance(D(0), None, rel=REL) == D(0)


def test_value_tolerance_takes_rel_from_the_caller() -> None:
    """``rel`` comes from ``VerifyConfig``, not a constant inside the function."""
    assert value_tolerance(D(1000), None, rel=D("0.1")) == D(100)
    assert value_tolerance(D(1000), None, rel=D(0)) == D(0)


# --- C: fiscal years and dates -------------------------------------------------------------------


@pytest.mark.parametrize("text", ["It ended on 2024-01-31.", "It ended on 31 January 2024."])
def test_other_date_forms_hold_no_value_or_year(text: str) -> None:
    found = parse_numbers(text)
    assert found and all(n.kind == "label" for n in found), found


@pytest.mark.parametrize("text", ["fiscal 2024 sales", "FY2024 sales", "FY24 sales"])
def test_a_fiscal_year_is_a_year_even_beside_a_date(text: str) -> None:
    """ "fiscal YYYY" / "FYYYYY" is what numeric.py holds to fact.fiscal_year."""
    found = parse_numbers(f"In {text} through January 31, 2024, nothing changed.")
    assert (found[0].kind, found[0].value) == ("year", D(2024))
    assert [n.kind for n in found[1:]] == ["label", "label"]


# --- F: ordinals and "52-week" -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "It ranked 1st among peers.",
        "It ranked 2nd among peers.",
        "It ranked 23rd among peers.",
        "It ranked 4th among peers.",
        "The shares sit near a 52-week high.",
    ],
)
def test_ordinals_and_52_week_assert_no_quantity(text: str) -> None:
    assert kinds(text) == ["label"]


# --- M: a unit word means it is not a year -------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    ["2000 bps", "2050 basis points", "2050 shares", "1999 bps", "2023 shares"],
)
def test_a_number_followed_by_a_unit_word_is_not_a_year(written: str) -> None:
    """Decision M: read as a year it would never be checked."""
    [n] = parse_numbers(f"The change was {written} in total.")
    assert n.kind == "value"


# --- digits inside a company's name ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("3M Co reported liabilities of $35.4 billion.", "35400000000"),
        ("In its filing, 3M described a plan costing $1.5 billion.", "1500000000"),
    ],
)
def test_3m_is_a_name_not_three_million(text: str, value: str) -> None:
    """ "3M" is the company. Read with the letter scale M it would be an unsupported value and
    drop true claims (dev: num-0043 c1, fb-00499 c1). The real figure must still be found."""
    found = parse_numbers(text)
    assert [n.value for n in found if n.kind == "value"] == [D(value)]
    assert not [n for n in found if n.kind == "year"]


def test_dev_3m_claim_holds_one_value() -> None:
    """num-0043 c1: only "$35.4 billion" asserts a quantity."""
    found = parse_numbers(vc.dev_claim("num-0043", "c1").text)
    assert [n.value for n in found if n.kind == "value"] == [D("35400000000")]


# --- C before M ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "In fiscal 2023 shares outstanding rose.",
        "FY2023 shares outstanding rose.",
    ],
)
def test_a_fiscal_year_stays_a_year_before_a_unit_word(text: str) -> None:
    """ "fiscal YYYY" is a fiscal year (decision C) even when the next word is a unit word
    (decision M is about a bare number)."""
    [n] = parse_numbers(text)
    assert (n.kind, n.value) == ("year", D(2023))
