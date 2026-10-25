"""Provider pricing tables + model→rate lookup.

Pure data module: no I/O, no imports beyond typing. Rates are USD per million
tokens and are used to estimate per-request cost from the SSE/JSON ``usage``
block. Consumers MUST treat an unknown model as **unknown cost** — this module
returns ``None`` for it rather than guessing (see ``_pricing_for``).

Sources:

* Anthropic rows — claude.com /pricing (2026-05).
* MiMo rows — official Xiaomi MiMo Open Platform USD card, delivered and
  metering-calibrated by the MiMo lane seat on 09/10/2026 (vault: "Active
  Routing S2 — MiMo PAYG key labels, rate card and metering calibration
  (2026-10-09)"): pro 0.435 / 0.0036 / 0.87; flash 0.14 / 0.0028 / 0.28;
  pro-ultraspeed 4.35 / 0.036 / 8.70 (input / cache-read / output). TTS and
  cache-write are free, so ``cache_creation`` is 0.00 for MiMo rows.
"""

from __future__ import annotations

# input | output | cache_read | cache_creation
PRICING: dict[str, dict[str, float]] = {
    # --- Anthropic -----------------------------------------------------------
    "claude-opus-4-7": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_creation": 18.75},
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_creation": 18.75},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_creation": 3.75},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_creation": 3.75},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_creation": 1.25},
    # Current generation, from pi's pinned catalog (packages/ai/src/providers/data/
    # anthropic.json, 23/10/2026) - the same rates pi's usage.jsonl costs use, so the
    # gateway's per-project spend and the fleet analysis share one unit. Prefix
    # matching is insertion order: each longer id MUST precede its prefix
    # ("claude-opus-5-5" before "claude-opus-5").
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_creation": 5.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_creation": 6.25},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_creation": 6.25},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_creation": 6.25},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_creation": 2.5},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_creation": 2.5},
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_creation": 12.5},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_creation": 12.5},
    "claude-haiku-5-5": {
        "input": 0.10,
        "output": 0.50,
        "cache_read": 0.01,
        "cache_creation": 0.125,
    },
    # --- Xiaomi MiMo Open Platform (USD/M, 09/10/2026 official card) ---------
    # Prefix matching is insertion order, so ultraspeed MUST precede pro.
    "mimo-v2.6-pro-ultraspeed": {
        "input": 4.35,
        "output": 8.70,
        "cache_read": 0.036,
        "cache_creation": 0.00,
    },
    "mimo-v2.6-pro": {"input": 0.435, "output": 0.87, "cache_read": 0.0036, "cache_creation": 0.00},
    "mimo-v2.6-flash": {
        "input": 0.14,
        "output": 0.28,
        "cache_read": 0.0028,
        "cache_creation": 0.00,
    },
}


# Request-wide tiers (pi's ModelCost.tiers): when a request's total input
# (input + cache_read + cache_creation) EXCEEDS the threshold, the tier's rates
# apply to the whole request. Keyed like PRICING; checked after the row matched.
PRICING_TIERS: dict[str, tuple[tuple[int, dict[str, float]], ...]] = {
    "claude-haiku-5-5": (
        (100_000, {"input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_creation": 0.625}),
    ),
}


def rates_for_usage(model: str | None, usage: dict[str, int]) -> dict[str, float] | None:
    """Rates for one request: the matched row, or its highest exceeded tier.

    Mirrors pi's ``calculateCost``: the tier is chosen by the request's total
    input and applies to every token kind of that request.
    """
    rates = _pricing_for(model)
    if rates is None or not model:
        return rates
    candidate = model.strip().lower().rsplit("/", 1)[-1]
    key = next((k for k in PRICING if candidate.startswith(k)), None)
    total_input = (
        usage.get("input", 0) + usage.get("cache_read", 0) + usage.get("cache_creation", 0)
    )
    for threshold, tier in PRICING_TIERS.get(key or "", ()):
        if total_input > threshold:
            rates = tier
    return rates


def _pricing_for(model: str | None) -> dict[str, float] | None:
    """Match a request model string to the pricing table, or ``None``.

    ``None`` means UNKNOWN COST and consumers must record no USD for the
    request (tokens still count). There is deliberately no default row: the
    old Opus fallback priced every unknown model at $15/$75 per M, which made
    a MiMo flash lane report **107.1x** its real input cost and **267.9x** its
    real output cost (measured 09/10/2026 on :8774; vault S2 receipt). A made-up
    number is worse than an honest gap — unknown providers (Z.AI, DeepSeek, …)
    get their rows from their own delivered cards, not from Anthropic's.
    """
    if not model:
        return None
    # Tolerate provider-prefixed ids ("xiaomi/mimo-v2.6-flash") and case drift.
    candidate = model.strip().lower().rsplit("/", 1)[-1]
    for key, rates in PRICING.items():
        if candidate.startswith(key):
            return rates
    return None
