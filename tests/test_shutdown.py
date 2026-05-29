"""Graceful-shutdown drain window (web.run_app shutdown_timeout)."""

from __future__ import annotations

from anthropic_throttle_proxy import config


def test_shutdown_timeout_default_is_120s():
    # web.run_app(shutdown_timeout=...) is the window aiohttp waits for in-flight
    # streaming turns to finish on SIGTERM before force-closing. Default 120s
    # (> aiohttp's built-in 60s) so a deploy/restart drains most turns instead
    # of SIGKILLing them mid-stream. Tunable via THROTTLE_SHUTDOWN_TIMEOUT_S;
    # must be float seconds for run_app.
    assert config.SHUTDOWN_TIMEOUT_S == 120.0
    assert isinstance(config.SHUTDOWN_TIMEOUT_S, float)
