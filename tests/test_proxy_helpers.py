"""Coverage tests for small proxy.py helpers without full request lifecycle.

Targets coverage gaps left by the broader integration suites:

* ``_log_413_reason`` — Anthropic 413 envelope decoding (PR #20 empty-body branch).
* ``_check_upstream_egress`` — DNS resolve probe used by ``/__throttle/health``.
* ``_maybe_glide`` — opt-in proactive shrink at ``UTILIZATION_TARGET``.
* ``root_probe`` — local GET/HEAD ``/`` probe (PR #29).
* ``_systemd_listen_sockets`` — socket-activation FD inheritance.
* ``main()`` — invalid ``THROTTLE_QUEUE_MODE`` warning at boot.

These are direct-call unit tests (no aiohttp test client) so they run fast and
isolate one branch per case. Pacing/forwarding/integration paths live in
``test_proxy_app.py`` and ``test_forwarding_paths.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import socket
import time
from typing import Any, cast

import pytest
from aiohttp.test_utils import make_mocked_request

from anthropic_throttle_proxy import accounts, config, limiter, proxy
from anthropic_throttle_proxy.limiter import FairBearerLimiter

# ---------------------------------------------------------------------------
# _log_413_reason — PR #17/#20 — translate Anthropic's real 413 cause.
# ---------------------------------------------------------------------------


def test_log_413_reason_empty_bytearray_logs_empty_body(monkeypatch) -> None:
    """``bytearray()`` must hit the explicit empty-body branch (PR #20).

    The central tier sometimes forwards a Content-Length:0 413 envelope; the
    operator at least gets ``reason=empty_body`` instead of a silent JSON parse
    error.
    """
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    proxy._log_413_reason("abcd1234", "claude-opus-4-8", bytearray())
    assert len(lines) == 1
    assert "upstream_413" in lines[0]
    assert "bid=abcd1234" in lines[0]
    assert "model=claude-opus-4-8" in lines[0]
    assert "reason=empty_body" in lines[0]


def test_log_413_reason_none_captured_logs_empty_body(monkeypatch) -> None:
    """``captured=None`` must take the same empty-body branch (``not None`` truthy)."""
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    proxy._log_413_reason("abcd1234", "claude-opus-4-8", None)
    assert len(lines) == 1
    assert "reason=empty_body" in lines[0]


def test_log_413_reason_error_envelope_dict(monkeypatch) -> None:
    """The standard Anthropic shape ``{"error": {"type": ..., "message": ...}}``."""
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    body = bytearray(b'{"error":{"type":"request_too_large","message":"prompt is too long"}}')
    proxy._log_413_reason("abcd1234", "claude-opus-4-8", body)
    assert len(lines) == 1
    assert "type='request_too_large'" in lines[0]
    assert "message='prompt is too long'" in lines[0]


def test_log_413_reason_falls_back_to_top_level(monkeypatch) -> None:
    """When ``error`` key is absent, fall back to top-level ``type``/``message``."""
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    body = bytearray(b'{"type":"flat_top","message":"top-level reason"}')
    proxy._log_413_reason("abcd1234", "claude-opus-4-8", body)
    assert "type='flat_top'" in lines[0]
    assert "message='top-level reason'" in lines[0]


def test_log_413_reason_error_field_not_dict_uses_no_message(monkeypatch) -> None:
    """``"error"`` as a string degrades to ``<no type>``/``<no message>``.

    The ``isinstance(err.get("error"), dict)`` guard at line 906 short-circuits
    the unwrap, and the top-level lacks ``type``/``message``, so the placeholder
    fallbacks fire — proves the guard is doing its job rather than throwing
    ``AttributeError`` on ``"flat string".get(...)``.
    """
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    body = bytearray(b'{"error":"flat string envelope"}')
    proxy._log_413_reason("abcd1234", "claude-opus-4-8", body)
    assert "type='<no type>'" in lines[0]
    assert "message='<no message>'" in lines[0]


def test_log_413_reason_empty_envelope_uses_no_message(monkeypatch) -> None:
    """``{}`` JSON body — both unwrap and fallback miss → placeholder strings."""
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    body = bytearray(b"{}")
    proxy._log_413_reason("abcd1234", "claude-opus-4-8", body)
    assert "type='<no type>'" in lines[0]
    assert "message='<no message>'" in lines[0]


def test_log_413_reason_non_json_logs_parse_error(monkeypatch) -> None:
    """Non-JSON body trips ``json.JSONDecodeError`` → ``parse_error=`` log path."""
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    body = bytearray(b"not json at all")
    proxy._log_413_reason("abcd1234", "claude-opus-4-8", body)
    assert len(lines) == 1
    assert "parse_error=" in lines[0]
    assert "JSONDecodeError" in lines[0]
    # 200-byte preview gets emitted so the operator can eyeball the body shape.
    assert "preview=" in lines[0]


# ---------------------------------------------------------------------------
# _check_upstream_egress — DNS probe for /__throttle/health.
# ---------------------------------------------------------------------------


async def test_check_upstream_egress_no_host_short_circuits(monkeypatch) -> None:
    """``http:///`` (no host) must NOT call getaddrinfo — empty error, ok=True."""
    monkeypatch.setattr(config, "UPSTREAM", "http:///some-path")
    # Mark the loop's getaddrinfo as a tripwire — must NOT be reached.
    loop = asyncio.get_running_loop()

    async def tripwire(*_args, **_kwargs):
        raise AssertionError("getaddrinfo must not be called when host is empty")

    monkeypatch.setattr(loop, "getaddrinfo", tripwire)

    ok, err = await proxy._check_upstream_egress()
    assert ok is True
    assert err == ""


async def test_check_upstream_egress_returns_false_on_gaierror(monkeypatch) -> None:
    """A DNS failure surfaces as ``ok=False`` with a typed error string.

    health() returns 503 when ``ok`` is False — covers the egress-broken path
    of the 413/503/429 incident workflow.
    """
    monkeypatch.setattr(config, "UPSTREAM", "https://does-not-exist.test")
    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(*_args, **_kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    ok, err = await proxy._check_upstream_egress()
    assert ok is False
    assert "gaierror" in err


async def test_check_upstream_egress_https_resolves_with_port_443(monkeypatch) -> None:
    """Happy path — ``https://`` derives port 443 when none is in the URL."""
    monkeypatch.setattr(config, "UPSTREAM", "https://api.example.test")
    loop = asyncio.get_running_loop()
    captured: dict[str, Any] = {}

    async def fake_getaddrinfo(host, port, **kwargs):
        captured["host"] = host
        captured["port"] = port
        captured["kwargs"] = kwargs
        return [("addr", "info")]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    ok, err = await proxy._check_upstream_egress()
    assert ok is True
    assert err == ""
    assert captured["host"] == "api.example.test"
    assert captured["port"] == 443
    # Caller forces TCP via type=SOCK_STREAM so we don't get UDP echoes.
    assert captured["kwargs"].get("type") == socket.SOCK_STREAM


async def test_check_upstream_egress_http_defaults_to_port_80(monkeypatch) -> None:
    """Plain ``http://`` derives port 80 when none is in the URL."""
    monkeypatch.setattr(config, "UPSTREAM", "http://internal.test")
    loop = asyncio.get_running_loop()
    captured: dict[str, Any] = {}

    async def fake_getaddrinfo(host, port, **_kwargs):
        captured["host"] = host
        captured["port"] = port
        return [("addr", "info")]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    ok, err = await proxy._check_upstream_egress()
    assert ok is True
    assert captured["port"] == 80


# ---------------------------------------------------------------------------
# _maybe_glide — opt-in proactive shrink at UTILIZATION_TARGET.
# ---------------------------------------------------------------------------


class _StubLimiter:
    """Minimal ``FairBearerLimiter`` stand-in with a counted async ``shrink``.

    The real limiter does AIMD math + cooldown checks; for ``_maybe_glide``
    coverage we only need to assert (a) it is awaited (or NOT awaited), and
    (b) its return value drives whether the debounce key is recorded.
    """

    def __init__(self, return_value: int | None) -> None:
        self.return_value = return_value
        self.shrink_calls = 0

    async def shrink(self) -> int | None:
        self.shrink_calls += 1
        return self.return_value


async def test_maybe_glide_disabled_when_target_zero(monkeypatch) -> None:
    """``UTILIZATION_TARGET<=0`` (default) must short-circuit immediately."""
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0)
    bstate: dict[str, object] = {}
    lim = _StubLimiter(return_value=4)
    await proxy._maybe_glide(
        "ee", bstate, cast(FairBearerLimiter, lim), {"util_5h": 0.95, "util_7d": 0.99}
    )
    assert lim.shrink_calls == 0
    assert "_util_shrink_key" not in bstate


async def test_maybe_glide_no_op_below_target(monkeypatch) -> None:
    """Binding utilization below target → no shrink, no debounce key written."""
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.9)
    bstate: dict[str, object] = {}
    lim = _StubLimiter(return_value=4)
    unified = {"util_5h": 0.50, "representative_claim": "five_hour"}
    await proxy._maybe_glide("ee", bstate, cast(FairBearerLimiter, lim), unified)
    assert lim.shrink_calls == 0
    assert "_util_shrink_key" not in bstate


async def test_maybe_glide_debounces_within_one_reset_window(monkeypatch) -> None:
    """A second call inside the same reset window MUST NOT call shrink twice.

    Without the per-reset debounce, an active swarm at util>=target collapses
    to one slot on every response (one shrink per turn) — see ``_maybe_glide``
    docstring.
    """
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.8)
    bstate: dict[str, object] = {}
    lim = _StubLimiter(return_value=4)
    unified = {
        "util_5h": 0.95,
        "reset_5h": 99999,
        "representative_claim": "five_hour",
    }

    await proxy._maybe_glide("ee", bstate, cast(FairBearerLimiter, lim), unified)
    assert lim.shrink_calls == 1
    # Stable per-reset key: ``f"{UTILIZATION_TARGET}:{reset}"``.
    assert bstate["_util_shrink_key"] == "0.8:99999"

    await proxy._maybe_glide("ee", bstate, cast(FairBearerLimiter, lim), unified)
    assert lim.shrink_calls == 1  # debounced — second call is a no-op


async def test_maybe_glide_floor_reached_keeps_key_unset(monkeypatch) -> None:
    """``shrink()`` returning ``None`` (floor reached) must NOT mark debounced.

    If the limiter is already at ``THROTTLE_AIMD_MIN``, shrink is a no-op and
    returns None. Recording the debounce key would silently swallow the next
    real shrink chance once the floor lifts — keep the key unset.
    """
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.8)
    bstate: dict[str, object] = {}
    lim = _StubLimiter(return_value=None)
    unified = {
        "util_5h": 0.95,
        "reset_5h": 99999,
        "representative_claim": "five_hour",
    }

    await proxy._maybe_glide("ee", bstate, cast(FairBearerLimiter, lim), unified)
    assert lim.shrink_calls == 1
    # Floor reached → debounce key NOT set (line 415 early return).
    assert "_util_shrink_key" not in bstate


