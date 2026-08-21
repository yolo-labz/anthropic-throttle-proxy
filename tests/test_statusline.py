"""Contract tests for ``GET /__throttle/statusline`` (spec 205).

The endpoint is a bounded projection of ``/__throttle/health`` for a terminal
statusline redrawing at render cadence, so the properties worth pinning are the
ones a consumer would silently lose: the exact key set it parses, the byte
ceiling that keeps it pollable, the live-window rule that stops a dead reading
being rendered as free capacity, and the queued-vs-throttled split that decides
whether a human should wait or switch accounts.

The app under test registers the real routes in the real order, catch-all
included, so "never forwarded upstream" is a property of the wiring rather than
of the assertion.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import string
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import accounts, config, history, limiter, pacing, proxy

# Every scalar leaf the normative shape promises, dotted. Written out rather
# than derived from the response so a field that silently disappears (or a new
# one that silently appears) fails here instead of at the renderer.
_NORMATIVE_LEAVES = frozenset(
    {
        "schema",
        "now",
        "state",
        "state_since_s",
        "account.label",
        "account.bearer",
        "account.window",
        "account.util",
        "account.status",
        "account.reset",
        "account.stale",
        "queue.depth",
        "queue.inflight",
        "queue.cap",
        "blocked_until",
        "fleet.usable",
        "fleet.configured",
        "queue_mode",
    }
)

# `/__throttle/health`'s top-level key set, frozen. FR-013: this slice adds a
# surface, it does not reshape the existing one — `claude-account-pick`, the
# Dokku healthcheck and `/ui` all read this document.
_HEALTH_TOP_LEVEL = frozenset(
    {
        "build",
        "version",
        "inflight",
        "queued",
        "served",
        "keepalive_holds_active",
        "client_disconnects",
        "upstream_retries",
        "max_concurrent",
        "queue_mode",
        "min_dispatch_gap_ms",
        "upstream",
        "upstream_egress_ok",
        "upstream_egress_error",
        "upstream_egress_last_check",
        "upstream_auth_ok",
        "upstream_auth_error",
        "upstream_auth_last_check",
        "central_url",
        "central_status",
        "account_identity",
        "brake",
        "api_key",
        "central_last_check",
        "last_advisor",
        "bearers",
    }
)

HOUR = 3600.0


def _leaves(obj: object, prefix: str = "") -> set[str]:
    """Dotted paths of every non-container value, including ``false``/``null``.

    Mirrors SC-001's jq. The obvious ``paths(scalars)`` spelling drops both, so
    a payload missing ``account.stale`` or ``blocked_until`` would pass.
    """
    if not isinstance(obj, dict):
        return {prefix}
    return {leaf for k, v in obj.items() for leaf in _leaves(v, f"{prefix}.{k}" if prefix else k)}


def _bearer_id_for(token: str) -> str:
    return hashlib.sha256(f"Bearer {token}".encode()).hexdigest()[:8]


def _unified(
    *,
    util_5h: float | None = None,
    status_5h: str | None = None,
    reset_5h: float | None = None,
    util_7d: float | None = None,
    status_7d: str | None = None,
    reset_7d: float | None = None,
    claim: str | None = None,
) -> dict[str, object]:
    """One parsed unified block, shaped exactly as ``_parse_unified`` emits it."""
    return {
        "representative_claim": claim,
        "util_5h": util_5h,
        "status_5h": status_5h,
        "reset_5h": None if reset_5h is None else int(reset_5h),
        "util_7d": util_7d,
        "status_7d": status_7d,
        "reset_7d": None if reset_7d is None else int(reset_7d),
    }


def _seed_bearer(bid: str, unified: dict[str, object] | None = None, clients: int = 0) -> None:
    """Register a bearer in the process registries as a served request would.

    ``clients`` inflates the per-client topology map that makes health 72 KB —
    the collection this endpoint must stay independent of.
    """
    config.bearer_state[bid] = {
        "inflight": 0,
        "queued": 0,
        "served": 0,
        "clients": {
            f"127.0.0.1:{40000 + i}": {"queued": 0, "inflight": 0, "served": 0}
            for i in range(clients)
        },
        "unified": unified,
        "unified_at": 1_786_933_241.0,
    }


def _seed_limiter(bid: str, *, max_concurrent: int = 5) -> limiter.FairBearerLimiter:
    lim = limiter.FairBearerLimiter(max_concurrent, "fair", bid)
    config.bearer_limiters[bid] = lim
    return lim


def _configure_accounts(tmp_path, monkeypatch, *tokens: str) -> list[str]:
    """Write one credential file per token, point the router at them, return bearer ids.

    Labels are ``A``, ``B``, … in argument order, matching how the env spec is
    written by hand.
    """
    spec, bids = [], []
    for label, token in zip(string.ascii_uppercase, tokens, strict=False):
        cred = tmp_path / f"{label}.json"
        cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
        spec.append(f"{label}:{cred}")
        bids.append(_bearer_id_for(token))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", ",".join(spec))
    return bids


def _quarantine(bid: str, *, status: int, reason: str) -> None:
    """Mark ``bid``'s credential refused, as ``_quarantine_bearer`` would."""
    config.bearer_state[bid]["credential"] = {"ok": False, "status": status, "reason": reason}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Clear every process-global the endpoint reads, before and after."""
    for reset in (
        config.bearer_state.clear,
        config.bearer_limiters.clear,
        accounts._cache.clear,
        history.reset,
    ):
        reset()
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "")
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    monkeypatch.setattr(config, "MAX_CONCURRENT", 5)
    config.state["upstream_egress_ok"] = True
    config.state["served"] = 0
    yield
    config.bearer_state.clear()
    config.bearer_limiters.clear()
    accounts._cache.clear()
    history.reset()


@pytest.fixture
async def client() -> TestClient:
    """The real routes in the real order, catch-all last."""
    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    app = web.Application()
    app.router.add_get("/", proxy.root_probe)
    app.router.add_get("/__throttle/health", proxy.health)
    app.router.add_get("/__throttle/statusline", proxy.statusline)
    app.router.add_route("*", "/{path:.*}", proxy.handler)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


# ---------------------------------------------------------------------------
# Shape + bound (FR-003, FR-010, FR-014 / SC-001)
# ---------------------------------------------------------------------------


async def test_key_set_is_exactly_the_normative_shape(client: TestClient) -> None:
    wall = time.time()
    _seed_bearer(
        "666a53af",
        _unified(
            util_5h=0.25,
            status_5h="allowed",
            reset_5h=wall + HOUR,
            util_7d=0.68,
            status_7d="allowed",
            reset_7d=wall + 96 * HOUR,
            claim="five_hour",
        ),
    )

    resp = await client.get("/__throttle/statusline")

    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    body = await resp.json()
    assert _leaves(body) == set(_NORMATIVE_LEAVES)
    assert body["schema"] == "statusline/1"
    assert body["queue_mode"] == "fair"
    assert body["account"]["bearer"] == "666a53af"
    assert body["account"]["window"] == "5h"
    assert body["account"]["util"] == 0.25


async def test_payload_stays_under_1024_bytes_and_ignores_the_clients_map(
    client: TestClient,
) -> None:
    """FR-003: bounded, and O(1) in the collection that makes health 72 KB."""
    wall = time.time()
    for bid in ("b144f62f", "47f0b262", "666a53af"):
        _seed_bearer(
            bid,
            _unified(
                util_5h=0.5312,
                status_5h="allowed",
                reset_5h=wall + HOUR,
                util_7d=0.8912,
                status_7d="allowed_warning",
                reset_7d=wall + 96 * HOUR,
                claim="seven_day",
            ),
            clients=4,
        )
        _seed_limiter(bid)

    lean = await (await client.get("/__throttle/statusline")).read()
    assert len(lean) <= 1024

    # 1,000 tracked clients is the live desktop's steady state; health grew
    # ~131 B/min on exactly this map. The projection must not move at all.
    for bid in config.bearer_state:
        _seed_bearer(bid, config.bearer_state[bid]["unified"], clients=1000)
    fat = await (await client.get("/__throttle/statusline")).read()

    assert len(fat) == len(lean)
    assert len(fat) <= 1024


async def test_no_token_or_credential_path_is_ever_published(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    """Invariant #2 / FR-012 — only the 8-hex hash and the short label."""
    # Named for what the test does with it (plant, then prove absent) rather
    # than for what it resembles, so the credential-literal lint has nothing to
    # fire on and this file needs no suppression.
    planted = "sk-ant-oat01-not-a-real-value-1234567890"
    (bid,) = _configure_accounts(tmp_path, monkeypatch, planted)
    _seed_bearer(bid, _unified(util_5h=0.1, status_5h="allowed", claim="five_hour"))

    raw = (await (await client.get("/__throttle/statusline")).read()).decode()

    assert planted not in raw
    # The whole tmp dir, not just the one file: no credential path may leak.
    assert str(tmp_path) not in raw
    assert json.loads(raw)["account"] == {
        "label": "A",
        "bearer": bid,
        "window": "5h",
        "util": 0.1,
        "status": "allowed",
        "reset": None,
        "stale": False,
    }


