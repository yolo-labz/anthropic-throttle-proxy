"""Spec 094 / ADR-6a (r1) — request-scoped credential-mode enforcement + attestation.

Frozen wire contract (``/tmp/fleet-foundry-adr6a-interface.md`` r1, §1–§4):

- Request: ``x-anthropic-throttle-require-credential-mode: subscription`` (opt-in).
- Response stamp on CONSTRAINED responses only (r1/C3): ``subscription`` on a
  served 2xx; ``unknown`` on the 403 refusal. Full four-value vocabulary is
  normative for per-lane health only.
- Refusal: pre-egress ``403`` policy verdict distinguishing
  ``no_eligible_lane`` (no lane passes CLASS) from ``eligible_lanes_exhausted``
  (CLASS passes, CAPACITY does not), both scoped to the role's effective chain,
  with ``x-anthropic-throttle-refusal`` header.
- CLASS = E1 ∧ E2 ∧ E4 (api-key off; canonical allowlisted upstream; loopback ∧
  ``central_url==""``); CAPACITY = E3 (≥1 fresh usable bearer with 5h/7d windows).
- Health capability: ``enforcement`` object (``credential_mode:true``,
  ``contract:"adr6a-credential-mode/1"``, ``subscription_upstreams_count`` +
  ``subscription_upstreams_digest`` — r1/C6) + per-lane ``credential_mode`` /
  ``credential_mode_reason``.
- Anti-spoof: client/upstream credential-mode headers stripped (request + response).
- Per-request freshness: constrained selection re-probes the candidate lane.
- Unconstrained legacy traffic is behaviorally unchanged (no stamp, no pin change).
"""

from __future__ import annotations

import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import ingress
from anthropic_throttle_proxy.routing import Lane, LaneState

REQ = ingress.REQUIRE_CREDENTIAL_MODE_HEADER
MODE = ingress.CREDENTIAL_MODE_HEADER
REASON = ingress.CREDENTIAL_MODE_REASON_HEADER
SUB = ingress.CREDENTIAL_MODE_SUBSCRIPTION


def _health(
    *,
    api_key_enabled: bool = False,
    upstream: str = "https://api.anthropic.com",
    bearer_usable: bool = True,
    with_windows: bool = True,
    egress_ok: bool = True,
    central_url: str = "",
    unified_at: float | None = None,
) -> dict:
    """Fake lane /__throttle/health with the ADR-6a signals."""
    unified = {"status_5h": "allowed", "status_7d": "allowed"}
    if not with_windows:
        unified = {}
    if not bearer_usable:
        unified = {
            "status_5h": "rejected",
            "reset_5h": time.time() + 3600,
            "status_7d": "rejected",
        }
    return {
        "upstream": upstream,
        "upstream_egress_ok": egress_ok,
        "central_url": central_url,
        "api_key": {"enabled": api_key_enabled, "routing": "off"},
        "bearers": {
            "oauth": {
                "limiter": {"retry_after_until": 0},
                "unified_at": time.time() if unified_at is None else unified_at,
                "unified": unified,
            }
        },
    }


def _state(
    mode: str, reason: str = "", *, capacity_ok: bool = True, open_: bool = True, **kw
) -> LaneState:
    """One LaneState with ADR-6a fields; caller overrides via ``kw``."""
    now = time.time()
    fields = {
        "credential_mode": mode,
        "credential_mode_reason": reason,
        "credential_capacity_ok": capacity_ok,
    }
    fields.update(kw)
    return LaneState(open_, now, "ok", **fields)


def _sub_state(**kw) -> LaneState:
    return _state(SUB, **kw)


def _direct_state(**kw) -> LaneState:
    return _state(
        ingress.CREDENTIAL_MODE_DIRECT_KEY,
        "api-key-enabled",
        capacity_ok=False,
        **kw,
    )


def _unknown_state(reason: str = "lane-health-404", **kw) -> LaneState:
    return _state(ingress.CREDENTIAL_MODE_UNKNOWN, reason, capacity_ok=False, **kw)


