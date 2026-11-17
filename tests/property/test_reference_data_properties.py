"""Property tests (Hypothesis) for the reference-data parsers.

Covers ``parse_company_tickers`` (``pipelines/fetch/reference_data.py``) and ``normalize_ticker``
(``pipelines/spark/build_companies.py``). The example-based tests live in
``tests/unit/test_reference_data.py`` and ``tests/integration/test_build_companies.py``.
"""

from __future__ import annotations

import string
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from pipelines.fetch.reference_data import parse_company_tickers
from pipelines.spark.build_companies import normalize_ticker

# Valid entries only: a blank ticker or title is rejected by the parser (a unit test covers that).
_TEXT = st.text(
    alphabet=string.ascii_letters + string.digits + " .-,", min_size=1, max_size=12
).filter(lambda t: t.strip() != "")


@st.composite
def ticker_documents(draw: Any) -> tuple[dict[str, Any], dict[int, tuple[int, str, str]]]:
    """A SEC-shaped document (shuffled key order, cik_str as int or zero-padded str) plus the
    expected ``rank -> (cik, ticker, title)`` mapping."""
    ranks = draw(st.lists(st.integers(0, 20_000), min_size=1, max_size=30, unique=True))
    expected: dict[int, tuple[int, str, str]] = {}
    entries: dict[int, dict[str, Any]] = {}
    for rank in ranks:
        cik = draw(st.integers(1, 9_999_999_999))
        ticker, title = draw(_TEXT), draw(_TEXT)
        as_text = draw(st.booleans())
        entries[rank] = {
            "cik_str": f"{cik:010d}" if as_text else cik,
            "ticker": ticker,
            "title": title,
        }
        expected[rank] = (cik, ticker, title)
    order = draw(st.permutations(ranks))
    return {str(rank): entries[rank] for rank in order}, expected


@given(doc=ticker_documents())
def test_parse_is_sorted_by_rank_and_lossless_property(
    doc: tuple[dict[str, Any], dict[int, tuple[int, str, str]]],
):
    """For any key order: one row per entry, ascending integer rank, fields mapped faithfully."""
    data, expected = doc

    rows = parse_company_tickers(data)

    assert [r.rank for r in rows] == sorted(expected)
    for row in rows:
        assert (row.cik, row.ticker, row.name) == expected[row.rank]


_ASCII = st.text(alphabet=string.printable, max_size=12)


@given(t=_ASCII)
def test_normalize_ticker_is_idempotent_property(t: str):
    """Normalizing twice equals normalizing once (it is applied to both sides of a join)."""
    assert normalize_ticker(normalize_ticker(t)) == normalize_ticker(t)


@given(t=_ASCII)
def test_normalize_ticker_output_shape_property(t: str):
    """Output is stripped, upper-case and dot-free, whatever the input."""
    out = normalize_ticker(t)

    assert out == out.strip()
    assert out == out.upper()
    assert "." not in out


@given(t=_ASCII)
def test_normalize_ticker_is_blind_to_case_edge_whitespace_and_dot_vs_dash_property(t: str):
    """Spellings that differ only by case, surrounding spaces or '.' vs '-' normalize equal, so
    a ticker map and an S&P list that disagree on those still match."""
    base = t.strip()
    assert normalize_ticker(f"  {base.lower()} ") == normalize_ticker(base.replace("-", "."))