# ---------------------------------------------------------------------------
# Live-window rule (FR-005, FR-006 / SC-003)
# ---------------------------------------------------------------------------


async def test_a_window_past_its_own_reset_is_dropped_and_flagged_stale(
    client: TestClient,
) -> None:
    """The 31/07/2026 trap: a frozen snapshot outliving the window it describes.

    Live on 16/08/2026 bearer ``47f0b262`` published ``status_5h=allowed
    util_5h=0.0`` from a snapshot 622 minutes old whose ``reset_5h`` had passed
    324 minutes earlier. Rendered raw, a dead window reads as free capacity.
    """
    wall = time.time()
    _seed_bearer(
        "47f0b262",
        _unified(
            util_5h=0.0,
            status_5h="allowed",
            reset_5h=wall - 300 * 60,  # rolled five hours ago, never re-probed
            util_7d=0.72,
            status_7d="allowed",
            reset_7d=wall + 96 * HOUR,
            claim="five_hour",  # the RAW snapshot binds on the dead window
        ),
    )

    body = await (await client.get("/__throttle/statusline")).json()

    # The dead 5h reading is gone; the surviving 7d window is what binds.
    assert body["account"]["window"] == "7d"
    assert body["account"]["util"] == 0.72
    assert body["account"]["stale"] is True
    # SC-003's invariant: a non-null reset is always in the future.
    assert body["account"]["reset"] > body["now"]