async def test_maybe_glide_success_records_key_and_logs(monkeypatch) -> None:
    """Successful shrink writes the per-reset debounce key and emits one log.

    Format check is loose — we only assert the load-bearing tokens
    (``util-shrink``, bid, ``max_concurrent=2``) so future formatting tweaks
    don't ripple here.
    """
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.8)
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)

    bstate: dict[str, object] = {}
    lim = _StubLimiter(return_value=2)
    unified = {
        "util_5h": 0.95,
        "reset_5h": 22222,
        "representative_claim": "five_hour",
    }

    await proxy._maybe_glide("ee", bstate, cast(FairBearerLimiter, lim), unified)
    assert lim.shrink_calls == 1
    assert bstate["_util_shrink_key"] == "0.8:22222"
    assert any(
        "util-shrink" in line and "bid=ee" in line and "max_concurrent=2" in line for line in lines
    )


# ---------------------------------------------------------------------------
# root_probe — PR #29 — local GET/HEAD / probe, never forwarded upstream.
# ---------------------------------------------------------------------------


async def test_root_probe_get_returns_text_marker() -> None:
    """GET / returns the literal ``anthropic-throttle-proxy\\n`` body.

    Downstream tools (Dokku healthcheck, curl smoke test, load balancers)
    grep this token to confirm they hit the proxy and not a stale upstream.
    """
    request = make_mocked_request("GET", "/")
    response = await proxy.root_probe(request)
    assert response.status == 200
    assert response.text == "anthropic-throttle-proxy\n"


async def test_root_probe_head_returns_empty_200() -> None:
    """HEAD / must return 200 with no body — RFC 7231 compliant probe."""
    request = make_mocked_request("HEAD", "/")
    response = await proxy.root_probe(request)
    assert response.status == 200
    # aiohttp Response with no text/body has body=None until prepared; both
    # forms count as empty.
    assert response.body in (None, b"")


# ---------------------------------------------------------------------------
# _systemd_listen_sockets — socket-activation FD inheritance.
# ---------------------------------------------------------------------------


def test_systemd_listen_sockets_empty_env_returns_empty(monkeypatch) -> None:
    """No ``LISTEN_FDS`` → not socket-activated → empty list (use host:port)."""
    monkeypatch.delenv("LISTEN_FDS", raising=False)
    monkeypatch.delenv("LISTEN_PID", raising=False)
    monkeypatch.delenv("LISTEN_FDNAMES", raising=False)
    assert proxy._systemd_listen_sockets() == []


def test_systemd_listen_sockets_bad_fds_int_returns_empty(monkeypatch) -> None:
    """Non-integer ``LISTEN_FDS`` is corrupt env → degrade to host:port bind."""
    monkeypatch.setenv("LISTEN_FDS", "not-a-number")
    monkeypatch.delenv("LISTEN_PID", raising=False)
    assert proxy._systemd_listen_sockets() == []


def test_systemd_listen_sockets_zero_fds_returns_empty(monkeypatch) -> None:
    """``LISTEN_FDS=0`` is technically valid systemd but means "no sockets"."""
    monkeypatch.setenv("LISTEN_FDS", "0")
    monkeypatch.delenv("LISTEN_PID", raising=False)
    assert proxy._systemd_listen_sockets() == []


def test_systemd_listen_sockets_pid_mismatch_returns_empty(monkeypatch) -> None:
    """``LISTEN_PID`` not equal to ``getpid()`` means env was inherited stale."""
    monkeypatch.setenv("LISTEN_FDS", "1")
    # +999999 keeps us off any real PID on the host.
    monkeypatch.setenv("LISTEN_PID", str(os.getpid() + 999999))
    assert proxy._systemd_listen_sockets() == []


def test_systemd_listen_sockets_bad_pid_returns_empty(monkeypatch) -> None:
    """Non-integer ``LISTEN_PID`` is corrupt env → no sockets."""
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setenv("LISTEN_PID", "not-a-pid")
    assert proxy._systemd_listen_sockets() == []


class _FakeSocket:
    """``socket.socket`` stand-in that records constructor args + close calls."""

    def __init__(self, fileno: int | None = None) -> None:
        self.fileno_arg = fileno
        self.blocking = True
        self.closed = False

    def setblocking(self, flag: bool) -> None:
        self.blocking = flag

    def close(self) -> None:
        self.closed = True


def test_systemd_listen_sockets_success_dups_fds_and_pops_env(monkeypatch) -> None:
    """Happy path: 2 FDs are duped, marked non-inheritable, non-blocking, and
    the activation env keys are popped (sd_listen_fds(unset_environment=1))."""
    pid = os.getpid()
    monkeypatch.setenv("LISTEN_FDS", "2")
    monkeypatch.setenv("LISTEN_PID", str(pid))
    monkeypatch.setenv("LISTEN_FDNAMES", "tcp:tcp")

    dup_calls: list[int] = []
    setinh_calls: list[tuple[int, bool]] = []

    def fake_dup(fd: int) -> int:
        dup_calls.append(fd)
        return 100 + fd

    def fake_set_inheritable(fd: int, flag: bool) -> None:
        setinh_calls.append((fd, flag))

    monkeypatch.setattr(proxy.os, "dup", fake_dup)
    monkeypatch.setattr(proxy.os, "set_inheritable", fake_set_inheritable)
    monkeypatch.setattr(proxy.socket, "socket", _FakeSocket)

    socks = cast(list[_FakeSocket], proxy._systemd_listen_sockets())

    assert len(socks) == 2
    # systemd hands out FDs starting at 3.
    assert dup_calls == [3, 4]
    # Each duped FD is marked non-inheritable so child execs don't keep it.
    assert setinh_calls == [(103, False), (104, False)]
    # Each fake socket was constructed with the duped FD and then turned async.
    assert all(isinstance(s, _FakeSocket) for s in socks)
    assert {s.fileno_arg for s in socks} == {103, 104}
    assert all(s.blocking is False for s in socks)
    # finally: env is wiped so child processes don't see stale activation.
    assert "LISTEN_FDS" not in os.environ
    assert "LISTEN_PID" not in os.environ
    assert "LISTEN_FDNAMES" not in os.environ


def test_systemd_listen_sockets_oserror_closes_partial_and_pops_env(monkeypatch) -> None:
    """Mid-loop ``OSError`` (e.g. ``os.dup`` failure on FD 2 of 2) must:

    1. Close any sockets already constructed in this call (no FD leak).
    2. Re-raise so ``main()`` can fall back to host:port instead of silently
       binding nothing.
    3. STILL pop the env keys via the ``finally`` block (otherwise a retry
       reads stale activation metadata).
    """
    pid = os.getpid()
    monkeypatch.setenv("LISTEN_FDS", "2")
    monkeypatch.setenv("LISTEN_PID", str(pid))

    constructed: list[_FakeSocket] = []

    def tracking_socket(fileno: int | None = None) -> _FakeSocket:
        sock = _FakeSocket(fileno=fileno)
        constructed.append(sock)
        return sock

    call_count = [0]

    def fake_dup(_fd: int) -> int:
        call_count[0] += 1
        if call_count[0] == 1:
            return 103
        raise OSError("simulated dup failure on second FD")

    monkeypatch.setattr(proxy.os, "dup", fake_dup)
    monkeypatch.setattr(proxy.os, "set_inheritable", lambda *_a: None)
    monkeypatch.setattr(proxy.socket, "socket", tracking_socket)

    with pytest.raises(OSError, match="simulated dup failure"):
        proxy._systemd_listen_sockets()

    # Exactly one socket got constructed (FD 3) before the FD-4 dup blew up,
    # and the except-OSError branch closed it.
    assert len(constructed) == 1
    assert constructed[0].closed is True
    # finally: env STILL gets wiped on the raise path.
    assert "LISTEN_FDS" not in os.environ
    assert "LISTEN_PID" not in os.environ


# ---------------------------------------------------------------------------
# main() — invalid-mode warning at boot.
# ---------------------------------------------------------------------------


def test_main_logs_invalid_queue_mode_warning(monkeypatch, capsys) -> None:
    """When ``config.log_mode`` is non-empty, ``main()`` emits a warning line.

    ``config.py`` sets ``log_mode = QUEUE_MODE`` (and forces ``QUEUE_MODE="off"``)
    when the env value is not in ``{off, observe, fair, reactive}``. main()
    surfaces that to the journal so the operator sees the typo instead of
    silently running in passthrough.
    """
    monkeypatch.setattr(proxy.web, "run_app", lambda app, **kw: None)
    monkeypatch.setattr(proxy.asyncio, "set_event_loop", lambda _loop: None)
    monkeypatch.setattr(config, "load_overrides", lambda: None)
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(config, "log_mode", "garbage-mode")
    # Avoid touching socket-activation env / FD inheritance during the test.
    monkeypatch.setattr(proxy, "_systemd_listen_sockets", lambda: [])

    proxy.main()

    err = capsys.readouterr().err
    assert "invalid THROTTLE_QUEUE_MODE" in err
    # Format spec is ``{config.log_mode!r}`` so the value appears single-quoted.
    assert "'garbage-mode'" in err


# ---------------------------------------------------------------------------
# credential-failover nudge (THROTTLE_ACTIVE_CRED_PATH) — PR #59
# ---------------------------------------------------------------------------


def _write_cred(path: Any, token: str) -> str:
    """Write a minimal Claude credentials file; return the bearer_id the proxy
    would compute for that token (sha256 of the full ``Bearer <token>`` header)."""
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
    return hashlib.sha256(f"Bearer {token}".encode("utf-8", "replace")).hexdigest()[:8]


@pytest.fixture
def isolated_account_routing(monkeypatch: pytest.MonkeyPatch):
    """Keep account-router tests from leaking global limiter/account state."""
    accounts._cache.clear()
    accounts._endpoint_cache.clear()
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "off")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "")
    monkeypatch.setattr(config, "API_KEY_FILE", "")
    monkeypatch.setattr(config, "API_KEY_ROUTING_MODE", "off")
    monkeypatch.setattr(config, "API_KEY_LABEL", "API")
    monkeypatch.setattr(config, "API_KEY_MAX_CONCURRENT", 8)
    monkeypatch.setattr(proxy, "_api_key_cache", None)
    yield
    accounts._cache.clear()
    accounts._endpoint_cache.clear()
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    monkeypatch.setattr(proxy, "_api_key_cache", None)


