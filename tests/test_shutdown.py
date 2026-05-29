"""Graceful-shutdown drain window (web.run_app shutdown_timeout)."""

from __future__ import annotations

from anthropic_throttle_proxy import config, proxy


def test_shutdown_timeout_default_is_85s():
    # Bare default is 85s — deliberately under systemd's 90s DefaultTimeoutStopSec
    # so aiohttp's graceful drain+close finishes before systemd would SIGKILL,
    # i.e. honored even under a stock unit. The NixOS module couples a higher
    # value with a matching TimeoutStopSec. Must be float seconds for run_app.
    assert config.SHUTDOWN_TIMEOUT_S == 85.0
    assert isinstance(config.SHUTDOWN_TIMEOUT_S, float)


def test_main_passes_shutdown_timeout_to_run_app(monkeypatch):
    # Proves main() actually wires config.SHUTDOWN_TIMEOUT_S into web.run_app
    # (the executable line, not just the constant). Mock run_app so no server
    # starts; no-op set_event_loop so main()'s fresh loop doesn't disturb the
    # test runner; stub load_overrides + CENTRAL_URL so main() reaches run_app
    # deterministically without touching disk or spawning the central task.
    captured: dict = {}
    monkeypatch.setattr(proxy.web, "run_app", lambda app, **kw: captured.update(kw))
    monkeypatch.setattr(proxy.asyncio, "set_event_loop", lambda _loop: None)
    monkeypatch.setattr(config, "load_overrides", lambda: None)
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(config, "SHUTDOWN_TIMEOUT_S", 123.0)

    proxy.main()

    assert captured.get("shutdown_timeout") == 123.0