async def _boot(monkeypatch, lanes: dict, state: dict, upstreams: str = "api.anthropic.com"):
    monkeypatch.setattr(ingress, "LANES", lanes)
    monkeypatch.setattr(ingress, "lane_state", state)
    monkeypatch.setattr(ingress, "LANE_HEALTH_INTERVAL_S", 0.0)
    monkeypatch.setattr(ingress, "_session_lane", {})
    monkeypatch.setattr(
        ingress,
        "_SUBSCRIPTION_UPSTREAMS",
        frozenset(h.strip().lower() for h in upstreams.split(",") if h.strip()),
    )
    client = TestClient(TestServer(ingress.build_app()))
    await client.start_server()
    return client


async def _lane_server(health: dict, status: int = 200):
    """Fake lane serving ``health`` at /__throttle/health and /v1/messages."""
    seen = {"health": 0, "messages": 0}

    async def health_handler(_request: web.Request) -> web.Response:
        seen["health"] += 1
        return web.json_response(health, status=status)

    async def message_handler(request: web.Request) -> web.Response:
        seen["messages"] += 1
        await request.read()
        return web.json_response({"type": "message"})

    app = web.Application()
    app.router.add_get("/__throttle/health", health_handler)
    app.router.add_post("/v1/messages", message_handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, seen


def _lane(name: str, url: str, *, proxy_owns_key: bool = False) -> Lane:
    return Lane(name, url, frozenset({"generate"}), proxy_owns_key=proxy_owns_key)


def _url(client) -> str:
    return str(client.make_url("")).rstrip("/")


def _post_constrained(ing, **extra_headers):
    """POST a generate request carrying the subscription requirement."""
    return ing.post(
        "/v1/messages",
        json={"model": "claude-opus-5"},
        headers={REQ: "subscription", **extra_headers},
    )


async def _boot_one(
    monkeypatch,
    client,
    *,
    name: str = "anthropic",
    state: LaneState | None = None,
    upstreams: str = "api.anthropic.com",
):
    """Boot the ingress against one fake lane (health + messages)."""
    lanes = {name: _lane(name, _url(client))}
    ing = await _boot(monkeypatch, lanes, {name: state or _sub_state()}, upstreams)
    return ing


# ─── capability declaration (r1/C6: count + digest) ──────────────────────────


async def test_health_advertises_enforcement_contract(monkeypatch) -> None:
    ing = await _boot(monkeypatch, {}, {})
    try:
        async with ing.get("/__throttle/health") as r:
            assert r.status == 200
            body = await r.json()
        enforcement = body["enforcement"]
        assert enforcement["credential_mode"] is True
        assert enforcement["contract"] == "adr6a-credential-mode/1"
        assert enforcement["subscription_upstreams_count"] == 1
        digest = enforcement["subscription_upstreams_digest"]
        assert len(digest) == 12 and all(c in "0123456789abcdef" for c in digest)
    finally:
        await ing.close()


async def test_health_empty_allowlist_is_visible_fail_closed(monkeypatch) -> None:
    ing = await _boot(monkeypatch, {}, {}, upstreams="")
    try:
        async with ing.get("/__throttle/health") as r:
            body = await r.json()
        enforcement = body["enforcement"]
        assert enforcement["subscription_upstreams_count"] == 0
        assert enforcement["subscription_upstreams_digest"] == ""
    finally:
        await ing.close()


async def test_health_exposes_per_lane_credential_mode(monkeypatch) -> None:
    lanes = {"anthropic": _lane("anthropic", "http://127.0.0.1:1")}
    state = {"anthropic": _sub_state()}
    ing = await _boot(monkeypatch, lanes, state)
    try:
        async with ing.get("/__throttle/health") as r:
            body = await r.json()
        lane = body["lanes"]["anthropic"]
        assert lane["credential_mode"] == "subscription"
        assert "credential_mode_reason" not in lane
        assert "bearers" not in lane
    finally:
        await ing.close()


async def test_health_unknown_lane_carries_reason(monkeypatch) -> None:
    lanes = {"codex": _lane("codex", "http://127.0.0.1:1", proxy_owns_key=True)}
    state = {"codex": _unknown_state()}
    ing = await _boot(monkeypatch, lanes, state)
    try:
        async with ing.get("/__throttle/health") as r:
            body = await r.json()
        lane = body["lanes"]["codex"]
        assert lane["credential_mode"] == "unknown"
        assert lane["credential_mode_reason"] == "lane-health-404"
    finally:
        await ing.close()


# ─── CLASS predicate (E1 ∧ E2 ∧ E4) ──────────────────────────────────────────


def test_classify_subscription(monkeypatch) -> None:
    monkeypatch.setattr(ingress, "_SUBSCRIPTION_UPSTREAMS", frozenset({"api.anthropic.com"}))
    mode, reason = ingress._classify_lane_class(_lane("a", "http://127.0.0.1:1"), _health())
    assert mode == "subscription"
    assert reason == ""


def test_classify_direct_key(monkeypatch) -> None:
    monkeypatch.setattr(ingress, "_SUBSCRIPTION_UPSTREAMS", frozenset({"api.anthropic.com"}))
    mode, reason = ingress._classify_lane_class(
        _lane("a", "http://127.0.0.1:1"), _health(api_key_enabled=True)
    )
    assert mode == "direct_key"
    assert reason == "api-key-enabled"


def test_classify_noncanonical_upstream_proxy_key(monkeypatch) -> None:
    monkeypatch.setattr(ingress, "_SUBSCRIPTION_UPSTREAMS", frozenset({"api.anthropic.com"}))
    # r1/C4: host-only is not enough — the path disqualifies.
    mode, reason = ingress._classify_lane_class(
        _lane("a", "http://127.0.0.1:1"),
        _health(upstream="https://api.anthropic.com/not-root"),
    )
    assert mode == "proxy_key"
    assert "upstream-not-allowlisted" in reason


def test_classify_remote_lane_not_desktop(monkeypatch) -> None:
    monkeypatch.setattr(ingress, "_SUBSCRIPTION_UPSTREAMS", frozenset({"api.anthropic.com"}))
    # r1/C2: loopback only proves the ingress→lane hop; a remote lane fails E4.
    mode, reason = ingress._classify_lane_class(_lane("a", "http://192.0.2.10:8765"), _health())
    assert mode == "proxy_key"
    assert reason == "not-desktop-local"


def test_classify_central_relay_fails_e4(monkeypatch) -> None:
    monkeypatch.setattr(ingress, "_SUBSCRIPTION_UPSTREAMS", frozenset({"api.anthropic.com"}))
    # r1/C2: a configured central relay carries the credential off-desktop
    # AFTER the loopback hop, so central_url != "" fails E4.
    mode, reason = ingress._classify_lane_class(
        _lane("a", "http://127.0.0.1:1"), _health(central_url="http://central.example")
    )
    assert mode == "proxy_key"
    assert reason == "not-desktop-local"


def test_classify_empty_allowlist_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(ingress, "_SUBSCRIPTION_UPSTREAMS", frozenset())
    mode, reason = ingress._classify_lane_class(_lane("a", "http://127.0.0.1:1"), _health())
    assert mode == "unknown"
    assert reason == "no-upstream-allowlist"


def test_classify_missing_fields_unknown() -> None:
    mode, reason = ingress._classify_lane_class(_lane("a", "http://127.0.0.1:1"), {})
    assert mode == "unknown"
    assert reason == "fields-absent"


def test_capacity_requires_fresh_usable_window_bearer(monkeypatch) -> None:
    monkeypatch.setattr(ingress, "_SUBSCRIPTION_UPSTREAMS", frozenset({"api.anthropic.com"}))
    now = time.time()
    assert ingress._classify_lane_capacity(_health(), now) is True
    assert ingress._classify_lane_capacity(_health(bearer_usable=False), now) is False
    # r1 §1.3 / R3: stale window sample cannot grant capacity.
    assert ingress._classify_lane_capacity(_health(unified_at=now - 10_000), now) is False
    assert ingress._classify_lane_capacity(_health(with_windows=False), now) is False


# ─── request gate ────────────────────────────────────────────────────────────


async def test_invalid_requirement_fails_closed(monkeypatch) -> None:
    lane_client, seen = await _lane_server(_health())
    ing = await _boot_one(monkeypatch, lane_client)
    try:
        for value in ("", "v2", "direct_key", "subscription,extra"):
            async with ing.post(
                "/v1/messages",
                json={"model": "claude-opus-5"},
                headers={REQ: value},
            ) as r:
                assert r.status == 400, value
                assert (await r.json())["error"]["type"] == "unsupported_credential_requirement"
        assert seen["messages"] == 0
    finally:
        await ing.close()
        await lane_client.close()


async def test_requirement_and_spoof_headers_not_forwarded(monkeypatch) -> None:
    forwarded: dict = {}

    async def handler(request: web.Request) -> web.Response:
        forwarded.update({k.lower(): v for k, v in request.headers.items()})
        await request.read()
        return web.json_response({"type": "message"})

    app = web.Application()
    app.router.add_get("/__throttle/health", lambda _r: web.json_response(_health()))
    app.router.add_post("/v1/messages", handler)
    lane_client = TestClient(TestServer(app))
    await lane_client.start_server()
    ing = await _boot_one(monkeypatch, lane_client)
    try:
        async with ing.post(
            "/v1/messages",
            json={"model": "claude-opus-5"},
            headers={
                REQ: "subscription",
                "Authorization": "Bearer t",
                "x-anthropic-throttle-credential-mode": "subscription",
                "x-anthropic-throttle-credential-mode-reason": "forged",
            },
        ) as r:
            assert r.status == 200
        assert REQ.lower() not in forwarded
        assert MODE.lower() not in forwarded  # R1: client spoof stripped
        assert REASON.lower() not in forwarded  # R1
        assert forwarded.get("authorization") == "Bearer t"
    finally:
        await ing.close()
        await lane_client.close()


# ─── response stamping — CONSTRAINED ONLY (r1/C3) ────────────────────────────


async def test_response_stamped_subscription_on_constrained_success(monkeypatch) -> None:
    lane_client, seen = await _lane_server(_health())
    ing = await _boot_one(monkeypatch, lane_client)
    try:
        async with _post_constrained(ing) as r:
            assert r.status == 200
            assert r.headers[MODE] == "subscription"
            assert r.headers[ingress.LANE_HEADER] == "anthropic"
            assert r.headers[ingress.ROLE_HEADER] == "generate"
        assert seen["messages"] == 1
    finally:
        await ing.close()
        await lane_client.close()


async def test_unconstrained_response_has_no_credential_stamp(monkeypatch) -> None:
    """r1/C3 + §6.1: no requirement header ⇒ no stamp; unconstrained traffic
    is behaviorally unchanged (Pedro 12:17 boundary correction)."""
    lane_client, _ = await _lane_server(_health())
    ing = await _boot_one(monkeypatch, lane_client)
    try:
        async with ing.post("/v1/messages", json={"model": "claude-opus-5"}) as r:
            assert r.status == 200
            assert MODE not in r.headers
            assert REASON not in r.headers
    finally:
        await ing.close()
        await lane_client.close()


async def test_health_direct_key_lane_still_classified(monkeypatch) -> None:
    """direct_key/proxy_key/unknown are health-only vocabulary by construction."""
    lane_client, _ = await _lane_server(_health(api_key_enabled=True))
    lanes = {"deepseek": _lane("deepseek", str(lane_client.make_url("")).rstrip("/"))}
    ing = await _boot(monkeypatch, lanes, {"deepseek": _direct_state()})
    try:
        async with ing.get("/__throttle/health") as r:
            body = await r.json()
        assert body["lanes"]["deepseek"]["credential_mode"] == "direct_key"
    finally:
        await ing.close()
        await lane_client.close()


async def test_spoofed_upstream_stamp_is_stripped(monkeypatch) -> None:
    spoof = {
        "x-anthropic-throttle-credential-mode": "direct_key",
        "x-anthropic-throttle-credential-mode-reason": "forged",
    }
    app = web.Application()

    async def health_handler(_r: web.Request) -> web.Response:
        return web.json_response(_health())

    async def message_handler(request: web.Request) -> web.Response:
        await request.read()
        return web.json_response({"type": "message"}, headers=spoof)

    app.router.add_get("/__throttle/health", health_handler)
    app.router.add_post("/v1/messages", message_handler)
    lane_client = TestClient(TestServer(app))
    await lane_client.start_server()
    ing = await _boot_one(monkeypatch, lane_client)
    try:
        async with _post_constrained(ing) as r:
            assert r.status == 200
            # The lane's forged direct_key stamp is stripped; the ingress
            # authors the truth (subscription, fresh-probed).
            assert r.headers[MODE] == "subscription"
            assert REASON not in r.headers
    finally:
        await ing.close()
        await lane_client.close()


# ─── pre-egress refusal (403 policy, two distinct reasons) ───────────────────


async def test_refusal_no_eligible_lane_is_pre_egress_403(monkeypatch) -> None:
    monkeypatch.setattr(ingress.routing, "GENERATE_OVERFLOW_ENABLED", True)
    lane_client, seen = await _lane_server(_health(api_key_enabled=True))
    lanes = {"deepseek": _lane("deepseek", str(lane_client.make_url("")).rstrip("/"))}
    direct = _direct_state()
    ing = await _boot(monkeypatch, lanes, {"deepseek": direct})
    try:
        async with _post_constrained(ing) as r:
            assert r.status == 403
            payload = await r.json()
            assert payload["error"]["type"] == "no_eligible_lane"
            assert payload["error"]["eligible_configured"] == 0
            assert r.headers[ingress.CREDENTIAL_MODE_HEADER] == "unknown"
            assert r.headers["x-anthropic-throttle-refusal"] == "no_eligible_lane"
        assert seen["messages"] == 0  # pre-egress: no model call
    finally:
        await ing.close()
        await lane_client.close()


async def test_refusal_exhausted_distinct(monkeypatch) -> None:
    monkeypatch.setattr(ingress.routing, "GENERATE_OVERFLOW_ENABLED", True)
    lane_client, seen = await _lane_server(_health(bearer_usable=False))
    closed = _sub_state(open_=False, credential_capacity_ok=False)
    ing = await _boot_one(monkeypatch, lane_client, state=closed)
    try:
        async with _post_constrained(ing) as r:
            assert r.status == 403
            payload = await r.json()
            assert payload["error"]["type"] == "eligible_lanes_exhausted"
            assert payload["error"]["eligible_configured"] == 1
            assert payload["error"]["eligible_open"] == 0
            assert r.headers["x-anthropic-throttle-refusal"] == "eligible_lanes_exhausted"
        assert seen["messages"] == 0
    finally:
        await ing.close()
        await lane_client.close()


async def test_refusal_counts_only_role_chain_lanes(monkeypatch) -> None:
    """R2 / r1 §3(c): an eligible lane outside the role's chain must NOT flip
    the verdict to exhausted — the operator remedy is chain config, not patience."""
    monkeypatch.setattr(ingress.routing, "GENERATE_OVERFLOW_ENABLED", False)
    sub_client, _ = await _lane_server(_health())
    lanes = {"anthropic": _lane("anthropic", str(sub_client.make_url("")).rstrip("/"))}
    sub = _sub_state(open_=True)
    ing = await _boot(monkeypatch, lanes, {"anthropic": sub})
    try:
        async with ing.post(
            "/v1/messages",
            json={"model": "claude-opus-5"},
            headers={
                REQ: "subscription",
                "x-anthropic-throttle-role-hint": "bulk",
            },
        ) as r:
            assert r.status == 403
            payload = await r.json()
            assert payload["error"]["type"] == "no_eligible_lane"
            assert payload["error"]["eligible_configured"] == 0
    finally:
        await ing.close()
        await sub_client.close()


async def test_constrained_never_spills_to_direct_key_lane(monkeypatch) -> None:
    """PO fixture (d): healthy direct-key lane with detail=ok stays ineligible."""
    monkeypatch.setattr(ingress.routing, "GENERATE_OVERFLOW_ENABLED", True)
    direct_client, direct_seen = await _lane_server(_health(api_key_enabled=True))
    sub_client, _ = await _lane_server(_health())
    lanes = {
        "anthropic": _lane("anthropic", str(sub_client.make_url("")).rstrip("/")),
        "deepseek": _lane("deepseek", str(direct_client.make_url("")).rstrip("/")),
    }
    sub = _sub_state(open_=False)  # eligible but closed
    direct = _direct_state()
    ing = await _boot(monkeypatch, lanes, {"anthropic": sub, "deepseek": direct})
    try:
        async with _post_constrained(ing) as r:
            assert r.status == 403
        assert direct_seen["messages"] == 0  # direct trap stays zero
    finally:
        await ing.close()
        await direct_client.close()
        await sub_client.close()


# ─── per-request freshness (ADR-6a §1.3, R3) ─────────────────────────────────


async def test_constrained_selection_reprobes_lane_health(monkeypatch) -> None:
    """The decision must be attributable to the request: constrained selection
    re-probes the candidate's health (E3) rather than trusting a stale row."""
    stale = _sub_state(open_=True)
    health = _health(bearer_usable=False)
    lane_client, seen = await _lane_server(health)
    lanes = {"anthropic": _lane("anthropic", str(lane_client.make_url("")).rstrip("/"))}
    ing = await _boot(monkeypatch, lanes, {"anthropic": stale})
    try:
        async with _post_constrained(ing) as r:
            assert r.status == 403
            assert (await r.json())["error"]["type"] == "eligible_lanes_exhausted"
        assert seen["health"] >= 1  # fresh probe happened
        assert seen["messages"] == 0
    finally:
        await ing.close()
        await lane_client.close()


async def test_stale_cached_evidence_marks_refusal_stale(monkeypatch) -> None:
    """R3: a dead poll task leaves stale lane evidence; a constrained refusal
    must surface staleness so the operator checks the poll, not just waits."""
    monkeypatch.setattr(ingress.routing, "GENERATE_OVERFLOW_ENABLED", True)
    lane_client, seen = await _lane_server(_health())
    ancient = _sub_state(open_=False, credential_capacity_ok=False)
    ancient.checked_at = time.time() - 10_000
    ing = await _boot_one(monkeypatch, lane_client, state=ancient)
    try:
        async with _post_constrained(ing) as r:
            assert r.status == 403
            payload = await r.json()
            assert payload["error"]["type"] == "no_eligible_lane"
            assert payload["error"].get("credential_evidence_stale") is True
        assert seen["messages"] == 0
    finally:
        await ing.close()
        await lane_client.close()


async def test_stale_capacity_never_stamps_subscription(monkeypatch) -> None:
    """R4 (r1 fixture response-stale-capacity): CLASS passes but E3 is stale —
    a constrained request must refuse or be unknown, never stamp subscription."""
    stale_at = time.time() - 10_000
    lane_client, seen = await _lane_server(_health(unified_at=stale_at))
    # Cached state says CLASS subscription AND capacity ok — but the fresh
    # probe's unified_at is stale, so capacity must fail.
    cached = _sub_state(open_=True, credential_capacity_ok=True)
    ing = await _boot_one(monkeypatch, lane_client, state=cached)
    try:
        async with _post_constrained(ing) as r:
            assert r.status == 403
            assert r.headers[MODE] == "unknown"
            assert MODE != "subscription"
        assert seen["messages"] == 0
    finally:
        await ing.close()
        await lane_client.close()


# ─── unconstrained compatibility ─────────────────────────────────────────────


async def test_unconstrained_legacy_behavior_unchanged(monkeypatch) -> None:
    lane_client, seen = await _lane_server(_health())
    ing = await _boot_one(monkeypatch, lane_client)
    try:
        async with ing.post("/v1/messages", json={"model": "claude-opus-5"}) as r:
            assert r.status == 200
            assert r.headers[ingress.LANE_HEADER] == "anthropic"
            assert MODE not in r.headers
        assert seen["messages"] == 1
        # No requirement header => direct-key lane still serves unconstrained.
        direct_client, _ = await _lane_server(_health(api_key_enabled=True))
        lanes2 = {"deepseek": _lane("deepseek", str(direct_client.make_url("")).rstrip("/"))}
        direct = _direct_state()
        ing2 = await _boot(monkeypatch, lanes2, {"deepseek": direct})
        try:
            async with ing2.post("/v1/messages", json={"model": "claude-sonnet-4-6"}) as r:
                assert r.status == 200
                assert MODE not in r.headers
        finally:
            await ing2.close()
            await direct_client.close()
    finally:
        await ing.close()
        await lane_client.close()
