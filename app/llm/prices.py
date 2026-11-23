"""Claude API prices (USD per million tokens), from docs/plan/04_aws_and_cost_plan.md section 5.

Checked 2026-11-16. Cache writes (5-minute TTL) cost 1.25x the input price; batch is 50% off
every component. Haiku 5.5 prompts above 100,000 input tokens (uncached + cache write + cache
read) are priced at the long-prompt tier for the whole request.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MILLION = Decimal(1_000_000)
CACHE_WRITE_MULTIPLIER = Decimal("1.25")
BATCH_MULTIPLIER = Decimal("0.5")
HAIKU_LONG_PROMPT_THRESHOLD = 100_000


@dataclass(frozen=True)
class ModelPrice:
    input: Decimal
    output: Decimal
    cache_read: Decimal


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(Decimal("0.10"), Decimal("0.50"), Decimal("0.01")),
    "claude-sonnet-5-5": ModelPrice(Decimal("2"), Decimal("10"), Decimal("0.10")),
    "claude-opus-5-5": ModelPrice(Decimal("4"), Decimal("20"), Decimal("0.20")),
}

# Haiku 5.5 above 100K input tokens: $0.50 in / $2.50 out (04 section 5). The cost plan gives
# no long-tier cache-read price, so the base tier's ratio (cache read = 0.1x input) is assumed.
# Every prompt is meant to stay well under 100K, so this tier should never be hit in practice.
HAIKU_LONG_PROMPT = ModelPrice(Decimal("0.50"), Decimal("2.50"), Decimal("0.05"))


def cost_usd(
    model: str,
    input_tokens: int,
    cache_write_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    *,
    batch: bool = False,
) -> Decimal:
    """Exact cost of one request (no rounding). Unknown model -> KeyError."""
    counts = (input_tokens, cache_write_tokens, cache_read_tokens, output_tokens)
    if any(n < 0 for n in counts):
        raise ValueError(f"token counts must be non-negative, got {counts}")
    price = PRICES[model]
    total_in = input_tokens + cache_write_tokens + cache_read_tokens
    if model == "claude-haiku-5-5" and total_in > HAIKU_LONG_PROMPT_THRESHOLD:
        price = HAIKU_LONG_PROMPT
    cost = (
        input_tokens * price.input
        + cache_write_tokens * price.input * CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * price.cache_read
        + output_tokens * price.output
    ) / MILLION
    return cost * BATCH_MULTIPLIER if batch else cost
