"""Executable seat-capacity acceptance for pooled static-key seats (spec 284).

Drives the REAL handler (account routing -> fair queue -> leases -> dispatch)
against a local recording upstream. The acceptance matrix from
``specs/284-capacity-acceptance/assignment.md``:

  1. static-key pool retirement A->B while B is busy      -> test ..._retirement_...
  2. concurrent requests spanning two eligible accounts   -> test ..._span_two_...
  3. one exhausted account not consuming useful slots     -> test ..._exhausted_...
  4. differing caps and durations                         -> test ..._caps_and_durations_...
  5. queued cancellation returning leases                 -> test ..._queued_cancellation_...
  6. measured per-client fair access without exceeding caps -> test ..._per_client_fair_...
  7. shrunk/cooldown/meter-full A excluded while B serves  -> test ..._attracts_no_load/
     ..._live_ceiling_.../..._both_busy_fair_.../..._full_meter_.../..._leakage_...

Determinism rules used here (no real providers, no real credentials):

* dynamic ``tp-<uuid>`` static keys in ``tmp_path`` — the PR #274 pool shape;
* per-tag arrival + hold events at the fake upstream, so overlap and dispatch
  ORDER are proven, not sampled with sleeps;
* cold-start probation is cleared per seat (``try_begin_retry_probe`` +
  ``finish_retry_probe(success=True)``) — a separate tested mechanism
  (``test_expiry_burst_makes_exactly_one_half_open_b_attempt``), deliberately
  out of scope here;
* where two idle seats tie, the request carries the winning seat's own token
  (router stickiness); where LOAD balancing is the claim, ties are strict;
* every wait is bounded and fails with the observed upstream order attached.

Assertions are on actual successful completions (HTTP 200 + a response body
naming the seat that served it), upstream per-seat accounting, and the limiter
books (``inflight`` / ``queued_total`` / ``queued_per_client`` / ``_holds``).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_proxy_app import _reset_proxy_state, _wait_for_limiter_queued

from anthropic_throttle_proxy import accounts, config, limiter, pacing, proxy

_BODY = {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "acceptance"}]}


class _SeatUpstream:
    """Recording fake upstream: per-seat accounting + per-tag arrival/hold gating.

    ``X-Test-Tag`` names the request so tests can await its arrival and release
    it individually; ``X-Test-Hold: 1`` parks the response on that release.
    Every response body names the seat (matched by the upstream Authorization
    header) and the tag, so a completion proves WHICH seat served it.
    """

    def __init__(self, seats: dict[str, str]) -> None:
        self._seats = seats
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self.count: dict[str, int] = defaultdict(int)
        self.inflight: dict[str, int] = defaultdict(int)
        self.max_inflight: dict[str, int] = defaultdict(int)
        self.order: list[tuple[str, str]] = []
        self.arrived: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}

    def app(self) -> web.Application:
        application = web.Application()
        application.router.add_post("/v1/messages", self._messages)
        application.router.add_post("/v1/chat/completions", self._messages)
        return application

    async def _messages(self, request: web.Request) -> web.Response:
        await request.read()
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        seat = self._seats.get(token, "?")
        tag = request.headers.get("X-Test-Tag", "")
        hold = request.headers.get("X-Test-Hold", "") not in ("", "0", "no")
        async with self._lock:
            self.count[seat] += 1
            self.inflight[seat] += 1
            self.max_inflight[seat] = max(self.max_inflight[seat], self.inflight[seat])
            self.order.append((seat, tag))
            self._changed.set()
            if tag:
                self.arrived.setdefault(tag, asyncio.Event()).set()
        try:
            if hold:
                await self.release.setdefault(tag, asyncio.Event()).wait()
            return web.json_response(
                {
                    "id": f"msg-{seat}-{tag}",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "seat": seat,
                    "tag": tag,
                    "content": [{"type": "text", "text": f"{seat}:{tag}"}],
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                }
            )
        finally:
            async with self._lock:
                self.inflight[seat] -= 1

    async def wait_arrived(self, tag: str) -> None:
        """Wait until ``tag`` reached the upstream; fail loudly with the order."""
        try:
            await asyncio.wait_for(self.arrived.setdefault(tag, asyncio.Event()).wait(), 2)
        except TimeoutError:
            raise AssertionError(
                f"request {tag!r} never reached the upstream (order={self.order})"
            ) from None

    async def next_arrival(self, seen: int) -> tuple[str, str]:
        """Wait for dispatch number ``seen + 1``; return its ``(seat, tag)``."""
        try:
            await asyncio.wait_for(self._has_order(seen + 1), 2)
        except TimeoutError:
            raise AssertionError(
                f"dispatch #{seen + 1} never reached the upstream (order={self.order})"
            ) from None
        return self.order[seen]

    async def _has_order(self, n: int) -> None:
        while len(self.order) < n:
            self._changed.clear()
            await self._changed.wait()

    def let_go(self, tag: str) -> None:
        self.release.setdefault(tag, asyncio.Event()).set()


async def _settle() -> None:
    """Let the handler's post-response bookkeeping run before reading state."""
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)