def _setup_route_creds(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    cred_a = tmp_path / "a.json"
    cred_b = tmp_path / "b.json"
    bid_a = _write_cred(cred_a, "sk-ant-oat01-SIM-A")
    bid_b = _write_cred(cred_b, "sk-ant-oat01-SIM-B")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred_a},B:{cred_b}")
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    return bid_a, bid_b


def test_account_routing_disabled_keeps_incoming_bearer(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "off")
    headers = {"authorization": "Bearer incoming-token"}

    selected, label = proxy._route_account_if_enabled(
        headers, "incoming", method="POST", path="v1/messages"
    )

    assert selected == "incoming"
    assert label is None
    assert headers == {"authorization": "Bearer incoming-token"}


def test_account_routing_preserves_explicit_api_key(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _setup_route_creds(tmp_path, monkeypatch)
    headers = {"x-api-key": "sk-ant-api03-CLIENT", "x-test": "1"}
    incoming_bid = proxy._bearer_id(headers)

    selected, label = proxy._route_account_if_enabled(
        headers, incoming_bid, method="POST", path="v1/messages"
    )

    assert selected == incoming_bid
    assert label is None
    assert headers == {"x-api-key": "sk-ant-api03-CLIENT", "x-test": "1"}


def test_account_routing_selects_least_loaded_and_rewrites_authorization(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    bid_a, bid_b = _setup_route_creds(tmp_path, monkeypatch)
    config.bearer_limiters[bid_a] = FairBearerLimiter(8, "fair")
    config.bearer_limiters[bid_a].inflight = 2
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    headers = {"authorization": "Bearer sk-ant-oat01-SIM-A", "x-test": "1"}

    selected, label = proxy._route_account_if_enabled(
        headers, bid_a, method="POST", path="v1/messages"
    )

    assert selected == bid_b
    assert label == "B"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-B"
    assert "authorization" not in headers
    assert headers["x-test"] == "1"
    assert any(f"from={bid_a} to={bid_b} label=B" in line for line in lines)
    assert "SIM-B" not in "\n".join(lines)


def test_api_key_routing_prefer_rewrites_to_metered_key(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    key_file = tmp_path / "api-key"
    key_file.write_text("sk-ant-api03-SIM-KEY\n")
    monkeypatch.setattr(config, "API_KEY_FILE", str(key_file))
    monkeypatch.setattr(config, "API_KEY_ROUTING_MODE", "prefer")
    monkeypatch.setattr(config, "API_KEY_LABEL", "metered")
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    headers = {"Authorization": "Bearer sk-ant-oat01-SIM-A", "X-Test": "1"}

    selected, label = proxy._route_account_if_enabled(
        headers, "oauth-a", method="POST", path="v1/messages"
    )

    assert selected == proxy._api_key_bid("sk-ant-api03-SIM-KEY")
    assert selected.startswith("api-")
    assert label == "metered"
    assert "Authorization" not in headers
    assert headers["x-api-key"] == "sk-ant-api03-SIM-KEY"
    assert headers["X-Test"] == "1"
    assert "SIM-KEY" not in "\n".join(lines)
    assert any("api-key-route from=oauth-a" in line and "label=metered" in line for line in lines)


def test_api_key_routing_retry_after_falls_back_to_oauth(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    bid_a, _bid_b = _setup_route_creds(tmp_path, monkeypatch)
    key_file = tmp_path / "api-key"
    key_file.write_text("sk-ant-api03-SIM-KEY")
    monkeypatch.setattr(config, "API_KEY_FILE", str(key_file))
    monkeypatch.setattr(config, "API_KEY_ROUTING_MODE", "prefer")
    api_bid = proxy._api_key_bid("sk-ant-api03-SIM-KEY")
    lim = FairBearerLimiter(8, "fair")
    lim.note_retry_after(60)
    config.bearer_limiters[api_bid] = lim
    headers = {"Authorization": "Bearer sk-ant-oat01-STALE"}

    selected, label = proxy._route_account_if_enabled(
        headers, "stale", method="POST", path="v1/messages"
    )

    assert selected == bid_a
    assert label == "A"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-A"
    assert "x-api-key" not in headers


def test_api_key_bearer_id_and_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    api_value = "sk-ant-api03-SIM-KEY"
    bid = proxy._api_key_bid(api_value)
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "CENTRAL_URL", "http://central")
    monkeypatch.setattr(config, "MAX_CONCURRENT", 1)
    monkeypatch.setattr(config, "CENTRAL_LOCAL_MAX_CONCURRENT", 1)
    monkeypatch.setattr(config, "API_KEY_MAX_CONCURRENT", 9)

    assert proxy._bearer_id({"x-api-key": api_value}) == bid
    assert proxy._effective_admission("oauth-a") == ("fair", 1)
    assert proxy._effective_admission(bid) == ("fair", 9)


def test_account_routing_skips_retry_after_candidate(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    bid_a, bid_b = _setup_route_creds(tmp_path, monkeypatch)
    lim_b = FairBearerLimiter(8, "fair")
    lim_b.note_retry_after(3600)
    config.bearer_limiters[bid_b] = lim_b
    headers = {"Authorization": "Bearer sk-ant-oat01-SIM-B"}

    selected, label = proxy._route_account_if_enabled(
        headers, bid_b, method="POST", path="v1/messages"
    )

    assert selected == bid_a
    assert label == "A"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-A"


def test_account_routing_skips_persisted_retry_after_candidate(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    bid_a, bid_b = _setup_route_creds(tmp_path, monkeypatch)
    retry_state = tmp_path / "retry-after.json"
    retry_state.write_text(json.dumps({bid_b: time.time() + 3600}), encoding="utf-8")
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", str(retry_state))
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    headers = {"Authorization": "Bearer sk-ant-oat01-SIM-B"}

    selected, label = proxy._route_account_if_enabled(
        headers, bid_b, method="POST", path="v1/messages"
    )

    assert selected == bid_a
    assert label == "A"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-A"


# ---------------------------------------------------------------------------
# 15/07/2026 retry-after reroute incident (PR #106)
# A SHORT Retry-After window (<= MAX_HOLD_RETRY_AFTER_S) must NOT hard-gate a
# bearer out of routing — the dispatch path can hold through it. When it did,
# the router's empty-candidate fallback (return incoming_bid) masqueraded as a
# reroute and hopped the request onto the DEAD incoming bearer (41 h window),
# which then pre-dispatch-fast-failed instead of holding a couple seconds.
# ---------------------------------------------------------------------------


def test_short_retry_after_candidate_scored_finite(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """<= MAX_HOLD window: surcharged but routable; > MAX_HOLD: hard-gated inf."""
    _bid_a, bid_b = _setup_route_creds(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 60.0)

    short = FairBearerLimiter(8, "fair")
    short.note_retry_after(5)
    config.bearer_limiters[bid_b] = short
    acct = {"bearer_id": bid_b, "token": "sk-ant-oat01-SIM-B", "label": "B"}
    score = proxy._account_routing_candidate_score(acct, "incoming")
    assert math.isfinite(score)
    # Surcharge is ~5 s * 10/s above a clean idle bearer's baseline.
    assert score >= 5 * proxy._RETRY_AFTER_SURCHARGE_PER_S - 1

    short.note_retry_after(120)  # now past the hold ceiling
    assert proxy._account_routing_candidate_score(acct, "incoming") == math.inf


def test_short_retry_after_beats_dead_incoming_bearer(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Reroute picks the short-window configured bearer over a long-window one."""
    bid_a, bid_b = _setup_route_creds(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 60.0)
    # A (the incoming) is effectively dead: a 41 h window.
    dead_a = FairBearerLimiter(8, "fair")
    dead_a.note_retry_after(147800)
    config.bearer_limiters[bid_a] = dead_a
    # B has a tiny window the dispatch path can hold through.
    short_b = FairBearerLimiter(8, "fair")
    short_b.note_retry_after(2)
    config.bearer_limiters[bid_b] = short_b
    headers = {"Authorization": "Bearer sk-ant-oat01-SIM-A"}

    selected, label = proxy._route_account_if_enabled(
        headers, bid_a, method="POST", path="v1/messages"
    )

    assert selected == bid_b
    assert label == "B"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-B"


def test_clean_sibling_still_beats_short_retry_after(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A clean bearer must outrank a surcharged short-window one (no regression)."""
    bid_a, bid_b = _setup_route_creds(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 60.0)
    short_a = FairBearerLimiter(8, "fair")
    short_a.note_retry_after(5)
    config.bearer_limiters[bid_a] = short_a
    config.bearer_limiters[bid_b] = FairBearerLimiter(8, "fair")  # clean
    headers = {"Authorization": "Bearer sk-ant-oat01-SIM-A"}

    selected, label = proxy._route_account_if_enabled(
        headers, bid_a, method="POST", path="v1/messages"
    )

    assert selected == bid_b
    assert label == "B"


def test_reroute_refuses_hop_to_longer_window_bearer(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """B-only config: routing falls back to the DEAD incoming bearer; the reroute
    guard must reject a hop to a window >= the current one."""
    cred_b = tmp_path / "b.json"
    bid_b = _write_cred(cred_b, "sk-ant-oat01-SIM-B")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"B:{cred_b}")
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 60.0)
    incoming = "deadtab"
    dead = FairBearerLimiter(8, "fair")
    dead.note_retry_after(147800)
    config.bearer_limiters[incoming] = dead
    # B is over the hold ceiling too, so it is excluded and routing falls back
    # to the incoming bearer — exactly the masquerade the guard blocks.
    long_b = FairBearerLimiter(8, "fair")
    long_b.note_retry_after(120)
    config.bearer_limiters[bid_b] = long_b

    rerouted = proxy._retry_after_reroute_headers(
        {"Authorization": "Bearer sk-ant-oat01-DEAD"},
        incoming,
        bid_b,
        {bid_b},
        "POST",
        "v1/messages",
        "",
        None,
        current_remaining=proxy._bearer_retry_after_remaining(bid_b),
    )

    assert rerouted is None


def test_reroute_allows_hop_to_shorter_window_bearer(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Legit reroute still fires when the target can dispatch sooner."""
    bid_a, bid_b = _setup_route_creds(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 60.0)
    long_b = FairBearerLimiter(8, "fair")
    long_b.note_retry_after(50)
    config.bearer_limiters[bid_b] = long_b
    config.bearer_limiters[bid_a] = FairBearerLimiter(8, "fair")  # clean

    rerouted = proxy._retry_after_reroute_headers(
        {"Authorization": "Bearer sk-ant-oat01-SIM-B"},
        bid_b,
        bid_b,
        {bid_b},
        "POST",
        "v1/messages",
        "",
        None,
        current_remaining=proxy._bearer_retry_after_remaining(bid_b),
    )

    assert rerouted is not None
    next_bid, label, headers = rerouted
    assert next_bid == bid_a
    assert label == "A"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-A"


def test_all_configured_hard_gated_keeps_incoming_not_unusable_account(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """When every configured account is hard-gated (window > MAX_HOLD, i.e.
    genuinely unusable, not holdable), routing keeps the incoming bearer rather
    than hopping onto a will-fail account. Short holdable windows never reach
    this branch (the candidate score keeps them routable)."""
    cred_b = tmp_path / "b.json"
    bid_b = _write_cred(cred_b, "sk-ant-oat01-SIM-B")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"B:{cred_b}")
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 60.0)
    long_b = FairBearerLimiter(8, "fair")
    long_b.note_retry_after(120)  # over the hold ceiling → unusable
    config.bearer_limiters[bid_b] = long_b
    headers = {"Authorization": "Bearer sk-ant-oat01-STALE"}

    selected, label = proxy._route_account_if_enabled(
        headers, "staletab", method="POST", path="v1/messages"
    )

    assert (selected, label) == ("staletab", None)
    assert headers["Authorization"] == "Bearer sk-ant-oat01-STALE"


def test_reroute_skips_unconfigured_target_even_if_sooner(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A reroute target that is not a configured account (nor the API key) is
    rejected even when its window is SHORTER than the current bearer's."""
    bid_a, bid_b = _setup_route_creds(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 60.0)
    current = FairBearerLimiter(8, "fair")
    current.note_retry_after(40)  # current window
    config.bearer_limiters[bid_a] = current
    # Force the router to hand back an UNCONFIGURED bid with a shorter window,
    # so only the allowed-bids skip (not the worse-window guard) can catch it.
    unconf = FairBearerLimiter(8, "fair")
    unconf.note_retry_after(2)
    config.bearer_limiters["ghost"] = unconf
    monkeypatch.setattr(
        proxy, "_route_account_if_enabled", lambda headers, incoming, **kw: ("ghost", None)
    )
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)

    rerouted = proxy._retry_after_reroute_headers(
        {"Authorization": "Bearer sk-ant-oat01-SIM-A"},
        bid_a,
        bid_a,
        {bid_a},
        "POST",
        "v1/messages",
        "",
        None,
        current_remaining=proxy._bearer_retry_after_remaining(bid_a),
    )

    assert rerouted is None
    assert any("retry-after-reroute-skip" in line and "to=ghost" in line for line in lines)


def _probe_request() -> Any:
    return make_mocked_request("POST", "/v1/messages")


def _probe_body(**overrides: Any) -> bytes:
    payload = {"model": "claude-opus-4-8", "max_tokens": 1, "messages": [{"role": "user"}]}
    payload.update(overrides)
    return json.dumps(payload).encode()


def test_synthetic_probe_answers_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THROTTLE_SYNTHETIC_ONE_TOKEN_PROBES", "true")
    resp = proxy._synthetic_one_token_probe_response(
        _probe_request(), "v1/messages", _probe_body(), "claude-opus-4-8", 1, False
    )
    assert resp is not None
    payload = json.loads(resp.body)
    assert payload["id"].startswith("msg_throttle_probe_")
    assert payload["stop_reason"] == "max_tokens"
    assert payload["usage"]["output_tokens"] == 1
    assert payload["model"] == "claude-opus-4-8"


def test_synthetic_probe_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THROTTLE_SYNTHETIC_ONE_TOKEN_PROBES", raising=False)
    resp = proxy._synthetic_one_token_probe_response(
        _probe_request(), "v1/messages", _probe_body(), "claude-opus-4-8", 1, False
    )
    assert resp is None


@pytest.mark.parametrize(
    "max_tokens,has_tools,body",
    [
        (2, False, _probe_body(max_tokens=2)),  # not a 1-token probe
        (1, True, _probe_body()),  # carries tools
        (1, False, b"{" + b"x" * 4096),  # oversize body
        (1, False, None),  # missing body
    ],
)
def test_synthetic_probe_forwards_non_probes(
    monkeypatch: pytest.MonkeyPatch, max_tokens: int, has_tools: bool, body: bytes | None
) -> None:
    monkeypatch.setenv("THROTTLE_SYNTHETIC_ONE_TOKEN_PROBES", "true")
    resp = proxy._synthetic_one_token_probe_response(
        _probe_request(), "v1/messages", body, "claude-opus-4-8", max_tokens, has_tools
    )
    assert resp is None


def test_synthetic_probe_forwards_streaming_and_tool_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THROTTLE_SYNTHETIC_ONE_TOKEN_PROBES", "true")
    for override in ({"stream": True}, {"tool_choice": {"type": "auto"}}):
        resp = proxy._synthetic_one_token_probe_response(
            _probe_request(), "v1/messages", _probe_body(**override), "claude-opus-4-8", 1, False
        )
        assert resp is None


@pytest.mark.parametrize(
    "unified",
    [
        {"status": "allowed_warning", "util_5h": 0.37, "util_7d": 0.97},
        {"status": "allowed", "util_7d": 0.97},
    ],
)
def test_account_routing_skips_unified_pressure_candidate(
    isolated_account_routing,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    unified: dict[str, object],
) -> None:
    bid_a, bid_b = _setup_route_creds(tmp_path, monkeypatch)
    monkeypatch.setattr(proxy, "UTILIZATION_WARN", 0.9)
    config.bearer_state[bid_b] = {"unified": unified}
    headers = {"Authorization": "Bearer sk-ant-oat01-SIM-B"}

    selected, label = proxy._route_account_if_enabled(
        headers, bid_b, method="POST", path="v1/messages"
    )

    assert selected == bid_a
    assert label == "A"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-A"


def test_account_routing_skips_endpoint_rejected_candidate(
    isolated_account_routing,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    bid_a, bid_b = _setup_route_creds(tmp_path, monkeypatch)
    monkeypatch.setattr(proxy, "UTILIZATION_WARN", 0.9)
    accounts._endpoint_cache[str(tmp_path / "a.json")] = {
        "fetched": time.time(),
        "usage": {"util_5h": 1.0, "util_7d": 0.63},
        "err": None,
    }
    headers = {"Authorization": "Bearer sk-ant-oat01-SIM-A"}

    selected, label = proxy._route_account_if_enabled(
        headers, bid_a, method="POST", path="v1/messages"
    )

    assert selected == bid_b
    assert label == "B"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-B"


@pytest.mark.parametrize("cached_usage", [None, {"util_5h": 0.10, "util_7d": 0.20}])
def test_account_routing_skips_endpoint_429_candidate(
    isolated_account_routing,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    cached_usage: dict[str, float] | None,
) -> None:
    bid_a, bid_b = _setup_route_creds(tmp_path, monkeypatch)
    accounts._endpoint_cache[str(tmp_path / "b.json")] = {
        "fetched": time.time(),
        "usage": cached_usage,
        "err": "usage endpoint unavailable (429)",
    }
    headers = {"Authorization": "Bearer sk-ant-oat01-SIM-B"}

    selected, label = proxy._route_account_if_enabled(
        headers, bid_b, method="POST", path="v1/messages"
    )

    assert selected == bid_a
    assert label == "A"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-A"


def test_account_routing_uses_pressured_configured_account_for_stale_bearer(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    cred_a = tmp_path / "a.json"
    bid_a = _write_cred(cred_a, "sk-ant-oat01-SIM-A")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred_a}")
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(proxy, "UTILIZATION_WARN", 0.9)
    config.bearer_state[bid_a] = {"unified": {"status": "allowed_warning", "util_5h": 0.95}}
    headers = {"Authorization": "Bearer sk-ant-oat01-STALE-B"}

    selected, label = proxy._route_account_if_enabled(
        headers, "stale-b", method="POST", path="v1/messages"
    )

    assert selected == bid_a
    assert label == "A"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-A"


@pytest.mark.parametrize(("incoming_inflight", "expect_preserve"), [(0, True), (1, False)])
def test_account_routing_preserves_only_unloaded_healthy_known_unconfigured_bearer(
    isolated_account_routing,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    incoming_inflight: int,
    expect_preserve: bool,
) -> None:
    bid_a, _bid_b = _setup_route_creds(tmp_path, monkeypatch)
    monkeypatch.setattr(proxy, "UTILIZATION_WARN", 0.9)
    incoming_value = "Bearer sk-ant-oat01-KNOWN"
    incoming_bid = hashlib.sha256(incoming_value.encode()).hexdigest()[:8]
    limiter = FairBearerLimiter(8, "fair")
    limiter.inflight = incoming_inflight
    config.bearer_limiters[incoming_bid] = limiter
    config.bearer_state[incoming_bid] = {
        "unified": {"status": "allowed", "util_5h": 0.20, "util_7d": 0.32},
        "unified_at": time.time(),
    }
    headers = {"Authorization": incoming_value}

    selected, label = proxy._route_account_if_enabled(
        headers, incoming_bid, method="POST", path="v1/messages"
    )

    if expect_preserve:
        assert selected == incoming_bid
        assert label is None
        assert headers == {"Authorization": incoming_value}
    else:
        assert selected == bid_a
        assert label == "A"
        assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-A"


@pytest.mark.parametrize(
    "unified",
    [
        {"status": "allowed_warning", "util_5h": 0.40, "util_7d": 0.82},
        {"status": "allowed", "util_5h": 0.95, "util_7d": 0.30},
    ],
)
def test_account_routing_routes_pressured_known_unconfigured_bearer(
    isolated_account_routing,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    unified: dict[str, object],
) -> None:
    bid_a, _bid_b = _setup_route_creds(tmp_path, monkeypatch)
    monkeypatch.setattr(proxy, "UTILIZATION_WARN", 0.9)
    incoming_bid = "known-x"
    config.bearer_limiters[incoming_bid] = FairBearerLimiter(8, "fair")
    config.bearer_state[incoming_bid] = {"unified": unified, "unified_at": time.time()}
    headers = {"Authorization": "Bearer sk-ant-oat01-KNOWN"}

    selected, label = proxy._route_account_if_enabled(
        headers, incoming_bid, method="POST", path="v1/messages"
    )

    assert selected == bid_a
    assert label == "A"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-A"


def test_account_routing_routes_stale_known_unconfigured_bearer(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    bid_a, _bid_b = _setup_route_creds(tmp_path, monkeypatch)
    incoming_bid = "known-x"
    config.bearer_limiters[incoming_bid] = FairBearerLimiter(8, "fair")
    config.bearer_state[incoming_bid] = {
        "unified": {"status": "allowed", "util_5h": 0.20, "util_7d": 0.25},
        "unified_at": time.time() - proxy.UNIFIED_CACHE_FRESH_S - 1,
    }
    headers = {"Authorization": "Bearer sk-ant-oat01-KNOWN"}

    selected, label = proxy._route_account_if_enabled(
        headers, incoming_bid, method="POST", path="v1/messages"
    )

    assert selected == bid_a
    assert label == "A"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-A"


def test_account_routing_routes_retry_after_known_unconfigured_bearer(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    bid_a, _bid_b = _setup_route_creds(tmp_path, monkeypatch)
    incoming_bid = "known-x"
    limiter = FairBearerLimiter(8, "fair")
    limiter.note_retry_after(60)
    config.bearer_limiters[incoming_bid] = limiter
    config.bearer_state[incoming_bid] = {
        "unified": {"status": "allowed", "util_5h": 0.20, "util_7d": 0.25},
        "unified_at": time.time(),
    }
    headers = {"Authorization": "Bearer sk-ant-oat01-KNOWN"}

    selected, label = proxy._route_account_if_enabled(
        headers, incoming_bid, method="POST", path="v1/messages"
    )

    assert selected == bid_a
    assert label == "A"
    assert headers["Authorization"] == "Bearer sk-ant-oat01-SIM-A"


def test_fast_fail_429_when_account_routing_enabled(
    isolated_account_routing, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Routing owns account choice, so stale-bearer 401 nudges must not fire."""
    cred = tmp_path / ".credentials.json"
    _write_cred(cred, "sk-ant-oat01-ACTIVE")
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 15.0)
    monkeypatch.setattr(config, "ACTIVE_CRED_PATH", str(cred))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(proxy, "_active_bearer_cache", None)

    resp = proxy._retry_after_fast_fail_response("staletab", "v1/messages", 9000.0, source="t")

    assert resp is not None
    assert resp.status == 429


def test_active_account_bearer_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty ACTIVE_CRED_PATH → '' (feature off; callers keep the old fast-fail)."""
    monkeypatch.setattr(config, "ACTIVE_CRED_PATH", "")
    monkeypatch.setattr(proxy, "_active_bearer_cache", None)
    assert proxy._active_account_bearer() == ""


def test_active_account_bearer_reads_caches_and_invalidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Reads cred → bearer_id (header hash); caches by (mtime, size); a swap to the
    other account invalidates the cache so the next read returns the NEW bearer."""
    cred = tmp_path / ".credentials.json"
    bid_a = _write_cred(cred, "sk-ant-oat01-AAA")
    monkeypatch.setattr(config, "ACTIVE_CRED_PATH", str(cred))
    monkeypatch.setattr(proxy, "_active_bearer_cache", None)
    assert proxy._active_account_bearer() == bid_a
    # The (mtime, size, bearer) snapshot is now cached for the cheap repeat path.
    st = os.stat(cred)
    assert proxy._active_bearer_cache == (st.st_mtime_ns, st.st_size, bid_a)
    assert proxy._active_account_bearer() == bid_a
    # Broker swaps the active credential to the other account (distinct length →
    # (mtime, size) key changes): the cache must invalidate and re-read.
    bid_b = _write_cred(cred, "sk-ant-oat01-BBBBBBBBBBBBBBBBBBBB")
    assert bid_b != bid_a
    assert proxy._active_account_bearer() == bid_b


def test_active_account_bearer_missing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Missing/unreadable cred path → '' (never raises into the hot path)."""
    monkeypatch.setattr(config, "ACTIVE_CRED_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setattr(proxy, "_active_bearer_cache", None)
    assert proxy._active_account_bearer() == ""


def test_active_account_bearer_malformed(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Cred file without an access token → '' (degrade, do not crash)."""
    cred = tmp_path / ".credentials.json"
    cred.write_text("{not json")
    monkeypatch.setattr(config, "ACTIVE_CRED_PATH", str(cred))
    monkeypatch.setattr(proxy, "_active_bearer_cache", None)
    assert proxy._active_account_bearer() == ""


def test_fast_fail_holds_within_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """remaining <= MAX_HOLD → None (hold the request; neither fast-fail nor nudge)."""
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 15.0)
    monkeypatch.setattr(config, "ACTIVE_CRED_PATH", "")
    assert proxy._retry_after_fast_fail_response("bid1", "v1/messages", 5.0, source="t") is None


def test_fast_fail_429_when_nudge_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ACTIVE_CRED_PATH → historical 429 fast-fail carrying Retry-After."""
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 15.0)
    monkeypatch.setattr(config, "ACTIVE_CRED_PATH", "")
    resp = proxy._retry_after_fast_fail_response("bid1", "v1/messages", 9000.0, source="t")
    assert resp is not None
    assert resp.status == 429
    assert resp.headers["retry-after"] == "9000"


def test_fast_fail_429_when_bearer_is_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Request bearer == active on-disk bearer → 429: re-reading would not help,
    and a 401 here would loop. This is the both-accounts-exhausted case."""
    cred = tmp_path / ".credentials.json"
    active_bid = _write_cred(cred, "sk-ant-oat01-LIVE")
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 15.0)
    monkeypatch.setattr(config, "ACTIVE_CRED_PATH", str(cred))
    monkeypatch.setattr(proxy, "_active_bearer_cache", None)
    resp = proxy._retry_after_fast_fail_response(active_bid, "v1/messages", 9000.0, source="t")
    assert resp is not None
    assert resp.status == 429


def test_fast_fail_401_nudge_when_account_swapped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Stale request bearer != swapped active bearer → 401 nudge (no Retry-After),
    so claude's self-heal re-reads the swapped credential and adopts the account."""
    cred = tmp_path / ".credentials.json"
    active_bid = _write_cred(cred, "sk-ant-oat01-NEWACCOUNT")
    assert active_bid != "staletab"  # the bearer mismatch is what triggers the nudge
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 15.0)
    monkeypatch.setattr(config, "ACTIVE_CRED_PATH", str(cred))
    monkeypatch.setattr(proxy, "_active_bearer_cache", None)
    resp = proxy._retry_after_fast_fail_response("staletab", "v1/messages", 9000.0, source="t")
    assert resp is not None
    assert resp.status == 401
    assert "authentication_error" in resp.text
    assert "retry-after" not in {k.lower() for k in resp.headers}


async def test_retry_direct_once_nudges_swapped_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The central-down direct-retry path (which skips the pushback loop) still
    applies the 401 nudge when the active account was swapped — covers the wiring
    of bid into ``_retry_direct_once`` (Codex MAJOR for PR #59)."""
    cred = tmp_path / ".credentials.json"
    cred.write_text('{"claudeAiOauth": {"accessToken": "sk-ant-oat01-DIRECT"}}')
    monkeypatch.setattr(config, "ACTIVE_CRED_PATH", str(cred))
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 15.0)
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(config, "UPSTREAM", "http://direct.example")
    monkeypatch.setattr(proxy, "_active_bearer_cache", None)
    config.state["central_status"] = "up"

    async def stub(*args: Any, **_kw: Any) -> tuple[Any, None]:
        att = args[5]
        att.final_status = 429
        att.meta = {"retry-after": "88888"}
        att.response = proxy.web.Response(status=429)
        return att.response, None

    monkeypatch.setattr(proxy, "_try_forward", stub)
    fake_request = cast(Any, type("R", (), {"query_string": ""})())

    resp = await proxy._retry_direct_once(
        fake_request,
        {},
        None,
        "v1/messages",
        "central",
        "http://x/v1/messages",
        proxy.aiohttp.ClientTimeout(total=1),
        RuntimeError("central down"),
        proxy._Attempt(),
        "stale-bearer",
    )
    assert resp.status == 401
    assert "authentication_error" in resp.text


# ---------------------------------------------------------------------------
# _route_account_if_enabled — FR-019 pure-OAuth-Bearer passthrough lock
# ---------------------------------------------------------------------------


def test_account_route_rewrites_only_bearer_no_apikey(monkeypatch) -> None:
    """The router swaps ONLY Authorization to a Bearer of the operator's own
    token — never injects an api-key/auth-token (the #20976 trap that silently
    bills subscription traffic as API usage) and leaves every other header
    intact (FR-019/FR-022)."""
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "A:/tmp/x")
    monkeypatch.setattr(
        accounts,
        "routing_snapshot",
        lambda _now=None: [{"bearer_id": "fakeacct1", "token": "SECRET-TOKEN", "label": "A"}],
    )
    headers = {
        "Authorization": "Bearer incoming-token",
        "x-keep": "unchanged",
        "content-type": "application/json",
    }

    bid, label = proxy._route_account_if_enabled(
        headers, "incbid", method="POST", path="/v1/messages"
    )

    assert (bid, label) == ("fakeacct1", "A")
    assert headers["Authorization"] == "Bearer SECRET-TOKEN"
    assert headers["x-keep"] == "unchanged"
    assert headers["content-type"] == "application/json"
    banned = {"x-api-key", "api-key", "anthropic-auth-token", "anthropic_auth_token"}
    assert not any(k.lower() in banned for k in headers)


def test_account_route_disabled_leaves_headers_untouched(monkeypatch) -> None:
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "off")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "")
    headers = {"Authorization": "Bearer incoming-token"}
    bid, label = proxy._route_account_if_enabled(
        headers, "incbid", method="POST", path="/v1/messages"
    )
    assert (bid, label) == ("incbid", None)
    assert headers["Authorization"] == "Bearer incoming-token"


# ---------------------------------------------------------------------------
# FR-005 distinctness guard — collision gauge + debounced warning
# ---------------------------------------------------------------------------


def test_note_identity_collision_gauge_and_debounced_warn(monkeypatch) -> None:
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    proxy._identity_warn_state["sig"] = ""

    proxy._note_identity_collision({"duplicates": {"dup@pm.me": ["A", "B"]}})
    assert proxy.M_ACCOUNT_COLLISIONS._value.get() == 2
    assert len(lines) == 1 and "ACCOUNT COLLISION" in lines[0]
    assert "A+B" in lines[0] and "dup@pm.me" in lines[0]

    proxy._note_identity_collision({"duplicates": {"dup@pm.me": ["A", "B"]}})
    assert len(lines) == 1  # debounced

    proxy._note_identity_collision({"duplicates": {}})
    assert proxy.M_ACCOUNT_COLLISIONS._value.get() == 0
    assert len(lines) == 1  # clearing collision does not warn


def test_account_identity_verdict_none_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "")
    assert proxy._account_identity_verdict() is None


# ---------------------------------------------------------------------------
# FR-005 verify-before-warn — suspected collisions probed before alarming.
# 10/07 incident: claude-account-promote swapped .credentials.json between two
# stores and left both .claude.json labels behind → the guard mixed one fresh
# profile email with one stale label and warned a false A+B duplicate for
# ~2.6 h. Suspected groups must be probed live before warning.
# ---------------------------------------------------------------------------


def _write_guard_account(base, sub: str, token: str, email: str) -> str:
    """Synthetic credential + .claude.json label pair (NEVER a real token)."""
    acct = base / sub
    acct.mkdir()
    cred = acct / ".credentials.json"
    cred.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": "never-read"}})
    )
    (acct / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": email}}))
    return str(cred)


async def _promote_swap_scene(
    tmp_path, monkeypatch, profiles: dict[str, tuple[int, dict | None]], extra_spec: str = ""
) -> list[str]:
    """Two stores whose LOCAL labels agree (stale after a promote swap).

    Runs verdict → _note_identity_collision → awaits the spawned verification
    task; returns the captured log lines. ``profiles`` routes the stubbed
    ``/api/oauth/profile`` responses by token (absent = transport error).
    """
    a = _write_guard_account(tmp_path, "a", "tok-a", "dup@pm.me")
    b = _write_guard_account(tmp_path, "b", "tok-b", "dup@pm.me")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{a},B:{b}{extra_spec}")
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    monkeypatch.setattr(proxy, "_IDENTITY_VERIFY_RETRY_S", 0.0)
    proxy._identity_warn_state["sig"] = ""
    proxy._identity_warn_state["emitted_sus"] = ""
    proxy._identity_verify_tasks.clear()
    for cache in (
        accounts._cache,
        accounts._endpoint_cache,
        accounts._email_cache,
        accounts._local_identity_cache,
        accounts._verify_locks,
    ):
        cache.clear()

    async def fake_get_json(url: str, token: str):
        if "profile" in url:
            return profiles.get(token, (0, None))
        return 0, None

    monkeypatch.setattr(accounts, "_get_json", fake_get_json)

    verdict = proxy._account_identity_verdict()
    assert verdict is not None
    assert verdict["duplicates"] == {}
    assert verdict["suspected"] == {"dup@pm.me": ["A", "B"]}
    proxy._note_identity_collision(verdict)
    # Never warns inline — health stays quiet + fast while the probe runs.
    assert not any("ACCOUNT COLLISION" in ln for ln in lines)
    assert proxy.M_ACCOUNT_COLLISIONS._value.get() == 0
    assert proxy.M_ACCOUNT_SUSPECTED._value.get() == 2  # visible while pending
    task = next(iter(proxy._identity_verify_tasks.values()))
    await task
    return lines


async def test_suspected_collision_cleared_by_probe(tmp_path, monkeypatch) -> None:
    # The 10/07 false alarm: labels agree, live tokens are DISTINCT accounts.
    lines = await _promote_swap_scene(
        tmp_path,
        monkeypatch,
        {
            "tok-a": (200, {"account": {"email": "real-a@pm.me"}}),
            "tok-b": (200, {"account": {"email": "real-b@proton.me"}}),
        },
    )
    assert not any("ACCOUNT COLLISION" in ln for ln in lines)
    assert any("cleared by profile probe" in ln for ln in lines)
    assert proxy.M_ACCOUNT_COLLISIONS._value.get() == 0
    verdict = proxy._account_identity_verdict()
    assert verdict is not None
    assert verdict["distinct"] == 2 and verdict["suspected"] == {}


async def test_suspected_collision_confirmed_by_probe(tmp_path, monkeypatch) -> None:
    # Both live tokens really resolve to ONE account → the verified warning,
    # exactly as loud as before the suspected split existed.
    same = (200, {"account": {"email": "dup@pm.me"}})
    lines = await _promote_swap_scene(tmp_path, monkeypatch, {"tok-a": same, "tok-b": same})
    warns = [ln for ln in lines if "ACCOUNT COLLISION" in ln]
    assert len(warns) == 1 and "(unverified)" not in warns[0]
    assert "A+B" in warns[0] and "dup@pm.me" in warns[0]
    assert proxy.M_ACCOUNT_COLLISIONS._value.get() == 2


async def test_suspected_collision_probe_dead_warns_unverified(tmp_path, monkeypatch) -> None:
    # 09/07 class: every token dead → the collision must STILL surface, but
    # flagged unverified so nobody re-auths on a possibly-stale label alone.
    lines = await _promote_swap_scene(tmp_path, monkeypatch, {})
    warns = [ln for ln in lines if "ACCOUNT COLLISION" in ln]
    assert len(warns) == 1 and "(unverified)" in warns[0]
    assert proxy.M_ACCOUNT_COLLISIONS._value.get() == 0  # gauge counts VERIFIED only
    assert proxy.M_ACCOUNT_SUSPECTED._value.get() == 2  # …but suspected stays visible
    verdict = proxy._account_identity_verdict()
    assert verdict is not None
    assert verdict["suspected"] == {"dup@pm.me": ["A", "B"]}


async def test_suspected_collision_third_account_untouched(tmp_path, monkeypatch) -> None:
    # C rides alongside the A+B suspicion: it must never be probed (only
    # unverified SUSPECTED members are), and the cleared verdict must count it.
    c = _write_guard_account(tmp_path, "c", "tok-c", "solo@gmail.com")
    probed: list[str] = []

    async def spying_force_verify(path: str, _orig=accounts.force_verify_email):
        probed.append(path)
        return await _orig(path)

    monkeypatch.setattr(accounts, "force_verify_email", spying_force_verify)
    lines = await _promote_swap_scene(
        tmp_path,
        monkeypatch,
        {
            "tok-a": (200, {"account": {"email": "real-a@pm.me"}}),
            "tok-b": (200, {"account": {"email": "real-b@proton.me"}}),
        },
        extra_spec=f",C:{c}",
    )
    assert any("cleared by profile probe" in ln for ln in lines)
    assert not any("ACCOUNT COLLISION" in ln for ln in lines)
    assert probed and all(p != c for p in probed)  # C never probed
    verdict = proxy._account_identity_verdict()
    assert verdict is not None
    assert verdict["distinct"] == 3 and verdict["suspected"] == {}


async def test_concurrent_verifiers_emit_unverified_once(tmp_path, monkeypatch) -> None:
    # Codex re-verify MINOR: two verifier tasks with DIFFERENT suspected keys
    # reaching the same still-suspected verdict must emit ONE unverified
    # warning, not one each.
    lines = await _promote_swap_scene(tmp_path, monkeypatch, {})
    assert sum("(unverified)" in ln for ln in lines) == 1
    # A second verifier (different key, e.g. spawned off a churned suspected
    # set) concludes on the SAME verdict → suppressed by the emitted-sig gate.
    await proxy._verify_suspected_identity("other-key", {"dup@pm.me": ["A", "B"]})
    assert sum("(unverified)" in ln for ln in lines) == 1

    # Resolution re-arms the emitter: probe B alive + distinct → clear, then a
    # fresh suspicion (new transition) must warn again.
    async def alive(url: str, token: str):
        who = {"tok-a": "real-a@pm.me", "tok-b": "real-b@proton.me"}.get(token)
        return (200, {"account": {"email": who}}) if who else (0, None)

    monkeypatch.setattr(accounts, "_get_json", alive)
    await proxy._verify_suspected_identity("other-key", {"dup@pm.me": ["A", "B"]})
    assert any("cleared by profile probe" in ln for ln in lines)
    assert proxy._identity_warn_state["emitted_sus"] == ""


async def test_spawn_identity_verification_pop_race(monkeypatch) -> None:
    # Codex MAJOR: a stale done-callback firing AFTER a same-key respawn must
    # not evict the newer live task from the dedupe dict.
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "")  # verdict → None, task is a no-op
    monkeypatch.setattr(proxy, "log", lambda _m: None)
    proxy._identity_verify_tasks.clear()
    suspected = {"d@x": ["A", "B"]}
    key = proxy._verify_suspected_key(suspected)

    proxy._spawn_identity_verification(suspected)
    t1 = proxy._identity_verify_tasks[key]
    await t1  # done — but its done-callback has NOT run yet (call_soon)
    proxy._spawn_identity_verification(suspected)
    t2 = proxy._identity_verify_tasks[key]
    assert t2 is not t1
    await asyncio.sleep(0)  # now t1's stale callback fires
    assert proxy._identity_verify_tasks.get(key) is t2  # survived the stale pop
    await t2
    for _ in range(3):
        await asyncio.sleep(0)  # let t2's own callback clean up
    assert key not in proxy._identity_verify_tasks


async def test_retry_after_state_restored_for_new_limiter(tmp_path, monkeypatch) -> None:
    # Hotfix service restarts must not forget a known upstream Retry-After and
    # rediscover the same window with one fresh 429 on the first request.
    state_file = tmp_path / "retry-after.json"
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", str(state_file))
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    limiter.set_lock(asyncio.Lock())
    config.bearer_limiters.clear()
    config.bearer_state.clear()

    lim = await proxy._get_bearer_limiter("bid1", "fair", 3)
    lim.note_retry_after(60)
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    monkeypatch.setattr(limiter, "_retry_after_state", None)

    restored = await proxy._get_bearer_limiter("bid1", "fair", 3)

    assert restored.retry_after_remaining() > 50


async def test_retry_after_restore_capped(tmp_path, monkeypatch) -> None:
    # 13-14/07 incident: budget windows noted at 100% exhaustion end at the
    # account's reset epoch, but the rolling usage decays underneath. Every
    # restart resurrected the full ~58 h window from disk and re-blocked an
    # account that was back at 91-92%. A restored window is hearsay — cap it;
    # a genuinely exhausted account re-notes the honest window with one 429.
    state_file = tmp_path / "retry-after.json"
    state_file.write_text(json.dumps({"bid1": time.time() + 210_000}))
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", str(state_file))
    monkeypatch.setattr(config, "RETRY_AFTER_RESTORE_CAP_S", 900.0)
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    limiter.set_lock(asyncio.Lock())
    config.bearer_limiters.clear()
    config.bearer_state.clear()

    restored = await proxy._get_bearer_limiter("bid1", "fair", 3)

    assert 0 < restored.retry_after_remaining() <= 900


async def test_retry_after_restore_uncapped_when_knob_zero(tmp_path, monkeypatch) -> None:
    state_file = tmp_path / "retry-after.json"
    state_file.write_text(json.dumps({"bid1": time.time() + 210_000}))
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", str(state_file))
    monkeypatch.setattr(config, "RETRY_AFTER_RESTORE_CAP_S", 0.0)
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    limiter.set_lock(asyncio.Lock())
    config.bearer_limiters.clear()
    config.bearer_state.clear()

    restored = await proxy._get_bearer_limiter("bid1", "fair", 3)

    assert restored.retry_after_remaining() > 200_000


async def test_clear_retry_after_drops_live_and_persisted_window(tmp_path, monkeypatch) -> None:
    state_file = tmp_path / "retry-after.json"
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", str(state_file))
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    limiter.set_lock(asyncio.Lock())
    config.bearer_limiters.clear()
    config.bearer_state.clear()

    lim = await proxy._get_bearer_limiter("bid1", "fair", 3)
    lim.note_retry_after(50_000)
    assert lim.retry_after_remaining() > 49_000

    cleared = limiter.clear_retry_after("bid1")

    assert cleared > 49_000
    assert lim.retry_after_remaining() == 0.0
    assert "bid1" not in json.loads(state_file.read_text())
    assert limiter.clear_retry_after("bid1") == 0.0  # idempotent


async def test_retry_after_restore_cap_is_persisted(tmp_path, monkeypatch) -> None:
    # Codex MAJOR: capping was in-memory only, so the raw multi-day deadline
    # stayed on disk and every restart re-granted a fresh 900 s stale window.
    # The cap must be written back so subsequent loads see the capped deadline.
    state_file = tmp_path / "retry-after.json"
    state_file.write_text(json.dumps({"bid1": time.time() + 210_000}))
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", str(state_file))
    monkeypatch.setattr(config, "RETRY_AFTER_RESTORE_CAP_S", 900.0)
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    limiter.set_lock(asyncio.Lock())
    config.bearer_limiters.clear()
    config.bearer_state.clear()

    await proxy._get_bearer_limiter("bid1", "fair", 3)  # triggers cap + persist

    # Force a fresh limiter that re-restores from the (now-capped) file — a
    # second restart must NOT re-grant the original 210 000 s window.
    config.bearer_limiters.clear()
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    again = await proxy._get_bearer_limiter("bid1", "fair", 3)
    assert 0 < again.retry_after_remaining() <= 900


async def test_maybe_pause_rejected_caps_live_window(monkeypatch) -> None:
    # Codex BLOCKER: a live "rejected" window noted at the reset epoch can run
    # for days, and the central tier runs no usage poller to clear it. Cap the
    # LIVE pause so both tiers self-heal on a re-probe cadence; if still
    # rejected, the next request re-notes from a fresh 429.
    monkeypatch.setattr(config, "RETRY_AFTER_REJECT_CAP_S", 900.0)
    limiter.set_lock(asyncio.Lock())
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    lim = await proxy._get_bearer_limiter("bidr", "fair", 3)

    far_reset = int(time.time() + 200_000)
    unified = {"status": "rejected", "reset": far_reset}

    assert proxy._maybe_pause_rejected("bidr", lim, unified) is True
    assert 0 < lim.retry_after_remaining() <= 900


async def test_maybe_pause_rejected_uncapped_when_knob_zero(monkeypatch) -> None:
    monkeypatch.setattr(config, "RETRY_AFTER_REJECT_CAP_S", 0.0)
    limiter.set_lock(asyncio.Lock())
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    lim = await proxy._get_bearer_limiter("bidr0", "fair", 3)

    far_reset = int(time.time() + 200_000)
    unified = {"status": "rejected", "reset": far_reset}

    proxy._maybe_pause_rejected("bidr0", lim, unified)
    assert lim.retry_after_remaining() > 199_000  # legacy "pause until reset"


def test_publish_brake_enabled_reflects_target(monkeypatch) -> None:
    # Codex MAJOR: the gauge must be settable from metrics() too, not only
    # health() — verify the shared publisher tracks UTILIZATION_TARGET.
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.0)
    proxy._publish_brake_enabled()
    assert proxy.M_BRAKE_ENABLED._value.get() == 0
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.9)
    proxy._publish_brake_enabled()
    assert proxy.M_BRAKE_ENABLED._value.get() == 1


# ---------------------------------------------------------------------------
# spec 3 — model-aware routing (scoped per-model meter headroom)
# ---------------------------------------------------------------------------


def test_model_tier_normalization() -> None:
    assert proxy._model_tier("claude-sonnet-4-6") == "sonnet"
    assert proxy._model_tier("claude-opus-4-8") == "opus"
    assert proxy._model_tier("claude-haiku-4-5") == "haiku"
    assert proxy._model_tier("Fable") == "fable"
    assert proxy._model_tier("gpt-5") == ""
    assert proxy._model_tier("") == ""
    # Codex LOW: exact-token match, not substring; ambiguous >1 tier → unknown.
    assert proxy._model_tier("sonnets") == ""  # substring, not a token
    assert proxy._model_tier("claude-sonnet-opus-mix") == ""  # two tiers → ambiguous


def _acct(bid, tok, label, scoped_model, scoped_util):
    return {
        "bearer_id": bid,
        "token": tok,
        "label": label,
        "endpoint": {
            "usage": {
                "util_5h": 0.1,
                "util_7d": 0.1,
                "scoped": {"model": scoped_model, "util": scoped_util},
            }
        },
    }


def _route_for_accounts(
    monkeypatch,
    snapshot,
    *,
    paths="A:/x",
    model="claude-sonnet-4-6",
    mode="least_loaded",
    max_tokens=None,
):
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", mode)
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", paths)
    monkeypatch.setattr(accounts, "routing_snapshot", lambda _now=None: snapshot)
    headers = {"Authorization": "Bearer inc"}
    bid, label = proxy._route_account_if_enabled(
        headers, "inc", method="POST", path="/v1/messages", model=model, max_tokens=max_tokens
    )
    return bid, label, headers


_BUDGET_NOW = 1_800_000_000.0
_HOUR = 3600
_DAY = 24 * _HOUR


class _RoutingLimiter:
    def __init__(self, *, queued: int = 0, inflight: int = 0) -> None:
        self.queued = queued
        self.inflight = inflight

    def retry_after_remaining(self) -> float:
        return 0.0

    def snapshot(self) -> dict[str, int]:
        return {
            "queued_total": self.queued,
            "priority_queued": 0,
            "inflight": self.inflight,
        }


def _budget_acct(
    bid,
    tok,
    label,
    *,
    util_5h=0.1,
    reset_5h=None,
    util_7d=0.1,
    reset_7d=None,
    scoped_model=None,
    scoped_util=None,
    scoped_reset=None,
):
    usage = {
        "util_5h": util_5h,
        "util_7d": util_7d,
    }
    if reset_5h is not None:
        usage["reset_5h"] = reset_5h
    if reset_7d is not None:
        usage["reset_7d"] = reset_7d
    if scoped_model is not None and scoped_util is not None:
        usage["scoped"] = {"model": scoped_model, "util": scoped_util}
        if scoped_reset is not None:
            usage["scoped"]["reset"] = scoped_reset
    return {"bearer_id": bid, "token": tok, "label": label, "endpoint": {"usage": usage}}


def _warning_backpressure_accounts(queued: int) -> list[dict[str, object]]:
    config.bearer_limiters["aaa"] = _RoutingLimiter(queued=queued)
    config.bearer_state["bbb"] = {
        "unified": {
            "status": "allowed_warning",
            "util_5h": 0.50,
            "util_7d": 0.75,
            "reset_5h": _BUDGET_NOW + _HOUR,
            "reset_7d": _BUDGET_NOW + 3 * _DAY,
        }
    }
    return [
        _budget_acct("aaa", "TOKA", "A", util_5h=0.10, reset_5h=_BUDGET_NOW + _HOUR),
        _budget_acct("bbb", "TOKB", "B", util_5h=0.50, reset_5h=_BUDGET_NOW + _HOUR),
    ]


def _route_budget_for_accounts(
    _isolated_account_routing,
    monkeypatch,
    snapshot,
    *,
    model="claude-sonnet-4-6",
    max_tokens=None,
):
    monkeypatch.setattr(proxy.time, "time", lambda: _BUDGET_NOW)
    return _route_for_accounts(
        monkeypatch,
        snapshot,
        paths="A:/x,B:/y",
        model=model,
        mode="budget_paced",
        max_tokens=max_tokens,
    )


def _target_crossing_accounts() -> list[dict[str, object]]:
    reset_5h = _BUDGET_NOW + 30 * 60
    reset_7d = _BUDGET_NOW + 6 * _DAY
    return [
        _budget_acct("aaa", "TOKA", "A", util_5h=0.88, reset_5h=reset_5h, reset_7d=reset_7d),
        _budget_acct("bbb", "TOKB", "B", util_5h=0.70, reset_5h=reset_5h, reset_7d=reset_7d),
    ]


def test_budget_paced_same_reset_prefers_lower_util(isolated_account_routing, monkeypatch) -> None:
    reset_5h = _BUDGET_NOW + _HOUR
    reset_7d = _BUDGET_NOW + 3 * _DAY
    a = _budget_acct("aaa", "TOKA", "A", util_5h=0.30, reset_5h=reset_5h, reset_7d=reset_7d)
    b = _budget_acct("bbb", "TOKB", "B", util_5h=0.55, reset_5h=reset_5h, reset_7d=reset_7d)

    bid, label, headers = _route_budget_for_accounts(isolated_account_routing, monkeypatch, [a, b])

    assert (bid, label) == ("aaa", "A")
    assert headers["Authorization"] == "Bearer TOKA"


def test_budget_paced_can_spend_closer_reset_with_safe_slack(
    isolated_account_routing, monkeypatch
) -> None:
    # A has higher raw 5h utilization, but it resets in five minutes and still
    # has slack. B is lower utilization but too early in its cycle, so pacing
    # protects B and spends A before the reset.
    a = _budget_acct(
        "aaa",
        "TOKA",
        "A",
        util_5h=0.62,
        reset_5h=_BUDGET_NOW + 5 * 60,
        util_7d=0.01,
        reset_7d=_BUDGET_NOW + 6 * _DAY,
    )
    b = _budget_acct(
        "bbb",
        "TOKB",
        "B",
        util_5h=0.25,
        reset_5h=_BUDGET_NOW + 4 * _HOUR,
        util_7d=0.01,
        reset_7d=_BUDGET_NOW + 6 * _DAY,
    )

    bid, label, headers = _route_budget_for_accounts(isolated_account_routing, monkeypatch, [a, b])

    assert (bid, label) == ("aaa", "A")
    assert headers["Authorization"] == "Bearer TOKA"


def test_budget_paced_weekly_binding_overrides_5h_headroom(
    isolated_account_routing, monkeypatch
) -> None:
    # A looks attractive on 5h, but its 7d burn is far ahead of pace. The max
    # over budget windows makes weekly pressure binding and routes to B.
    a = _budget_acct(
        "aaa",
        "TOKA",
        "A",
        util_5h=0.10,
        reset_5h=_BUDGET_NOW + 5 * 60,
        util_7d=0.80,
        reset_7d=_BUDGET_NOW + 6 * _DAY,
    )
    b = _budget_acct(
        "bbb",
        "TOKB",
        "B",
        util_5h=0.35,
        reset_5h=_BUDGET_NOW + 5 * 60,
        util_7d=0.20,
        reset_7d=_BUDGET_NOW + 6 * _DAY,
    )

    bid, label, headers = _route_budget_for_accounts(isolated_account_routing, monkeypatch, [a, b])

    assert (bid, label) == ("bbb", "B")
    assert headers["Authorization"] == "Bearer TOKB"


def test_budget_paced_target_crossing_candidate_excluded(
    isolated_account_routing, monkeypatch
) -> None:
    # The live 12/07 failure class: a warning bearer was still "allowed" but
    # already at the cliff. With a 0.9 target, routing must not spend an Opus
    # turn that projects the account past the target and learns via 429.
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.9)

    bid, label, headers = _route_budget_for_accounts(
        isolated_account_routing,
        monkeypatch,
        _target_crossing_accounts(),
        model="claude-opus-4-8",
        max_tokens=64000,
    )

    assert (bid, label) == ("bbb", "B")
    assert headers["Authorization"] == "Bearer TOKB"


def test_budget_paced_queue_spillover_can_cross_soft_target(
    isolated_account_routing, monkeypatch
) -> None:
    # 12/07 live incident: cap=1 stopped upstream 429s, but the only strict
    # below-target account accumulated a deep local queue while warning accounts
    # sat idle. Once strict capacity is queued, spill to a near-target account
    # with a surcharge instead of surfacing queue-wait-timeout 503s.
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.9)
    config.bearer_limiters["bbb"] = _RoutingLimiter(queued=2)

    bid, label, headers = _route_budget_for_accounts(
        isolated_account_routing,
        monkeypatch,
        _target_crossing_accounts(),
        model="claude-opus-4-8",
        max_tokens=64000,
    )

    assert (bid, label) == ("aaa", "A")
    assert headers["Authorization"] == "Bearer TOKA"


def test_budget_paced_overpace_debt_steers_below_target(
    isolated_account_routing, monkeypatch
) -> None:
    # Both accounts are below the absolute target. A is ahead of the sustainable
    # slope for its 5h reset, while B has enough elapsed-window slack. Pick B
    # before A needs a reactive shrink/429.
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.9)
    reset_7d = _BUDGET_NOW + 6 * _DAY
    a = _budget_acct(
        "aaa", "TOKA", "A", util_5h=0.70, reset_5h=_BUDGET_NOW + 4 * _HOUR, reset_7d=reset_7d
    )
    b = _budget_acct(
        "bbb", "TOKB", "B", util_5h=0.82, reset_5h=_BUDGET_NOW + 30 * 60, reset_7d=reset_7d
    )

    bid, label, headers = _route_budget_for_accounts(isolated_account_routing, monkeypatch, [a, b])

    assert (bid, label) == ("bbb", "B")
    assert headers["Authorization"] == "Bearer TOKB"


