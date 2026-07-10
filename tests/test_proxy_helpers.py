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
import os
import socket
import time
from typing import Any, cast

import pytest
from aiohttp.test_utils import make_mocked_request

from anthropic_throttle_proxy import accounts, config, proxy
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
    yield
    accounts._cache.clear()
    accounts._endpoint_cache.clear()
    config.bearer_limiters.clear()
    config.bearer_state.clear()


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


def test_account_routing_skips_endpoint_429_candidate(
    isolated_account_routing,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    bid_a, bid_b = _setup_route_creds(tmp_path, monkeypatch)
    accounts._endpoint_cache[str(tmp_path / "b.json")] = {
        "fetched": time.time(),
        "usage": None,
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


def test_account_route_model_aware_prefers_scoped_headroom(monkeypatch) -> None:
    # A's Sonnet meter is near cap, B's has headroom → a Sonnet request routes
    # to B even though both have equal all-models room.
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "A:/x,B:/y")
    a = _acct("aaa", "TOKA", "A", "Sonnet", 0.95)
    b = _acct("bbb", "TOKB", "B", "Sonnet", 0.10)
    monkeypatch.setattr(accounts, "routing_snapshot", lambda _now=None: [a, b])
    headers = {"Authorization": "Bearer inc"}
    bid, label = proxy._route_account_if_enabled(
        headers, "inc", method="POST", path="/v1/messages", model="claude-sonnet-4-6"
    )
    assert (bid, label) == ("bbb", "B")
    assert headers["Authorization"] == "Bearer TOKB"


def test_account_route_scoped_ignored_on_tier_mismatch(monkeypatch) -> None:
    # A's scoped meter tracks FABLE at 95%; a SONNET request must NOT be
    # penalized by it (Fable≠Sonnet) — A stays a viable candidate.
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "A:/x")
    a = _acct("aaa", "TOKA", "A", "Fable", 0.95)
    monkeypatch.setattr(accounts, "routing_snapshot", lambda _now=None: [a])
    headers = {"Authorization": "Bearer inc"}
    bid, label = proxy._route_account_if_enabled(
        headers, "inc", method="POST", path="/v1/messages", model="claude-sonnet-4-6"
    )
    assert (bid, label) == ("aaa", "A")