async def _open_seat(
    harness: SimpleNamespace, label: str, *, live: int
) -> limiter.FairBearerLimiter:
    """Allocate ``label``'s limiter, clear cold-start probation, pin the live cap.

    Clearing probation here keeps the half-open probe machinery (its own tested
    mechanism) out of these capacity assertions: a probe-inflight seat is
    unroutable, which would conflate "busy" with "gated".
    """
    bid = harness.bids[label]
    got = await proxy._get_bearer_limiter(bid, "fair", config.MAX_CONCURRENT)
    if got.retry_probe_required():
        assert got.try_begin_retry_probe() is True
        assert got.finish_retry_probe(success=True) is True
    got.max_concurrent = live
    return got


async def _post(
    client: TestClient,
    *,
    tag: str,
    hold: bool = False,
    token: str | None = None,
    cid: str | None = None,
    path: str = "/v1/messages",
) -> tuple[int, bytes]:
    """One /v1/messages round trip; the upstream tag names the response body."""
    headers = {
        "Content-Type": "application/json",
        # Unique anonymous client credential per call when no seat token is
        # given: distinct incoming bearers, all rewritten by the pool router.
        "Authorization": f"Bearer {token or f'client-{tag}-{uuid.uuid4().hex}'}",
        "X-Test-Tag": tag,
    }
    if hold:
        headers["X-Test-Hold"] = "1"
    if cid:
        headers["X-Throttle-Client-Id"] = cid
    response = await asyncio.wait_for(client.post(path, headers=headers, json=_BODY), 5)
    payload = await asyncio.wait_for(response.read(), 5)
    return response.status, payload


def _seat_of(payload: bytes) -> str:
    return str(json.loads(payload)["seat"])


