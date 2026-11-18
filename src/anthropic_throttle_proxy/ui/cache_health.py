"""Prompt-cache health panel (spec 381): live, in-process, no new instrumentation.

Reads two things that already exist:

* ``metrics.M_TOKENS`` (``anthropic_tokens_total{model,kind}``), fed by
  ``ratelimit._parse_sse_usage`` for both the Anthropic and the
  OpenAI-compatible vocabularies. ``input`` is the UNCACHED remainder there,
  so OpenTelemetry's ``gen_ai.usage.input_tokens`` is
  ``input + cache_read + cache_creation`` and the cache kinds are subsets of it.
* ``session_affinity.stats()`` — the same dict ``/__throttle/health`` embeds.

Scope is this process since it started — the same anchor the account table's
"observed tokens · share" column uses (``account_pool.BALANCER.started_at``).
The counter only increments on a positive count, so a kind with no sample is
UNKNOWN for that model, never zero. USD (``M_COST``) is deliberately absent: it
is a list-price estimate, not a debit. A cache write is attributed overhead
(every account / provider / model switch is a guaranteed cold miss), never
labelled waste.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp_jinja2
from aiohttp import web

from .. import account_pool as _account_pool
from .. import session_affinity as _session_affinity
from ..metrics import M_TOKENS

KINDS = ("input", "cache_read", "cache_creation", "output")
_BRT = timezone(timedelta(hours=-3))  # Brazil has had no DST since 2019.
_TPL = "partials/cache_health.html"


def token_samples() -> list[tuple[str, str, float]]:
    """``(model, kind, value)`` for every live ``anthropic_tokens_total`` series."""
    out = []
    for metric in M_TOKENS.collect():
        for s in metric.samples:
            if s.name.endswith("_total"):
                out.append((s.labels.get("model", ""), s.labels.get("kind", ""), s.value))
    return out


def _pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1)


def model_row(model: str, counts: dict[str, int]) -> dict[str, Any]:
    """One model. ``counts`` holds only the kinds that were observed."""
    fresh = counts.get("input")
    read = counts.get("cache_read")
    write = counts.get("cache_creation")
    missing = [k for k in KINDS if k not in counts]
    known_input = sum(v for v in (fresh, read, write) if v is not None)
    # OTel input total; only a lower bound while an input-side kind is missing.
    exact = None not in (fresh, read, write)
    row: dict[str, Any] = {
        "model": model,
        "counts": {k: counts.get(k) for k in KINDS},
        "missing": missing,
        "input_total": known_input if known_input else None,
        "input_total_exact": exact,
        "read_share": None,
        "read_share_bound": "",
        "write_share": None,
        "bar": [],
    }
    if fresh is None or read is None or not known_input:
        return row  # cannot place cache reads against a denominator
    if write is None:
        # cache_creation never seen: the true share can only be lower.
        row["read_share"], row["read_share_bound"] = _pct(read, known_input), "≤ "
        return row
    row["read_share"] = _pct(read, known_input)
    row["write_share"] = _pct(write, known_input)
    x = 0.0
    for key, value in (("cache_read", read), ("cache_write", write), ("fresh_input", fresh)):
        if value:
            w = 100.0 * value / known_input
            row["bar"].append({"key": key, "x": round(x, 3), "w": round(w, 3), "n": value})
            x += w
    return row


def view(samples: Iterable[tuple[str, str, float]], started_at: float) -> dict[str, Any]:
    per_model: dict[str, dict[str, int]] = {}
    for model, kind, value in samples:
        if kind in KINDS:
            per_model.setdefault(model or "unknown", {})[kind] = int(value)
    rows = [
        model_row(m, c)
        for m, c in sorted(per_model.items(), key=lambda mc: -sum(mc[1].values()))
        if any(c.values())
    ]
    return {
        "since": datetime.fromtimestamp(started_at, _BRT).strftime("%d/%m/%Y %H:%M"),
        "rows": rows,
        "affinity_enabled": _session_affinity.enabled(),
        "affinity": _session_affinity.stats(),
    }


async def cache_health_partial(request: web.Request) -> web.Response:
    """GET /ui/cache — the polled panel body."""
    ctx = view(token_samples(), _account_pool.BALANCER.started_at)
    return aiohttp_jinja2.render_template(_TPL, request, {"cache": ctx})


def attach_cache_health(app: web.Application) -> None:
    """Mount the route; call after ``attach_ui`` (needs its jinja env)."""
    from .auth import guard  # the same opt-in remote gate as every other /ui route

    app.router.add_get("/ui/cache", guard(cache_health_partial))
