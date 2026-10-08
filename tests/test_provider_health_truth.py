"""Invalid configuration cannot retain the previous successful DNS verdict."""

import asyncio

import pytest

from anthropic_throttle_proxy import config, proxy


@pytest.mark.parametrize(
    "upstream", ["", "not-a-url", "ftp://example.invalid", "http://[", "http://host:bad"]
)
async def test_invalid_upstream_replaces_healthy_egress_without_dns(monkeypatch, upstream):
    async def forbidden_dns(*args, **kwargs):
        pytest.fail("invalid configuration must not reach DNS or a provider")

    monkeypatch.setattr(config, "UPSTREAM", upstream)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", forbidden_dns)
    monkeypatch.setitem(config.state, "upstream_egress_ok", True)
    monkeypatch.setitem(config.state, "upstream_egress_error", "")
    monkeypatch.setitem(config.state, "upstream_egress_last_check", 0)
    await proxy._refresh_upstream_egress()
    assert config.state["upstream_egress_ok"] is False
    assert config.state["upstream_egress_error"] == "invalid upstream URL"


async def test_valid_loopback_sink_remains_dns_only(monkeypatch):
    calls = []

    async def dns(host, port, **kwargs):
        calls.append((host, port))
        return []

    monkeypatch.setattr(config, "UPSTREAM", "http://127.0.0.1:1")
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    assert await proxy._check_upstream_egress() == (True, "")
    assert calls == [("127.0.0.1", 1)]