def test_budget_paced_retry_after_candidate_excluded(isolated_account_routing, monkeypatch) -> None:
    a = _budget_acct("aaa", "TOKA", "A", util_5h=0.50, reset_5h=_BUDGET_NOW + _HOUR)
    b = _budget_acct("bbb", "TOKB", "B", util_5h=0.10, reset_5h=_BUDGET_NOW + _HOUR)
    lim_b = FairBearerLimiter(8, "fair")
    monkeypatch.setattr(proxy.time, "time", lambda: _BUDGET_NOW)
    lim_b.note_retry_after(3600)
    config.bearer_limiters["bbb"] = lim_b

    bid, label, headers = _route_budget_for_accounts(isolated_account_routing, monkeypatch, [a, b])

    assert (bid, label) == ("aaa", "A")
    assert headers["Authorization"] == "Bearer TOKA"


def test_budget_paced_endpoint_429_keeps_fresh_usage_candidate(
    isolated_account_routing, monkeypatch
) -> None:
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.95)
    b = _budget_acct("bbb", "TOKB", "B", util_5h=0.04, util_7d=0.07)
    b["endpoint"]["err"] = "usage endpoint unavailable (429)"

    bid, label, headers = _route_budget_for_accounts(
        isolated_account_routing,
        monkeypatch,
        [_budget_acct("aaa", "TOKA", "A", util_5h=0.01, util_7d=0.92), b],
        model="claude-opus-4-8",
        max_tokens=64000,
    )

    assert (bid, label) == ("bbb", "B")
    assert headers["Authorization"] == "Bearer TOKB"


