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
