"""Normalization symmetry of ``verify/quotes.py``: any presentation change to the quote, the
passage or both leaves the comparison unchanged. Written before the implementation."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.engine.verify import numbers, quotes
from app.engine.verify.models import VerifyConfig
from app.engine.verify.quotes import check_text, locate_quote, normalize
from tests.unit.engine.verify import verify_cases as vc

pytestmark = pytest.mark.xfail(
    condition=not (quotes.IMPLEMENTED and numbers.IMPLEMENTED),
    raises=NotImplementedError,
    strict=True,
    reason="Shekar-owned verify/quotes.py and verify/numbers.py are not implemented yet",
)

SPACES = [" ", "  ", "\t", "\n", "\r\n", "\u00a0", "\u202f", " \u00a0\n", " \u200b "]
DASHES = ["-", "\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212"]
VARIANTS = {"'": ["'", "\u2018", "\u2019"], '"': ['"', "\u201c", "\u201d"], "-": DASHES}
ZERO_WIDTH = "\u200b"

# Words without digits, so that the number-in-quote rule cannot decide these cases.
WORDS = st.lists(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8)
    | st.sampled_from(["company's", '"core"', "year-over-year", "sales,", "rose."]),
    min_size=3,
    max_size=12,
)


@st.composite
def dirtied(draw: st.DrawFn, clean: str) -> str:
    """``clean`` with presentation changes only: case, space runs, quote and dash variants and
    zero-width spaces."""
    out = []
    for char in clean:
        if char == " ":
            out.append(draw(st.sampled_from(SPACES)))
        elif char in VARIANTS:
            out.append(draw(st.sampled_from(VARIANTS[char])))
        else:
            out.append(char.upper() if draw(st.booleans()) else char)
        if draw(st.integers(0, 9)) == 0:
            out.append(ZERO_WIDTH)
    return "".join(out)


@st.composite
def quote_in_passage(draw: st.DrawFn) -> tuple[str, str, str]:
    """(clean quote, dirtied quote, dirtied passage containing it between other words)."""
    words = draw(WORDS)
    start = draw(st.integers(0, len(words) - 3))
    end = draw(st.integers(start + 3, len(words)))
    quote = " ".join(words[start:end])
    passage = " ".join(["Lead", *words, "trail"])
    return quote, draw(dirtied(quote)), draw(dirtied(passage))


@given(st.data(), WORDS)
def test_presentation_changes_do_not_change_the_normal_form(
    data: st.DataObject, words: list[str]
) -> None:
    """A dirtied string normalizes to the same form as the clean one, so quote and passage are
    treated identically (M06 pitfall)."""
    clean = " ".join(words)
    assert normalize(data.draw(dirtied(clean))) == normalize(clean)


@given(st.text(max_size=200))
def test_normalize_is_idempotent_and_leaves_no_noise(text: str) -> None:
    """Normalizing twice changes nothing, and the result has no outer space, doubled space,
    zero-width space or variant quote or dash, for any input."""
    once = normalize(text)
    assert normalize(once) == once
    assert once == once.strip() and "  " not in once
    assert not set(once) & set("\u200b\u00a0\n\t\u2018\u2019\u201c\u201d")
    assert not set(once) & set(DASHES[1:])


@given(quote_in_passage())
def test_dirtied_quote_is_located_in_dirtied_passage(case: tuple[str, str, str]) -> None:
    """However each side is decorated, the quote is found and the original span normalizes back
    to it."""
    clean, quote, passage = case
    span = locate_quote(quote, passage)
    assert span is not None
    start, end = span
    assert 0 <= start < end <= len(passage)
    for edge in (passage[start], passage[end - 1]):  # the smallest span (decision O)
        assert not edge.isspace() and edge != ZERO_WIDTH
    assert normalize(passage[start:end]) == normalize(clean)


@given(quote_in_passage())
def test_check_text_accepts_a_quote_under_any_presentation(case: tuple[str, str, str]) -> None:
    """A claim whose quote differs from the passage only in presentation is never dropped."""
    _, quote, passage = case
    built = vc.text_case("Management described the business.", quote, passage=passage)
    outcome = check_text(built.claim, built.evidence, built.ctx, VerifyConfig())
    assert outcome.passed and outcome.checks.quote_substring == "pass", outcome.reason


@given(WORDS)
def test_a_word_absent_from_the_passage_is_never_located(words: list[str]) -> None:
    """Adding a token the passage cannot contain makes the quote unlocatable: normalization
    never invents a match."""
    passage = " ".join(words)
    assert locate_quote(f"{passage} 7", passage) is None