def test_budget_paced_queue_inflight_prevents_dogpiling(
    isolated_account_routing, monkeypatch
) -> None:
    a = _budget_acct("aaa", "TOKA", "A", util_5h=0.05, reset_5h=_BUDGET_NOW + _HOUR)
    b = _budget_acct("bbb", "TOKB", "B", util_5h=0.85, reset_5h=_BUDGET_NOW + 4 * _HOUR)
    lim_a = FairBearerLimiter(8, "fair")
    lim_a.inflight = 1
    config.bearer_limiters["aaa"] = lim_a

    bid, label, headers = _route_budget_for_accounts(isolated_account_routing, monkeypatch, [a, b])

    assert (bid, label) == ("bbb", "B")
    assert headers["Authorization"] == "Bearer TOKB"


@pytest.mark.parametrize(
    ("queued", "expected", "auth"),
    [
        (0, ("aaa", "A"), "Bearer TOKA"),
        (1, ("bbb", "B"), "Bearer TOKB"),
    ],
)
def test_budget_paced_warning_account_surcharge_balances_queue_backpressure(
    isolated_account_routing, monkeypatch, queued, expected, auth
) -> None:
    bid, label, headers = _route_budget_for_accounts(
        isolated_account_routing, monkeypatch, _warning_backpressure_accounts(queued)
    )

    assert (bid, label) == expected
    assert headers["Authorization"] == auth


