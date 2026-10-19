"""Shared free-lane test scaffolding (spec 333/334, 19/10/2026).

The AIMD/queue-admission suites each need the same isolated registry, lock
freshener, and knob baseline; keeping a copy per file is what tripped the
jscpd clone gate on PR #342. One definition, imported everywhere.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from anthropic_throttle_proxy import config, limiter, pacing

FREE_HARD_CEILING = 1024
FREE_INITIAL = 12
FREE_FLOOR = 2


@asynccontextmanager
async def isolate_free_lane(
    monkeypatch,
    *,
    queue_mode: str = "off",
    hold_s: float | None = None,
) -> AsyncIterator[None]:
    """Clean bearer registry + free-lane AIMD knobs on this test's own loop."""
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    monkeypatch.setattr(config, "MAX_CONCURRENT", FREE_HARD_CEILING, raising=True)
    monkeypatch.setattr(config, "QUEUE_MODE", queue_mode, raising=True)
    monkeypatch.setattr(config, "AIMD_MIN", FREE_FLOOR, raising=True)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", FREE_INITIAL, raising=True)
    monkeypatch.setattr(config, "AIMD_DECREASE", 0.7, raising=True)
    monkeypatch.setattr(config, "AIMD_BACKOFF_S", 30.0, raising=True)
    monkeypatch.setattr(config, "AIMD_RAMP_AFTER", 10, raising=True)
    monkeypatch.setattr(config, "AIMD_RAMP_AFTER_FAST", 5, raising=True)
    monkeypatch.setattr(config, "AIMD_STORM_THRESHOLD", 3, raising=True)
    if hold_s is not None:
        monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", hold_s, raising=True)
    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    try:
        yield
    finally:
        config.bearer_limiters.clear()
        config.bearer_state.clear()


def patch_admission(monkeypatch, **settings) -> None:
    """Apply an admission-settings dict and disable account routing."""
    for key, value in settings.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setenv("THROTTLE_ACCOUNT_ROUTING", "off")
