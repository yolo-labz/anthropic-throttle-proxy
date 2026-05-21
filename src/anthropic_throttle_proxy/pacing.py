"""Process-global burst pacer.

A single rate limiter across all bearers: never send two upstream POSTs closer
than ``MIN_DISPATCH_GAP_S`` apart. Does NOT cap concurrency (the per-bearer
``FairBearerLimiter`` does that) — it only smooths the millisecond-scale
dogpile. The lock is acquired briefly; the wait itself is an async sleep.
"""

from __future__ import annotations

import asyncio
import time

from . import config

# Burst pacing — process-global. Lock is late-bound in main() via set_lock()
# because it must be created on the running event loop.
_dispatch_lock: asyncio.Lock | None = None
_last_dispatch_ts: float = 0.0


def set_lock(lock: asyncio.Lock) -> None:
    """Bind the dispatch lock (called once from ``main()`` on the running loop)."""
    global _dispatch_lock
    _dispatch_lock = lock


async def _pace_dispatch() -> None:
    """Block until ``MIN_DISPATCH_GAP_S`` has elapsed since the last dispatch.

    No-op when ``MIN_DISPATCH_GAP_S <= 0``.
    """
    global _last_dispatch_ts
    if config.MIN_DISPATCH_GAP_S <= 0:
        return
    async with _dispatch_lock:
        now = time.monotonic()
        wait = config.MIN_DISPATCH_GAP_S - (now - _last_dispatch_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_dispatch_ts = time.monotonic()