def test_budget_paced_scoped_meter_matches_request_model_only(
    isolated_account_routing, monkeypatch
) -> None:
    reset_7d = _BUDGET_NOW + 6 * _DAY
    a = _budget_acct(
        "aaa",
        "TOKA",
        "A",
        util_5h=0.10,
        reset_5h=_BUDGET_NOW + _HOUR,
        util_7d=0.10,
        reset_7d=reset_7d,
        scoped_model="Sonnet",
        scoped_util=0.85,
        scoped_reset=reset_7d,
    )
    b = _budget_acct(
        "bbb",
        "TOKB",
        "B",
        util_5h=0.30,
        reset_5h=_BUDGET_NOW + _HOUR,
        util_7d=0.30,
        reset_7d=reset_7d,
    )

    bid, label, headers = _route_budget_for_accounts(
        isolated_account_routing, monkeypatch, [a, b], model="claude-sonnet-4-6"
    )
    assert (bid, label) == ("bbb", "B")
    assert headers["Authorization"] == "Bearer TOKB"

    a["endpoint"]["usage"]["scoped"]["model"] = "Fable"
    bid, label, headers = _route_budget_for_accounts(
        isolated_account_routing, monkeypatch, [a, b], model="claude-sonnet-4-6"
    )
    assert (bid, label) == ("aaa", "A")
    assert headers["Authorization"] == "Bearer TOKA"


