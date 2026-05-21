"""Anthropic API pricing table + model→rate lookup.

Pure data module: no I/O, no imports beyond typing. Rates are USD per million
tokens from claude.com /pricing (2026-05), used to estimate per-request cost
from the SSE ``usage`` block.
"""

from __future__ import annotations

# input | output | cache_read | cache_creation
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_creation": 18.75},
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_creation": 18.75},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_creation": 3.75},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_creation": 3.75},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_creation": 1.25},
}
PRICING_DEFAULT: dict[str, float] = {
    "input": 15.0,
    "output": 75.0,
    "cache_read": 1.50,
    "cache_creation": 18.75,
}


def _pricing_for(model: str | None) -> dict[str, float]:
    """Match a request model string to the pricing table, falling back to Opus rates."""
    if not model:
        return PRICING_DEFAULT
    for key, rates in PRICING.items():
        if model.startswith(key):
            return rates
    return PRICING_DEFAULT
