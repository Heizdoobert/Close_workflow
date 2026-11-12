"""USD price per model for API-mode calls (stdlib only).

Each price is (input, cache_read, cache_create, output) in USD per million tokens.
PRICES holds no real provider prices on purpose: an invented price is wrong accounting.
Set the real price for a model with FLOW_PRICE_<MODEL>, e.g.
FLOW_PRICE_ANTHROPIC_CLAUDE_HAIKU_5_5="1.0,0.1,1.25,5.0".
"""

import os
import re

# test fixture only, not a real provider price
PRICES = {
    "example/test-model": (1.0, 0.1, 1.25, 5.0),
}


def _env_name(model):
    return "FLOW_PRICE_" + re.sub(r"[^A-Z0-9]", "_", model.upper())


def price_for(model):
    raw = os.environ.get(_env_name(model))
    if raw is not None:
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 4:
            raise ValueError(f"{_env_name(model)} needs 4 comma-separated numbers (input,cache_read,cache_create,output)")
        return tuple(float(p) for p in parts)
    return PRICES.get(model)


def cost_of(model, usage):
    price = price_for(model)
    if price is None:
        return None
    counts = (
        usage.get("input_tokens", 0),
        usage.get("cache_read", 0),
        usage.get("cache_create", 0),
        usage.get("output_tokens", 0),
    )
    return sum(n * p for n, p in zip(counts, price)) / 1_000_000