def test_expected_request_util_cost_uses_model_and_max_tokens() -> None:
    small_sonnet = proxy._expected_request_util_cost("claude-sonnet-4-6", 1024)
    large_sonnet = proxy._expected_request_util_cost("claude-sonnet-4-6", 32000)
    large_opus = proxy._expected_request_util_cost("claude-opus-4-8", 32000)

    assert small_sonnet < large_sonnet
    assert large_sonnet < large_opus
    assert large_sonnet == pytest.approx(proxy._DEFAULT_REQUEST_UTIL_COST * 4.0)


def test_account_route_model_aware_prefers_scoped_headroom(monkeypatch) -> None:
    # A's Sonnet meter is near cap, B's has headroom → a Sonnet request routes
    # to B even though both have equal all-models room.
    a = _acct("aaa", "TOKA", "A", "Sonnet", 0.95)
    b = _acct("bbb", "TOKB", "B", "Sonnet", 0.10)
    bid, label, headers = _route_for_accounts(monkeypatch, [a, b], paths="A:/x,B:/y")
    assert (bid, label) == ("bbb", "B")
    assert headers["Authorization"] == "Bearer TOKB"


def test_account_route_scoped_ignored_on_tier_mismatch(monkeypatch) -> None:
    # A's scoped meter tracks FABLE at 95%; a SONNET request must NOT be
    # penalized by it (Fable≠Sonnet) — A stays a viable candidate.
    bid, label, _headers = _route_for_accounts(
        monkeypatch, [_acct("aaa", "TOKA", "A", "Fable", 0.95)]
    )
    assert (bid, label) == ("aaa", "A")


