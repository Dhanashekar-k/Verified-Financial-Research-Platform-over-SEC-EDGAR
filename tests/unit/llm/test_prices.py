"""Cost math for ``app/llm/prices.py`` (rules: docs/plan/04_aws_and_cost_plan.md section 5).

Every expected value below is hand-computed from the per-million-token price table:

    haiku-5-5   in 0.10  out 0.50  cache_read 0.01  (cache write = 1.25 x in = 0.125)
    sonnet-5-5  in 2     out 10    cache_read 0.10  (cache write = 2.5)
    opus-5-5    in 4     out 20    cache_read 0.20  (cache write = 5)

Batch is 50% off every component. Haiku 5.5 prompts above 100,000 total input tokens
(input + cache_write + cache_read) are priced at 0.50 in / 2.50 out for the whole request.
Results are exact Decimals: no rounding or quantizing inside ``cost_usd``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.llm.prices import PRICES, ModelPrice, cost_usd

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"


def D(s: str) -> Decimal:
    return Decimal(s)


def _cost(model: str, i: int = 0, cw: int = 0, cr: int = 0, o: int = 0, **kw: bool) -> Decimal:
    return cost_usd(
        model,
        input_tokens=i,
        cache_write_tokens=cw,
        cache_read_tokens=cr,
        output_tokens=o,
        **kw,
    )


# --- the table ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "inp", "out", "cache_read"),
    [
        (HAIKU, "0.10", "0.50", "0.01"),
        (SONNET, "2", "10", "0.10"),
        (OPUS, "4", "20", "0.20"),
    ],
)
def test_price_table_matches_cost_plan(model: str, inp: str, out: str, cache_read: str) -> None:
    """PRICES holds the 04 section 5 numbers as exact Decimals (USD per million tokens)."""
    p = PRICES[model]
    assert isinstance(p, ModelPrice)
    assert (p.input, p.output, p.cache_read) == (D(inp), D(out), D(cache_read))
    assert all(isinstance(x, Decimal) for x in (p.input, p.output, p.cache_read))


def test_registry_models_are_all_priced() -> None:
    """Every model the default registry can route to must be priced, or accounting breaks."""
    from app.llm.client import REGISTRY

    assert set(REGISTRY.values()) <= set(PRICES)


# --- hand-computed cases -----------------------------------------------------------------


def test_zero_tokens_cost_zero_decimal() -> None:
    """No tokens, no cost; and the type is Decimal (not int 0 or float)."""
    result = _cost(HAIKU)
    assert result == D("0")
    assert isinstance(result, Decimal)


def test_haiku_100k_input_tokens() -> None:
    """100K plain input tokens on Haiku (the base tier's limit) = 100,000 * 0.10/1e6 = 0.01.

    1M would cross the >100K long-prompt tier, so the base list price is checked at 100K.
    """
    assert _cost(HAIKU, i=100_000) == D("0.01")


def test_haiku_plan_example_2500_in_250_out() -> None:
    """04 section 5 planner row: 2,500 in / 250 out on Haiku = 0.00025 + 0.000125 = 0.000375."""
    assert _cost(HAIKU, i=2_500, o=250) == D("0.000375")


def test_sonnet_composer_example_7000_in_600_out() -> None:
    """04 section 5 composer row: 7,000 in / 600 out on Sonnet = 0.014 + 0.006 = 0.020."""
    assert _cost(SONNET, i=7_000, o=600) == D("0.020")


def test_cache_hit_case_sonnet_all_four_components() -> None:
    """Sonnet, 1,000 in + 4,000 cache write + 20,000 cache read + 500 out.

    in 1000*2/1e6=0.002; write 4000*2.5/1e6=0.010; read 20000*0.10/1e6=0.002; out
    500*10/1e6=0.005; total 0.019. Pins the 1.25x write multiplier and the cache-read rate.
    """
    assert _cost(SONNET, i=1_000, cw=4_000, cr=20_000, o=500) == D("0.019")


@pytest.mark.parametrize(
    ("model", "expected"),
    [(HAIKU, "0.0125"), (SONNET, "0.25"), (OPUS, "0.5")],
)
def test_cache_write_is_1_25x_input_price(model: str, expected: str) -> None:
    """A 5-minute cache write costs 1.25x the base input price (100K tokens: base Haiku tier)."""
    assert _cost(model, cw=100_000) == D(expected)


@pytest.mark.parametrize(
    ("model", "expected"),
    [(HAIKU, "0.001"), (SONNET, "0.01"), (OPUS, "0.02")],
)
def test_cache_read_uses_cache_read_price(model: str, expected: str) -> None:
    """Cache reads are billed at the cache-hit column, not the input price (100K tokens)."""
    assert _cost(model, cr=100_000) == D(expected)


def test_opus_one_million_in_and_out() -> None:
    """Opus 5.5: 1M in + 1M out = 4 + 20 = 24."""
    assert _cost(OPUS, i=1_000_000, o=1_000_000) == D("24")


# --- batch -------------------------------------------------------------------------------


def test_batch_halves_the_cache_hit_case() -> None:
    """Batch is 50% off: the Sonnet cache-hit case (0.019) becomes 0.0095."""
    assert _cost(SONNET, i=1_000, cw=4_000, cr=20_000, o=500, batch=True) == D("0.0095")


def test_batch_haiku_100k_in_1m_out() -> None:
    """Haiku batch row (0.05 in / 0.25 out): 100K in + 1M out = 0.005 + 0.25 = 0.255."""
    assert _cost(HAIKU, i=100_000, o=1_000_000, batch=True) == D("0.255")


def test_batch_discounts_cache_read_too() -> None:
    """'50% off every component' includes cache reads: Opus 1M read = 0.20 -> 0.10 in batch."""
    assert _cost(OPUS, cr=1_000_000, batch=True) == D("0.10")


def test_batch_defaults_to_false() -> None:
    """Omitting ``batch`` must mean full price."""
    assert _cost(SONNET, i=1_000_000) == D("2")
    assert _cost(SONNET, i=1_000_000, batch=False) == D("2")


# --- Haiku > 100K tier -------------------------------------------------------------------


def test_haiku_exactly_100k_input_is_standard_tier() -> None:
    """Boundary: exactly 100,000 input tokens is NOT above 100K, so 0.10/M applies.

    100,000 * 0.10 / 1e6 = 0.01 and 1,000 out * 0.50 / 1e6 = 0.0005.
    """
    assert _cost(HAIKU, i=100_000, o=1_000) == D("0.0105")


def test_haiku_100k_plus_one_input_is_high_tier() -> None:
    """Boundary: 100,001 input tokens flips the whole request to 0.50 in / 2.50 out.

    100,001 * 0.50 / 1e6 = 0.0500005 and 1,000 out * 2.50 / 1e6 = 0.0025.
    """
    assert _cost(HAIKU, i=100_001, o=1_000) == D("0.0525005")


def test_haiku_tier_uses_sum_of_input_cache_write_and_cache_read() -> None:
    """The tier is chosen by total prompt size (input + cache_write + cache_read), not input alone.

    Isolate the output component (1M out) so the unspecified cache rates in the high tier do not
    matter: at a total of exactly 100,000 output costs 0.50/M; at 100,001 it costs 2.50/M.
    """
    at_100k = _cost(HAIKU, i=40_000, cw=30_000, cr=30_000, o=1_000_000) - _cost(
        HAIKU, i=40_000, cw=30_000, cr=30_000
    )
    over = _cost(HAIKU, i=40_000, cw=30_000, cr=30_001, o=1_000_000) - _cost(
        HAIKU, i=40_000, cw=30_000, cr=30_001
    )
    assert at_100k == D("0.50")
    assert over == D("2.50")


def test_haiku_high_tier_applies_in_batch_with_discount() -> None:
    """Batch and tier compose: high-tier output 2.50/M becomes 1.25/M in batch."""
    base = _cost(HAIKU, i=100_001, batch=True)
    full = _cost(HAIKU, i=100_001, o=1_000_000, batch=True)
    assert full - base == D("1.25")


@pytest.mark.parametrize("model", [SONNET, OPUS])
def test_no_long_prompt_tier_for_sonnet_or_opus(model: str) -> None:
    """Only Haiku 5.5 has the >100K surcharge: 200,000 in-tokens stay at the base rate."""
    price = PRICES[model].input
    assert _cost(model, i=200_000) == price * D("0.2")


# --- error handling ----------------------------------------------------------------------


@pytest.mark.parametrize("model", ["claude-nonexistent-9", "", "HAIKU", "planner"])
def test_unknown_model_raises(model: str) -> None:
    """Unknown models must never be silently priced at zero (that would hide real spend).

    Roles like 'planner' are resolved by the client before pricing, so they are unknown here.
    """
    with pytest.raises((KeyError, ValueError)):
        _cost(model, i=1_000, o=10)


@pytest.mark.parametrize("field", ["i", "cw", "cr", "o"])
def test_negative_token_counts_rejected(field: str) -> None:
    """Negative tokens are a bug upstream; refuse instead of producing negative spend."""
    with pytest.raises(ValueError):
        _cost(SONNET, **{field: -1})  # type: ignore[arg-type]


def test_positional_argument_order_matches_contract() -> None:
    """Signature order is (model, input, cache_write, cache_read, output)."""
    assert cost_usd(SONNET, 1_000, 4_000, 20_000, 500) == D("0.019")


# --- properties --------------------------------------------------------------------------

_tokens = st.integers(min_value=0, max_value=5_000_000)


@given(i=_tokens, cw=_tokens, cr=_tokens, o=_tokens, model=st.sampled_from([SONNET, OPUS]))
def test_property_batch_is_exactly_half(i: int, cw: int, cr: int, o: int, model: str) -> None:
    """For any usage, batch cost is exactly half of the standard cost (no rounding drift)."""
    assert _cost(model, i, cw, cr, o, batch=True) * 2 == _cost(model, i, cw, cr, o)


@given(
    a=st.tuples(_tokens, _tokens, _tokens, _tokens),
    b=st.tuples(_tokens, _tokens, _tokens, _tokens),
    model=st.sampled_from([SONNET, OPUS]),
)
def test_property_cost_is_additive_without_tiers(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int], model: str
) -> None:
    """Without a tier surcharge cost is linear: cost(a) + cost(b) == cost(a + b)."""
    total = tuple(x + y for x, y in zip(a, b, strict=True))
    assert _cost(model, *a) + _cost(model, *b) == _cost(model, *total)  # type: ignore[misc]


@given(i=_tokens, cw=_tokens, cr=_tokens, o=_tokens, model=st.sampled_from([HAIKU, SONNET, OPUS]))
def test_property_cost_is_nonnegative_decimal(i: int, cw: int, cr: int, o: int, model: str) -> None:
    """Cost is always a non-negative Decimal, whichever tier is hit."""
    c = _cost(model, i, cw, cr, o)
    assert isinstance(c, Decimal)
    assert c >= 0
