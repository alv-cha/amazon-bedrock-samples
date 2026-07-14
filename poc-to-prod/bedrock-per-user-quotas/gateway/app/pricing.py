"""Model price table and cost estimation.

Prices are expressed in USD per **1 million tokens** and are deliberately
kept in one editable place. The values below are PLACEHOLDERS — check the
current Amazon Bedrock pricing page for the models you enable and update
this table (or set the MODEL_PRICES_JSON environment variable, which takes
precedence and lets you change prices without redeploying code).

Internally the gateway accounts cost in **micro-USD** (1e-6 USD) as
integers so DynamoDB atomic counters stay exact.
"""

import json
import os
from dataclasses import dataclass

MICRO = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float   # USD per 1M input tokens
    output_per_mtok: float  # USD per 1M output tokens


# --- PLACEHOLDER prices: verify against the Bedrock pricing page. ---
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "openai.gpt-oss-120b": ModelPrice(input_per_mtok=0.15, output_per_mtok=0.60),
    "openai.gpt-oss-20b": ModelPrice(input_per_mtok=0.07, output_per_mtok=0.30),
    "anthropic.claude-opus-4-7": ModelPrice(input_per_mtok=15.00, output_per_mtok=75.00),
}

# Unknown models are billed at the most expensive known rate so that a
# missing table entry can never be used to bypass a budget.
FALLBACK_PRICE = ModelPrice(input_per_mtok=15.00, output_per_mtok=75.00)


def load_prices() -> dict[str, ModelPrice]:
    raw = os.environ.get("MODEL_PRICES_JSON")
    if not raw:
        return dict(DEFAULT_PRICES)
    parsed = json.loads(raw)
    prices = dict(DEFAULT_PRICES)
    for model_id, p in parsed.items():
        prices[model_id] = ModelPrice(
            input_per_mtok=float(p["input_per_mtok"]),
            output_per_mtok=float(p["output_per_mtok"]),
        )
    return prices


_PRICES: dict[str, ModelPrice] | None = None


def get_price(model_id: str) -> ModelPrice:
    global _PRICES
    if _PRICES is None:
        _PRICES = load_prices()
    return _PRICES.get(model_id, FALLBACK_PRICE)


# Anthropic prompt-caching price multipliers relative to the input price
# (cache writes cost more than plain input, cache reads much less).
# Override via env if the models you use are priced differently.
CACHE_WRITE_MULTIPLIER = float(os.environ.get("CACHE_WRITE_MULTIPLIER", "1.25"))
CACHE_READ_MULTIPLIER = float(os.environ.get("CACHE_READ_MULTIPLIER", "0.1"))


def cost_micro_usd(model_id: str, input_tokens: int, output_tokens: int,
                   cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> int:
    """Cost of a request in integer micro-USD (rounded up, never negative)."""
    price = get_price(model_id)
    effective_input = (
        max(input_tokens, 0)
        + CACHE_WRITE_MULTIPLIER * max(cache_write_tokens, 0)
        + CACHE_READ_MULTIPLIER * max(cache_read_tokens, 0)
    )
    usd = (
        effective_input * price.input_per_mtok
        + max(output_tokens, 0) * price.output_per_mtok
    ) / 1_000_000
    # Round up so accounting always errs on the safe side.
    micro = int(usd * MICRO)
    if usd * MICRO > micro:
        micro += 1
    return micro


def reset_price_cache() -> None:
    """Test hook."""
    global _PRICES
    _PRICES = None