def test_account_route_scoped_full_excluded_even_in_fallback(monkeypatch) -> None:
    # scoped meter at 1.0 → excluded in BOTH passes (never picked, even as the
    # only option) → no candidate → no routing (incoming bid unchanged).
    bid, label, headers = _route_for_accounts(
        monkeypatch, [_acct("aaa", "TOKA", "A", "Sonnet", 1.0)]
    )
    assert (bid, label) == ("inc", None)
    assert headers["Authorization"] == "Bearer inc"


def test_account_route_scoped_warn_picked_as_only_option(monkeypatch) -> None:
    # scoped near cap (<1.0): first pass rejects at warn, second pass
    # (allow_pressure) picks it since it is the only option.
    bid, label, headers = _route_for_accounts(
        monkeypatch, [_acct("aaa", "TOKA", "A", "Sonnet", 0.95)]
    )
    assert (bid, label) == ("aaa", "A")
    assert headers["Authorization"] == "Bearer TOKA"


def test_account_route_no_model_ignores_scoped(monkeypatch) -> None:
    # model='' → scoped fold skipped, identical to pre-spec-3 behavior.
    bid, label, _headers = _route_for_accounts(
        monkeypatch, [_acct("aaa", "TOKA", "A", "Sonnet", 0.95)], model=""
    )
    assert (bid, label) == ("aaa", "A")  # 0.95 scoped ignored → routes on all-models


def test_account_route_malformed_scoped_no_crash(monkeypatch) -> None:
    bid, label, _headers = _route_for_accounts(
        monkeypatch, [_acct("aaa", "TOKA", "A", "Sonnet", None)]
    )
    assert (bid, label) == ("aaa", "A")  # scoped util=None → not folded, no crash
