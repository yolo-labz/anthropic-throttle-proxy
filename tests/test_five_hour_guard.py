"""Tests for the opt-in 5-hour guard (``THROTTLE_5H_SOURCE``).

Anthropic stopped sending ``anthropic-ratelimit-unified-5h-*`` (last observed
2026-10-31T20:45Z), so the header path in ``proxy._apply_unified`` no longer sees
that window. These tests pin the fallback contract:

* ``off`` — nothing happens (byte-identical to the pre-knob behaviour);
* ``observe`` — one debounced warning per 5h reset past ``THROTTLE_UTILIZATION_WARN``;
* ``enforce`` — the bearer is held until the 5h reset;
* absent ``five_hour`` data — always falls through, never a hard block.

Fixture/helper setup is reused from ``test_accounts`` rather than duplicated
(the credential file and the fake ``/api/oauth/usage`` transport are identical).
The gateway's ``log()`` writes to stderr, so assertions read ``capsys``.
"""

from __future__ import annotations

import pytest
from test_accounts import NOW, _expected_bid, _stub_endpoint, _usage_body, _write_cred

from anthropic_throttle_proxy import accounts, config, proxy

DAY = 86_400


class _Lim:
    """Minimal limiter double: records the Retry-After we would have set."""

    def __init__(self) -> None:
        self.notes: list[float] = []
        # accounts reads this when deciding whether a stale window may be cleared.
        self._retry_after_until = 0.0

    def note_retry_after(self, seconds: float) -> None:
        self.notes.append(seconds)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENDPOINT_CACHE_FILE", tmp_path / "endpoint-cache.json")
    monkeypatch.setattr(accounts, "_endpoint_cache", {})
    monkeypatch.setattr(accounts, "_endpoint_backoff", {})
    monkeypatch.setattr(accounts, "_endpoint_failures", {})
    monkeypatch.setattr(accounts, "_endpoint_locks", {})
    monkeypatch.setattr(accounts, "_endpoint_cache_loaded", False)
    monkeypatch.setattr(config, "bearer_limiters", {})
    monkeypatch.setattr(config, "bearer_state", {})
    yield


def _setup(tmp_path, monkeypatch, token: str, body: dict, mode: str) -> _Lim:
    cred = tmp_path / "a.json"
    _write_cred(cred, token)
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    monkeypatch.setattr(config, "FIVE_HOUR_SOURCE", mode)
    _stub_endpoint(monkeypatch, {token: {"usage": (200, body)}})
    lim = _Lim()
    config.bearer_limiters[_expected_bid(token)] = lim
    return lim


def _tok(name: str) -> str:
    """Opaque fake credential (keeps a bare literal out of an S105-shaped name)."""
    return f"tok-{name}"


def _warn_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if "unified-warning" in line]


async def test_off_is_a_noop(tmp_path, monkeypatch, capsys):
    token = _tok("off")
    lim = _setup(
        tmp_path, monkeypatch, token, _usage_body(100.0, 80.0, NOW + 1800, NOW + DAY), "off"
    )
    await accounts.refresh_endpoint(NOW)
    err = capsys.readouterr().err
    assert lim.notes == []  # no deferral
    assert _warn_lines(err) == []  # and no warning either
    assert config.bearer_state == {}  # shared state untouched


async def test_observe_warns_once_per_reset(tmp_path, monkeypatch, capsys):
    token = _tok("observe")
    lim = _setup(
        tmp_path, monkeypatch, token, _usage_body(95.0, 80.0, NOW + 1800, NOW + DAY), "observe"
    )
    await accounts.refresh_endpoint(NOW)
    await accounts.refresh_endpoint(NOW + accounts.ENDPOINT_TTL_S + 1)
    lines = _warn_lines(capsys.readouterr().err)
    assert len(lines) == 1, lines  # debounced per (window, reset)
    assert "window=5h" in lines[0] and "util=0.95" in lines[0]
    assert lim.notes == []  # observe never defers


async def test_observe_rearms_on_a_new_reset(tmp_path, monkeypatch, capsys):
    token = _tok("rearm")
    _setup(tmp_path, monkeypatch, token, _usage_body(95.0, 80.0, NOW + 1800, NOW + DAY), "observe")
    await accounts.refresh_endpoint(NOW)
    # next window: different reset epoch => a fresh warning is legitimate
    _stub_endpoint(
        monkeypatch,
        {token: {"usage": (200, _usage_body(95.0, 80.0, NOW + 5 * 3600, NOW + DAY))}},
    )
    await accounts.refresh_endpoint(NOW + accounts.ENDPOINT_TTL_S + 1)
    assert len(_warn_lines(capsys.readouterr().err)) == 2


async def test_enforce_defers_until_the_5h_reset(tmp_path, monkeypatch, capsys):
    token = _tok("enforce")
    lim = _setup(
        tmp_path, monkeypatch, token, _usage_body(100.0, 80.0, NOW + 1800, NOW + DAY), "enforce"
    )
    await accounts.refresh_endpoint(NOW)
    err = capsys.readouterr().err
    assert lim.notes == [1800.0]  # honest Retry-After = time to the 5h reset
    assert "five-hour-enforce" in err


async def test_enforce_does_not_defer_after_the_reset(tmp_path, monkeypatch):
    token = _tok("reset-past")
    lim = _setup(
        tmp_path, monkeypatch, token, _usage_body(100.0, 80.0, NOW - 60, NOW + DAY), "enforce"
    )
    await accounts.refresh_endpoint(NOW)
    assert lim.notes == []  # window already rolled: the hold clears


async def test_enforce_clamps_an_absurd_reset(tmp_path, monkeypatch):
    token = _tok("absurd")
    lim = _setup(
        tmp_path,
        monkeypatch,
        token,
        _usage_body(100.0, 80.0, NOW + 30 * 3600, NOW + DAY),
        "enforce",
    )
    await accounts.refresh_endpoint(NOW)
    assert lim.notes == [proxy._FIVE_HOUR_MAX_PAUSE_S]


async def test_absent_five_hour_never_defers(tmp_path, monkeypatch, capsys):
    token = _tok("absent")
    lim = _setup(
        tmp_path,
        monkeypatch,
        token,
        _usage_body(40.0, 95.0, NOW, NOW + DAY, five_hour=None),
        "enforce",
    )
    await accounts.refresh_endpoint(NOW)
    assert lim.notes == []  # UNKNOWN is not a wall
    assert _warn_lines(capsys.readouterr().err) == []


async def test_enforce_below_the_cap_only_warns(tmp_path, monkeypatch, capsys):
    token = _tok("below")
    lim = _setup(
        tmp_path, monkeypatch, token, _usage_body(92.0, 80.0, NOW + 1800, NOW + DAY), "enforce"
    )
    await accounts.refresh_endpoint(NOW)
    assert lim.notes == []  # 0.92 warns, it does not hold
    assert len(_warn_lines(capsys.readouterr().err)) == 1