async def test_every_window_dead_reports_nulls_rather_than_capacity(
    client: TestClient,
) -> None:
    wall = time.time()
    _seed_bearer(
        "47f0b262",
        _unified(
            util_5h=1.0,
            status_5h="rejected",
            reset_5h=wall - HOUR,
            util_7d=1.0,
            status_7d="rejected",
            reset_7d=wall - HOUR,
            claim="seven_day",
        ),
    )

    body = await (await client.get("/__throttle/statusline")).json()

    assert body["account"]["window"] is None
    assert body["account"]["util"] is None
    assert body["account"]["status"] is None
    assert body["account"]["reset"] is None
    assert body["account"]["stale"] is True
    # A stale `rejected` is not a live throttle — it is an unconfirmed reading.
    assert body["state"] != "throttled"


async def test_a_live_rejected_window_is_not_stale(client: TestClient) -> None:
    """The mirror case: `stale` must not become "anything I dislike"."""
    wall = time.time()
    _seed_bearer(
        "47f0b262",
        _unified(
            util_5h=0.0,
            status_5h="allowed",
            reset_5h=wall + HOUR,
            util_7d=1.0,
            status_7d="rejected",
            reset_7d=wall + 48 * HOUR,
            claim="seven_day",
        ),
    )

    body = await (await client.get("/__throttle/statusline")).json()

    assert body["account"]["window"] == "7d"
    assert body["account"]["status"] == "rejected"
    assert body["account"]["stale"] is False
    assert body["state"] == "throttled"


# ---------------------------------------------------------------------------
# State split (FR-008 / User Story 2)
# ---------------------------------------------------------------------------


async def test_queued_and_throttled_are_independent_states(client: TestClient) -> None:
    """Wait vs. switch accounts — the two answers must not collapse into one."""
    wall = time.time()
    _seed_bearer(
        "666a53af",
        _unified(util_5h=0.26, status_5h="allowed", reset_5h=wall + HOUR, claim="five_hour"),
    )
    lim = _seed_limiter("666a53af", max_concurrent=1)

    # Fill the only slot, then park a second caller behind it.
    await lim.acquire("c1")
    parked = asyncio.create_task(lim.acquire("c2"))
    await asyncio.sleep(0)

    body = await (await client.get("/__throttle/statusline")).json()
    assert body["state"] == "queued"
    assert body["queue"] == {"depth": 1, "inflight": 1, "cap": 1}
    assert body["blocked_until"] is None

    # Same bearer, same queue — now upstream hard-pauses it.
    lim.note_retry_after(120)
    throttled = await (await client.get("/__throttle/statusline")).json()
    assert throttled["state"] == "throttled"
    assert throttled["queue"]["depth"] == 1  # the queue did not move
    assert throttled["blocked_until"] > throttled["now"]

    parked.cancel()
    await asyncio.gather(parked, return_exceptions=True)