@pytest.fixture
async def pool(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Two static-key seats (A, B) + the real proxy app + a recording upstream.

    Mirrors ``tests/test_proxy_app.py``'s ``client`` fixture (locks bound on the
    running loop, ``UPSTREAM`` pointed at the stub) with the PR #274 account
    pool wired through real credential files.
    """
    tokens = {"A": "tp-" + uuid.uuid4().hex, "B": "tp-" + uuid.uuid4().hex}
    key_files = {}
    for label, token in tokens.items():
        path = tmp_path / f"seat-{label.lower()}.key"
        path.write_text(token + "\n", encoding="utf-8")
        key_files[label] = path

    _reset_proxy_state()
    accounts._cache.clear()
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{key_files['A']},B:{key_files['B']}")
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(config, "API_KEY_FILE", "")
    monkeypatch.setattr(config, "API_KEY_ROUTING_MODE", "off")
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    monkeypatch.setattr(config, "MAX_CONCURRENT", 3)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 3)
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 0)
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", str(tmp_path / "retry-after.json"))
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())

    upstream = _SeatUpstream({token: label for label, token in tokens.items()})
    upstream_server = TestServer(upstream.app())
    await upstream_server.start_server()
    monkeypatch.setattr(config, "UPSTREAM", str(upstream_server.make_url("")).rstrip("/"))

    app = web.Application()
    app.router.add_get("/", proxy.root_probe)
    app.router.add_get("/__throttle/health", proxy.health)
    app.router.add_route("*", "/{path:.*}", proxy.handler)
    client = TestClient(TestServer(app))
    await client.start_server()

    bids = {
        label: proxy._bearer_id({"Authorization": f"Bearer {token}"})
        for label, token in tokens.items()
    }
    yield SimpleNamespace(
        client=client,
        upstream=upstream,
        tokens=tokens,
        bids=bids,
        key_files=key_files,
    )
    await client.close()
    await upstream_server.close()


# ── 1. static-key pool retirement A→B while B is busy ─────────────────────────


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions"])
async def test_static_key_pool_retirement_hands_a_to_b_while_b_is_busy(pool, path) -> None:
    """Retiring seat A's key mid-flight hands new work to B even while B is busy.

    A must receive nothing after retirement, B's busy request must still
    complete, and the handover must not wait for B to drain (B's cap admits
    both requests concurrently).
    """
    lim_a = await _open_seat(pool, "A", live=2)
    lim_b = await _open_seat(pool, "B", live=2)

    # Warm the pool on both seats: one completion on A proves A was live in it.
    warm = await _post(pool.client, tag="warm-a", token=pool.tokens["A"])
    assert warm[0] == 200
    assert _seat_of(warm[1]) == "A"

    # B goes busy (its own token as incoming -> stickiness resolves the tie).
    busy = asyncio.create_task(_post(pool.client, tag="b-busy", hold=True, token=pool.tokens["B"]))
    await pool.upstream.wait_arrived("b-busy")

    # Retirement: A's credential file leaves the pool.
    pool.key_files["A"].unlink()

    config.bearer_state[pool.bids["A"]].update(
        unified={"status": "allowed", "util_5h": 0.1}, unified_at=time.time()
    )
    handover = asyncio.create_task(
        _post(pool.client, tag="after-retire", hold=True, token=pool.tokens["A"], path=path)
    )
    await pool.upstream.wait_arrived("after-retire")

    # The handover landed on B while B was still busy — concurrent, no drain wait.
    assert dict(pool.upstream.count) == {"A": 1, "B": 2}
    assert dict(pool.upstream.inflight) == {"A": 0, "B": 2}
    assert dict(pool.upstream.max_inflight) == {"A": 1, "B": 2}

    pool.upstream.let_go("b-busy")
    pool.upstream.let_go("after-retire")
    busy_status, busy_body = await asyncio.wait_for(busy, 5)
    handover_status, handover_body = await asyncio.wait_for(handover, 5)
    assert busy_status == handover_status == 200
    assert _seat_of(busy_body) == "B"
    assert _seat_of(handover_body) == "B"

    # Retired A: never dispatched again, and no slot/lease left behind.
    assert [tag for _seat, tag in pool.upstream.order if _seat == "A"] == ["warm-a"]
    await _settle()
    assert lim_a.inflight == 0
    assert lim_a._holds == {}
    assert config.bearer_state[pool.bids["A"]]["served"] == 1  # only the warm-up
    assert config.bearer_state[pool.bids["B"]]["served"] == 2
    assert lim_b._holds == {}


# ── 2. concurrent requests spanning two eligible accounts ─────────────────────


async def test_retired_static_cannot_escape_a_replacement_half_open_probe(pool) -> None:
    await _open_seat(pool, "A", live=2)
    lim = await _open_seat(pool, "B", live=2)
    pool.key_files["A"].unlink()
    lim.require_retry_probe(block_while_retry=True)
    probe = asyncio.create_task(
        _post(pool.client, tag="replacement-probe", hold=True, token=pool.tokens["B"])
    )
    await pool.upstream.wait_arrived("replacement-probe")
    assert lim.retry_probe_inflight()
    arrival = asyncio.create_task(
        _post(pool.client, tag="retired-during-probe", token=pool.tokens["A"])
    )
    await _settle()
    assert not arrival.done(), "retired credential escaped while replacement was probing"
    assert ("A", "retired-during-probe") not in pool.upstream.order
    pool.upstream.let_go("replacement-probe")
    assert (await probe)[0] == 200
    status, body = await arrival
    assert status == 200 and _seat_of(body) == "B"


async def test_concurrent_requests_span_two_eligible_accounts(pool) -> None:
    """Two eligible seats serve two CONCURRENT requests — one each.

    The second request is anonymous (no stickiness), so spanning is the
    least-loaded router's doing. Both requests must be in flight at the
    upstream SIMULTANEOUSLY (arrival before either release), and both must
    complete with the seat their body names.
    """
    await _open_seat(pool, "A", live=1)
    await _open_seat(pool, "B", live=1)

    first = asyncio.create_task(_post(pool.client, tag="one", hold=True, token=pool.tokens["A"]))
    await pool.upstream.wait_arrived("one")

    second = asyncio.create_task(_post(pool.client, tag="two", hold=True))
    await pool.upstream.wait_arrived("two")  # fails if it queued behind seat A

    # Overlap proven: both seats hold one request before either is released.
    assert dict(pool.upstream.inflight) == {"A": 1, "B": 1}
    assert dict(pool.upstream.count) == {"A": 1, "B": 1}

    pool.upstream.let_go("one")
    pool.upstream.let_go("two")
    (first_status, first_body), (second_status, second_body) = await asyncio.wait_for(
        asyncio.gather(first, second), 5
    )
    assert first_status == second_status == 200
    assert (_seat_of(first_body), _seat_of(second_body)) == ("A", "B")


# ── 3. one exhausted/rejected account not consuming useful slots ──────────────


async def test_exhausted_account_consumes_no_useful_slots(pool) -> None:
    """A seat with a live `rejected`/util-1.0 window is routed around entirely.

    Every useful completion must land on the healthy seat; the exhausted one
    must hold no slot, mint no lease, and serve nothing while its frozen
    window stays unexpired (it can never clear its own gate).
    """
    lim_a = await _open_seat(pool, "A", live=3)
    lim_b = await _open_seat(pool, "B", live=3)
    config.bearer_state[pool.bids["A"]]["unified"] = {
        "status_5h": "rejected",
        "util_5h": 1.0,
        "reset_5h": time.time() + 600,
    }

    tasks = [asyncio.create_task(_post(pool.client, tag=f"r{i}", hold=True)) for i in range(3)]
    for i in range(3):
        await pool.upstream.wait_arrived(f"r{i}")

    assert dict(pool.upstream.count) == {"B": 3}
    assert dict(pool.upstream.inflight) == {"B": 3}

    for i in range(3):
        pool.upstream.let_go(f"r{i}")
    outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert [status for status, _body in outcomes] == [200, 200, 200]
    assert {_seat_of(body) for _status, body in outcomes} == {"B"}

    await _settle()
    assert lim_a.inflight == 0
    assert lim_a.queued_total == 0
    assert lim_a._holds == {}
    assert config.bearer_state[pool.bids["A"]]["served"] == 0  # zero useful slots spent
    assert config.bearer_state[pool.bids["B"]]["served"] == 3
    assert lim_b._holds == {}


# ── 4. differing caps and durations ───────────────────────────────────────────


async def test_differing_caps_and_durations_hold_per_seat(pool) -> None:
    """Seats with different live caps and mixed request durations stay inside
    their own ceilings while short work keeps making progress.

    A's cap is 2 and B's is 1. A short request on A completes while two long
    (held) requests run; B's second request QUEUES behind its busy seat
    instead of over-dispatching, then drains on release.
    """
    await _open_seat(pool, "A", live=2)
    lim_b = await _open_seat(pool, "B", live=1)

    a_long = asyncio.create_task(
        _post(pool.client, tag="a-long", hold=True, token=pool.tokens["A"])
    )
    await pool.upstream.wait_arrived("a-long")
    b_long = asyncio.create_task(
        _post(pool.client, tag="b-long", hold=True, token=pool.tokens["B"])
    )
    await pool.upstream.wait_arrived("b-long")

    # Differing durations: the short request completes under load, on the
    # roomier seat, while both longs are still held.
    short_status, short_body = await _post(pool.client, tag="a-short", token=pool.tokens["A"])
    assert short_status == 200
    assert _seat_of(short_body) == "A"
    assert not a_long.done() and not b_long.done()

    # Fill A's remaining slot too: a load-aware router must use available A
    # capacity rather than queue behind B just because B's token arrived.
    a_second = asyncio.create_task(
        _post(pool.client, tag="a-second", hold=True, token=pool.tokens["B"])
    )
    await pool.upstream.wait_arrived("a-second")
    assert pool.upstream.order[-1] == ("A", "a-second")
    # Both seats are full. B's own token breaks the equal normalized-load tie.
    b_wait = asyncio.create_task(
        _post(pool.client, tag="b-wait", hold=True, token=pool.tokens["B"])
    )
    await _wait_for_limiter_queued(lim_b, 1)
    assert lim_b.snapshot()["queued_total"] == 1
    assert "b-wait" not in pool.upstream.arrived  # never dispatched while B busy

    pool.upstream.let_go("b-long")
    await pool.upstream.wait_arrived("b-wait")
    pool.upstream.let_go("b-wait")
    wait_status, wait_body = await asyncio.wait_for(b_wait, 5)
    assert wait_status == 200
    assert _seat_of(wait_body) == "B"

    pool.upstream.let_go("a-long")
    pool.upstream.let_go("a-second")
    second_status, second_body = await asyncio.wait_for(a_second, 5)
    assert second_status == 200 and _seat_of(second_body) == "A"
    long_status, long_body = await asyncio.wait_for(a_long, 5)
    b_status, b_body = await asyncio.wait_for(b_long, 5)
    assert long_status == b_status == 200
    assert _seat_of(long_body) == "A"
    assert _seat_of(b_body) == "B"

    # Caps held: A never ran more than 2 at once, B never more than 1.
    assert dict(pool.upstream.max_inflight) == {"A": 2, "B": 1}
    await _settle()
    assert all(
        snapshot["inflight"] == 0 and snapshot["queued_total"] == 0
        for snapshot in (
            (await proxy._get_bearer_limiter(pool.bids["A"], "fair", 3)).snapshot(),
            lim_b.snapshot(),
        )
    )


# ── 5. queued cancellation returning leases ───────────────────────────────────


async def test_queued_cancellation_returns_lease_and_queue_position(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled queued request returns its queue position/lease and never
    spends an upstream slot.

    Single-seat pool: with two idle seats the least-loaded router would spread
    the second request onto the other seat instead of queueing it, and there
    would be no queue entry to return. One seat = one shared fair queue.
    """
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{pool.key_files['A']}")
    lim_a = await _open_seat(pool, "A", live=1)

    holder = asyncio.create_task(_post(pool.client, tag="holder", hold=True, cid="cx"))
    await pool.upstream.wait_arrived("holder")

    cancelled = asyncio.create_task(_post(pool.client, tag="cancelled", cid="cy"))
    await _wait_for_limiter_queued(lim_a, 1)
    assert lim_a.snapshot()["queued_total"] == 1
    # A QUEUED request owns no slot lease — only the holder does.
    assert len(lim_a._holds) == 1

    cancelled.cancel()
    outcome = await asyncio.gather(cancelled, return_exceptions=True)
    assert isinstance(outcome[0], (asyncio.CancelledError, aiohttp.ClientError))

    # The queue entry returns on the cancellation itself, not on a later wake.
    await _wait_for_limiter_queued(lim_a, 0)
    assert lim_a.snapshot()["queued_total"] == 0
    assert lim_a.inflight == 1  # only the holder
    assert len(lim_a._holds) == 1  # and only the holder's lease

    pool.upstream.let_go("holder")
    holder_status, _holder_body = await asyncio.wait_for(holder, 5)
    assert holder_status == 200

    await _settle()
    assert "cancelled" not in pool.upstream.arrived  # no upstream slot wasted
    assert lim_a.inflight == 0
    assert lim_a.queued_total == 0
    assert lim_a._holds == {}  # every lease returned

    # Useful progress continues immediately on the freed seat.
    nxt_status, nxt_body = await _post(pool.client, tag="next")
    assert nxt_status == 200
    assert _seat_of(nxt_body) == "A"