async def test_warn_sits_between_ok_and_throttled(client: TestClient) -> None:
    wall = time.time()
    _seed_bearer(
        "b144f62f",
        _unified(
            util_5h=0.24,
            status_5h="allowed",
            reset_5h=wall + HOUR,
            util_7d=0.89,
            status_7d="allowed_warning",
            reset_7d=wall + 96 * HOUR,
            claim="seven_day",
        ),
    )

    body = await (await client.get("/__throttle/statusline")).json()

    assert body["state"] == "warn"
    assert body["blocked_until"] is None


async def test_egress_failure_is_state_down_on_a_200(client: TestClient) -> None:
    """FR-009: a shell renderer must never be pushed into an unparsable branch."""
    _seed_bearer("666a53af", _unified(util_5h=0.1, status_5h="allowed", claim="five_hour"))
    config.state["upstream_egress_ok"] = False

    resp = await client.get("/__throttle/statusline")

    assert resp.status == 200  # health answers 503 here; this one must not
    assert (await resp.json())["state"] == "down"


async def test_every_configured_account_unusable_reads_exhausted(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    wall = time.time()
    for bid in _configure_accounts(tmp_path, monkeypatch, "token-a", "token-b"):
        _seed_bearer(
            bid,
            _unified(
                util_7d=1.0,
                status_7d="rejected",
                reset_7d=wall + 48 * HOUR,
                claim="seven_day",
            ),
        )

    body = await (await client.get("/__throttle/statusline")).json()

    assert body["state"] == "exhausted"
    assert body["fleet"] == {"usable": 0, "configured": 2}


async def test_fresh_process_with_no_accounts_is_ok_not_exhausted(client: TestClient) -> None:
    """`0/0` is "nothing configured here", not "every account is dead"."""
    body = await (await client.get("/__throttle/statusline")).json()

    assert body["fleet"] == {"usable": 0, "configured": 0}
    assert body["state"] == "ok"
    assert body["account"]["bearer"] is None


# ---------------------------------------------------------------------------
# Election + wiring (FR-002, FR-004, edge cases)
# ---------------------------------------------------------------------------


async def test_state_resolution_is_most_severe_first() -> None:
    """FR-008 is an ORDER, not a set — and the wrong order is invisible live.

    Every precondition below holds at once, so the only thing under test is
    which one wins. Peeled one at a time the answer walks the enum downward. A
    resolver that checked `ok` first would answer `ok` at every step, and no
    happy-path render — nor `verify.sh` against a healthy proxy, which can only
    observe whichever state the instance happens to be in — would ever notice.
    """
    kwargs: dict[str, object] = {
        "account": {"status": "rejected", "util": 0.99},
        "depth": 7,
        "blocked_until": int(time.time() + 120),
        "usable": 0,
        "configured": 2,
    }

    config.state["upstream_egress_ok"] = False
    assert proxy._statusline_state(**kwargs) == "down"

    config.state["upstream_egress_ok"] = True
    assert proxy._statusline_state(**kwargs) == "exhausted"

    kwargs["usable"] = 1
    assert proxy._statusline_state(**kwargs) == "throttled"

    kwargs["blocked_until"] = None
    kwargs["account"] = {"status": "allowed_warning", "util": 0.99}
    assert proxy._statusline_state(**kwargs) == "queued"

    kwargs["depth"] = 0
    assert proxy._statusline_state(**kwargs) == "warn"

    kwargs["account"] = {"status": "allowed", "util": 0.12}
    assert proxy._statusline_state(**kwargs) == "ok"


@pytest.mark.parametrize(
    "unelectable",
    [
        pytest.param(["_anon"], id="anon-bypass-slot"),
        pytest.param(["api-key"], id="proxy-owned-key-lane"),
        pytest.param(["_anon", "api-key"], id="both-pseudo-bearers"),
        pytest.param(["deadbeef"], id="quarantined-credential"),
    ],
)
async def test_a_bearer_with_no_windows_is_never_elected(
    client: TestClient, unelectable: list[str]
) -> None:
    """One property, four ways to trip it: no windows must not read as idle capacity.

    `_anon` carries health/metrics traffic, `api-key` the proxy-owned key lane,
    and a quarantined credential carries neither unified gauges nor a
    Retry-After. All three therefore look like a zero-utilization account — the
    CHEAPEST candidate in any ranking. Electing the quarantined one is what
    spent 40 client turns on 403s (`_account_routing_candidate_score`,
    proxy.py:1265-1270); electing a bypass slot would render a statusline for a
    credential that serves nothing.
    """
    wall = time.time()
    for bid in unelectable:
        _seed_bearer(bid)
    if "deadbeef" in unelectable:
        _quarantine("deadbeef", status=403, reason="org_policy")
    _seed_bearer(
        "666a53af",
        _unified(util_5h=0.83, status_5h="allowed", reset_5h=wall + HOUR, claim="five_hour"),
    )

    body = await (await client.get("/__throttle/statusline")).json()

    assert body["account"]["bearer"] == "666a53af"
    assert body["account"]["util"] == 0.83


async def test_a_quarantined_account_is_not_counted_usable(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    """Same trap on the CONFIGURED path: `fleet.usable` is the escape-hatch count.

    If a dead account still counted, `state` would read `ok`/`warn` while the
    only live credential was the one already being throttled.
    """
    wall = time.time()
    dead, live = _configure_accounts(tmp_path, monkeypatch, "token-a", "token-b")
    _seed_bearer(dead)
    _quarantine(dead, status=401, reason="revoked")
    _seed_bearer(
        live,
        _unified(util_5h=0.44, status_5h="allowed", reset_5h=wall + HOUR, claim="five_hour"),
    )

    body = await (await client.get("/__throttle/statusline")).json()

    assert body["fleet"] == {"usable": 1, "configured": 2}
    assert body["account"] == {
        "label": "B",
        "bearer": live,
        "window": "5h",
        "util": 0.44,
        "status": "allowed",
        "reset": int(wall + HOUR),
        "stale": False,
    }
    assert body["state"] == "ok"


async def test_the_probe_consumes_no_slot_and_never_reaches_the_catch_all(
    client: TestClient,
) -> None:
    """FR-002 — an infrastructure probe, in the same class as root/health."""
    _seed_bearer("666a53af", _unified(util_5h=0.1, status_5h="allowed", claim="five_hour"))
    served_before = config.state["served"]

    await client.get("/__throttle/statusline")

    assert config.state["served"] == served_before
    assert config.state["inflight"] == 0
    # The catch-all would have allocated one; the dedicated route did not.
    assert "_anon" not in config.bearer_limiters


async def test_election_does_not_mutate_retry_probation(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    """A read-only projection must not seed probation the way routing may."""
    (bid,) = _configure_accounts(tmp_path, monkeypatch, "token-a")

    await client.get("/__throttle/statusline")

    assert limiter.retry_probe_required(bid) is False


async def test_model_query_is_accepted(client: TestClient) -> None:
    """FR-007 — the scoped-meter hook is wired, and absent scoping is harmless."""
    wall = time.time()
    _seed_bearer(
        "666a53af",
        _unified(util_5h=0.2, status_5h="allowed", reset_5h=wall + HOUR, claim="five_hour"),
    )

    body = await (await client.get("/__throttle/statusline?model=claude-opus-5")).json()

    assert body["account"]["bearer"] == "666a53af"
    assert body["state"] == "ok"


async def test_health_keeps_its_own_schema(client: TestClient) -> None:
    """FR-013 — the projection is additive; health is not reshaped to feed it.

    Including the collections the statusline deliberately drops: moving one out
    of health to make the projection cheaper would break `claude-account-pick`
    and `/ui`, which read exactly these.
    """
    wall = time.time()
    _seed_bearer(
        "666a53af",
        _unified(util_5h=0.25, status_5h="allowed", reset_5h=wall + HOUR, claim="five_hour"),
        clients=3,
    )
    _seed_limiter("666a53af")

    resp = await client.get("/__throttle/health")

    assert resp.status == 200
    body = await resp.json()
    assert set(body) == set(_HEALTH_TOP_LEVEL)
    bearer = body["bearers"]["666a53af"]
    assert set(bearer) >= {"clients", "unified", "unified_at", "limiter"}
    assert len(bearer["clients"]) == 3
    assert "queued_per_client" in bearer["limiter"]


# ---------------------------------------------------------------------------
# state_since_s (User Story 2, scenario 3)
# ---------------------------------------------------------------------------


def test_state_since_holds_the_transition_and_leaves_the_dashboard_alone() -> None:
    """Two vocabularies, one process: neither may reset the other's duration."""
    assert history.level_since("throttled", now=100.0, track="statusline") == 0.0
    # The dashboard strip keeps its own clock across statusline polls at render
    # cadence — otherwise "THROTTLED for 12m" resets three times a second.
    assert history.level_since("pacing", now=100.0) == 0.0
    for tick in (100.3, 100.6, 100.9):
        history.level_since("queued", now=tick, track="statusline")
    assert history.level_since("pacing", now=820.0) == 720.0
    # And the statusline's own clock runs from ITS transition, not its last poll.
    assert history.level_since("queued", now=820.0, track="statusline") == 719.7