# ── 6. measured per-client fair access without exceeding caps ─────────────────


async def test_per_client_fair_access_measured_within_caps(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chatty client's backlog must not starve a sibling on the shared seat.

    X queues 3 requests behind its own first one; Y queues 1. The measured
    per-client queue state must show both, Y must dispatch after at most one
    sibling turn (never behind X's whole backlog), and the seat's cap of 1
    must never be exceeded.

    Single-seat pool: both clients must land in ONE bearer's fair queue for
    per-client rotation to be the mechanism under test (two idle seats would
    spread them instead).
    """
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{pool.key_files['A']}")
    lim_a = await _open_seat(pool, "A", live=1)
    upstream = pool.upstream

    x1 = asyncio.create_task(_post(pool.client, tag="x1", hold=True, cid="cx"))
    await upstream.wait_arrived("x1")
    x2 = asyncio.create_task(_post(pool.client, tag="x2", hold=True, cid="cx"))
    x3 = asyncio.create_task(_post(pool.client, tag="x3", hold=True, cid="cx"))
    y1 = asyncio.create_task(_post(pool.client, tag="y1", hold=True, cid="cy"))
    await _wait_for_limiter_queued(lim_a, 3)

    # The MEASURED per-client state: X holds two queued, Y one.
    assert lim_a.snapshot()["queued_per_client"] == {"cx": 2, "cy": 1}
    assert dict(upstream.max_inflight) == {"A": 1}

    arrivals = ["x1"]
    upstream.let_go("x1")
    x1_status, _x1_body = await asyncio.wait_for(x1, 5)
    assert x1_status == 200
    for _ in range(3):
        _seat, tag = await upstream.next_arrival(len(arrivals))
        arrivals.append(tag)
        upstream.let_go(tag)

    assert set(arrivals) == {"x1", "x2", "x3", "y1"}
    # Fair access: Y is served within one sibling turn of the freed slot,
    # not after X's whole backlog.
    assert arrivals.index("y1") <= 2
    assert arrivals.index("y1") < arrivals.index("x3")

    outcomes = await asyncio.wait_for(asyncio.gather(x2, x3, y1), 5)
    assert [status for status, _body in outcomes] == [200, 200, 200]

    await _settle()
    assert dict(upstream.max_inflight) == {"A": 1}  # cap 1 never exceeded
    assert lim_a.inflight == 0
    assert lim_a.queued_total == 0
    assert lim_a._holds == {}


# ── 7. shrunk/cooldown/meter-full A exclusion (spec 285 verification) ────────
#
# Coordinator-verified live shape (03/10/2026): slot B serves while slot A is
# AIMD-shrunk to 2 after a 429 and in retry-probe cooldown. These are END-TO-END
# synthetic regressions of the EXCLUSION: while the healthy sibling has room,
# a shrunk, cooldown or meter-exhausted A must attract no load, hold no lease
# and never be counted as usable capacity — with no state leaking between lanes.


async def test_retry_probe_cooldown_seat_attracts_no_load(pool) -> None:
    """A in retry-probe cooldown is unroutable while B has free slots.

    Both of A's gates must hold: the blocking half-open probe window
    (``retry_probe_blocks_routing``) excludes it even for the retry-probe
    routing pass, and its post-shrink live ceiling (2) is preserved untouched.
    """
    lim_a = await _open_seat(pool, "A", live=2)
    await _open_seat(pool, "B", live=3)
    # The 429 aftermath: a pacing window plus a blocking half-open probe gate.
    lim_a.note_retry_after(30)
    limiter.require_retry_probe(pool.bids["A"], block_while_retry=True)

    # The routing pass that ALLOWS retry probes still refuses A.
    assert proxy._bearer_routing_retry_after(pool.bids["A"], allow_retry_probe=True) is None
    assert limiter.retry_probe_blocks_routing(pool.bids["A"]) is True

    tasks = [asyncio.create_task(_post(pool.client, tag=f"c{i}")) for i in range(3)]
    outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert [status for status, _body in outcomes] == [200, 200, 200]
    assert dict(pool.upstream.count) == {"B": 3}  # A absent from every dispatch

    await _settle()
    assert lim_a.inflight == 0 and lim_a.queued_total == 0 and lim_a._holds == {}
    assert lim_a.max_concurrent == 2  # live ceiling preserved, never "fixed up"
    assert config.bearer_state[pool.bids["A"]]["served"] == 0
    assert config.bearer_state[pool.bids["B"]]["served"] == 3


async def test_shrunk_seat_at_live_ceiling_keeps_new_load_off(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saturated at its SHRUNK live ceiling is not a candidate while B has room.

    The least-loaded score normalizes occupancy by the live cap: A at 2/2
    scores 1.0 while B idles at 0/3, so new arrivals must land on B and A must
    never run more than its ceiling — the shrink does not become a queue magnet.
    """
    await _open_seat(pool, "A", live=2)
    await _open_seat(pool, "B", live=3)
    # Fill A deterministically with the suite's single-seat idiom (stickiness is
    # a tie-breaker, not a pin): A-only pool for the fill, then restore.
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{pool.key_files['A']}")
    a1 = asyncio.create_task(_post(pool.client, tag="a1", hold=True))
    await pool.upstream.wait_arrived("a1")
    a2 = asyncio.create_task(_post(pool.client, tag="a2", hold=True))
    await pool.upstream.wait_arrived("a2")
    assert dict(pool.upstream.inflight) == {"A": 2}  # A full at its live cap
    monkeypatch.setattr(
        config, "ACCOUNT_CRED_PATHS", f"A:{pool.key_files['A']},B:{pool.key_files['B']}"
    )

    # Anonymous arrivals must use B's free capacity, not queue onto full A.
    b1 = asyncio.create_task(_post(pool.client, tag="b1", hold=True))
    await pool.upstream.wait_arrived("b1")
    b2 = asyncio.create_task(_post(pool.client, tag="b2", hold=True))
    await pool.upstream.wait_arrived("b2")
    assert dict(pool.upstream.count) == {"A": 2, "B": 2}
    assert dict(pool.upstream.max_inflight) == {"A": 2, "B": 2}  # ceilings held

    for tag in ("a1", "a2", "b1", "b2"):
        pool.upstream.let_go(tag)
    statuses = await asyncio.wait_for(asyncio.gather(a1, a2, b1, b2), 5)
    assert [status for status, _body in statuses] == [200, 200, 200, 200]


async def test_both_seats_busy_fair_queue_per_client(pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both seats occupied: queued work drains by per-client fair rotation.

    A (cap 2) and B (cap 1) are both filled to their live ceilings (single-seat
    fill idiom, so tag placement is deterministic). Queue depth counts as load,
    so the router SPREADS the backlog: X's first parks on A (stable tie), X's
    second on B, Y's on A. Each busy bearer drains its own queue by per-client
    RR — Y is served within one turn, never starved behind X — and no seat
    exceeds its own live ceiling.
    """
    await _open_seat(pool, "A", live=2)
    await _open_seat(pool, "B", live=1)
    upstream = pool.upstream

    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{pool.key_files['A']}")
    a_hold = asyncio.create_task(_post(pool.client, tag="a-hold", hold=True))
    await upstream.wait_arrived("a-hold")
    a_hold2 = asyncio.create_task(_post(pool.client, tag="a-hold2", hold=True))
    await upstream.wait_arrived("a-hold2")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"B:{pool.key_files['B']}")
    b_hold = asyncio.create_task(_post(pool.client, tag="b-hold", hold=True))
    await upstream.wait_arrived("b-hold")
    monkeypatch.setattr(
        config, "ACCOUNT_CRED_PATHS", f"A:{pool.key_files['A']},B:{pool.key_files['B']}"
    )
    assert dict(upstream.inflight) == {"A": 2, "B": 1}  # both occupied

    x2 = asyncio.create_task(_post(pool.client, tag="x2", hold=True, cid="cx"))
    x3 = asyncio.create_task(_post(pool.client, tag="x3", hold=True, cid="cx"))
    y1 = asyncio.create_task(_post(pool.client, tag="y1", hold=True, cid="cy"))
    lim_a = await proxy._get_bearer_limiter(pool.bids["A"], "fair", config.MAX_CONCURRENT)
    lim_b = await proxy._get_bearer_limiter(pool.bids["B"], "fair", config.MAX_CONCURRENT)
    await _wait_for_limiter_queued(lim_a, 2)
    await _wait_for_limiter_queued(lim_b, 1)
    # Deterministic spread: two on A (one per client), one on B; nothing ran.
    assert lim_a.snapshot()["queued_per_client"] == {"cx": 1, "cy": 1}
    assert lim_b.snapshot()["queued_per_client"] == {"cx": 1}
    for tag in ("x2", "x3", "y1"):
        assert tag not in upstream.arrived

    upstream.let_go("a-hold")  # a slot frees on A: its queue drains by RR
    _seat, tag = await upstream.next_arrival(len(upstream.order))
    assert tag == "x2"
    upstream.let_go(tag)
    _seat, tag = await upstream.next_arrival(len(upstream.order))
    assert tag == "y1"  # fair access: Y within one turn, never behind X
    upstream.let_go(tag)

    upstream.let_go("b-hold")  # a slot frees on B: its queue drains too
    _seat, tag = await upstream.next_arrival(len(upstream.order))
    assert tag == "x3"
    upstream.let_go(tag)

    await asyncio.wait_for(asyncio.gather(x2, x3, y1), 5)
    upstream.let_go("a-hold2")
    await asyncio.wait_for(asyncio.gather(a_hold, a_hold2, b_hold), 5)
    await _settle()
    assert dict(upstream.max_inflight) == {"A": 2, "B": 1}  # ceilings never exceeded


async def test_full_meter_seat_is_exhausted_never_usable_capacity(pool) -> None:
    """A meter at 100% is EXHAUSTED whatever the status field claims.

    The routing gate echoes #284's probe rule (used>=total is exhausted; the
    display field is not authoritative): a lagging ``allowed`` sample with
    ``util_5h=1.0`` must hard-gate A — even under the pressure/spillover pass —
    so its "free" slots are never counted as usable capacity while B queues
    and serves the load.
    """
    lim_a = await _open_seat(pool, "A", live=3)
    lim_b = await _open_seat(pool, "B", live=1)
    config.bearer_state[pool.bids["A"]]["unified"] = {
        "status_5h": "allowed",  # lagging display — the meter is authoritative
        "util_5h": 1.0,
        "reset_5h": time.time() + 600,
    }

    holder = asyncio.create_task(_post(pool.client, tag="b-hold", hold=True))
    await pool.upstream.wait_arrived("b-hold")
    assert pool.upstream.order[-1] == ("B", "b-hold")

    # B is full and A LOOKS idle: the arrival must queue on B, never spill to A.
    queued = asyncio.create_task(_post(pool.client, tag="spill", hold=True))
    await _wait_for_limiter_queued(lim_b, 1)
    assert "spill" not in pool.upstream.arrived
    assert lim_a.inflight == 0 and lim_a._holds == {}  # A holds no slot, no lease

    pool.upstream.let_go("b-hold")
    await pool.upstream.wait_arrived("spill")
    pool.upstream.let_go("spill")
    (holder_status, _holder_body), (queued_status, _queued_body) = await asyncio.wait_for(
        asyncio.gather(holder, queued), 5
    )
    assert holder_status == queued_status == 200
    assert dict(pool.upstream.count) == {"B": 2}  # A's capacity never used

    await _settle()
    assert config.bearer_state[pool.bids["A"]]["served"] == 0
    assert config.bearer_state[pool.bids["B"]]["served"] == 2


async def test_no_state_leakage_between_seats(pool) -> None:
    """A's cooldown/gate state must not gate, pause or reopen across to B.

    B serves with no induced pause while A sits behind a 30s window (a leak
    would park this request for the window — the 5s round-trip bound is the
    falsifier), B's own gate state stays untouched, and B's success must NOT
    clear A's gate or window.
    """
    lim_a = await _open_seat(pool, "A", live=2)
    lim_b = await _open_seat(pool, "B", live=3)
    lim_a.note_retry_after(30)
    limiter.require_retry_probe(pool.bids["A"], block_while_retry=True)

    status, body = await asyncio.wait_for(_post(pool.client, tag="b1"), 5)
    assert status == 200 and _seat_of(body) == "B"  # completed inside 5s: no leak
    await _settle()  # let post-response bookkeeping land before reading counters

    # B's state is untouched by A's gate.
    assert lim_b.retry_after_remaining() == 0
    assert limiter.retry_probe_required(pool.bids["B"]) is False
    assert config.bearer_state[pool.bids["B"]]["served"] == 1
    # B's success did not reopen or clear A's gate or window.
    assert limiter.retry_probe_required(pool.bids["A"]) is True
    assert limiter.retry_probe_blocks_routing(pool.bids["A"]) is True
    assert lim_a.retry_after_remaining() > 0
    assert config.bearer_state[pool.bids["A"]]["served"] == 0
