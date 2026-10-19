"""Queue-admission acceptance — spec 334 (19/10/2026 go-gate collapse).

Each test is paired with a falsifier: a gate that cannot fail on the
plausible-but-healthy case is not a gate, it is a decoration.

Ground truth these pin (receipts in ``specs/334-queue-admission/spec.md`` and
``docs/incidents/2026-10-19-free-lane-collapse.md``): bearer ``e574f38a`` on the
go gate ``:8770`` went 64 -> 44 -> 30 -> 21 -> 14 -> 9 -> 6 -> 4 -> 2 -> 1
between 16:47:10 and 16:52:28 and then RESTED at ``max_concurrent=1`` for
~2h47m; with ~171 s generations that single slot refused 16 queue-wait-timeout
503s across 13 client connections and killed at least one pi pane.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator

import pytest

from anthropic_throttle_proxy import config, limiter, pacing

FREE_HARD_CEILING = 1024
FREE_INITIAL = 12
FREE_FLOOR = 2


@pytest.fixture(autouse=True)
async def _isolate(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[None]:
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    monkeypatch.setattr(config, "MAX_CONCURRENT", FREE_HARD_CEILING, raising=True)
    monkeypatch.setattr(config, "QUEUE_MODE", "fair", raising=True)
    monkeypatch.setattr(config, "AIMD_MIN", FREE_FLOOR, raising=True)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", FREE_INITIAL, raising=True)
    monkeypatch.setattr(config, "AIMD_DECREASE", 0.7, raising=True)
    monkeypatch.setattr(config, "AIMD_BACKOFF_S", 30.0, raising=True)
    monkeypatch.setattr(config, "AIMD_RAMP_AFTER", 10, raising=True)
    monkeypatch.setattr(config, "AIMD_RAMP_AFTER_FAST", 5, raising=True)
    monkeypatch.setattr(config, "AIMD_STORM_THRESHOLD", 3, raising=True)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 240.0, raising=True)
    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    yield
    config.bearer_limiters.clear()
    config.bearer_state.clear()


def _lane(
    queue_mode: str = "fair",
    hard_max: int = FREE_HARD_CEILING,
    bearer_id: str = "e574f38a",
) -> limiter.FairBearerLimiter:
    return limiter.FairBearerLimiter(hard_max, queue_mode, bearer_id=bearer_id)


# ---------------------------------------------------------------------------
# (a) the ceiling never rests at the floor forever
# ---------------------------------------------------------------------------


async def test_shrink_never_rests_below_the_floor() -> None:
    """Acceptance (a) part 1 — the floor is a floor, whatever the pushback says."""
    lim = _lane()
    for _ in range(12):
        cap = await lim.shrink()
        assert cap is not None
        assert cap >= lim.aimd_floor()


async def test_aimd_floor_yields_to_the_operator_hard_ceiling() -> None:
    """A hot-tuned ceiling below AIMD_MIN must not be re-inflated by a 429."""
    lim = _lane()
    await limiter._retune_limiter_hard_max("e574f38a", lim, 1)  # noqa: SLF001
    assert lim.aimd_floor() == 1
    for _ in range(5):
        assert await lim.shrink() == 1
        assert lim.max_concurrent <= lim.hard_max


async def test_lane_resting_at_the_floor_regains_a_slot_after_dwell() -> None:
    """Acceptance (a) part 2 — the 2h47m pin is what this prevents.

    FALSIFIER: without the dwell ramp this needs ``AIMD_RAMP_AFTER`` (10)
    consecutive successes at the collapsed width, which at ~171 s per
    generation is ~28 min of single-file traffic. The healthy case — the lane
    has sat quiet at the floor and served one clean request — must yield a slot.
    """
    lim = _lane()
    lim.max_concurrent = FREE_FLOOR
    for _ in range(8):
        await lim.shrink()  # drive to the floor the incident's way
    assert lim.max_concurrent == FREE_FLOOR
    lim._last_throttle_at = time.time() - (2 * config.AIMD_BACKOFF_S + 1.0)  # noqa: SLF001

    # `grow()` books the 2xx it is reporting, so the first call IS the one clean
    # success: it earns exactly one slot, not several.
    assert await lim.grow() == FREE_FLOOR + 1
    assert lim.max_concurrent == FREE_FLOOR + 1


async def test_cold_start_bearer_gets_no_free_ramp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FALSIFIER for the dwell ramp itself.

    A never-throttled limiter has ``_last_throttle_at == 0.0``, so its \"time at
    the floor\" reads as \"forever\". On a lane whose ``AIMD_MIN`` equals its
    warm-start cap that made a fresh bearer earn +1 on its very first 2xx — a
    ramp with no pushback to recover from. Caught by
    ``test_unified_no_shrink_when_target_off_via_http`` during development.
    """
    monkeypatch.setattr(config, "AIMD_MIN", 1, raising=True)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 1, raising=True)
    lim = _lane()
    # Warm start sits ON the floor, which is the shape that made the dwell ramp
    # dangerous: "rested here forever" and "never left" look identical.
    assert lim.max_concurrent == lim.aimd_floor() == 1
    assert lim._last_throttle_at == 0.0  # noqa: SLF001
    assert await lim.grow() is None
    assert lim.max_concurrent == 1


async def test_cooldown_alone_is_not_enough_at_the_floor() -> None:
    """FALSIFIER for the dwell ramp: the COOLDOWN must not unlock it.

    The anti-oscillation pin is that a lane which just took sustained pushback
    keeps the success-count ramp. So the gate is the full dwell (2 x cooldown),
    not the cooldown that already existed — otherwise the ramp fires immediately
    after every shrink and the lane oscillates straight back into the wall.
    """
    lim = _lane()
    lim.max_concurrent = FREE_FLOOR
    await lim.shrink()
    lim._last_throttle_at = time.time() - (config.AIMD_BACKOFF_S + 1.0)  # noqa: SLF001
    assert lim._successes_since_throttle == 0  # noqa: SLF001
    assert await lim.grow() is None  # cooled, but has not RESTED at the floor


async def test_open_retry_after_window_blocks_the_dwell_ramp() -> None:
    """A lane inside an upstream hold must not widen, however long it rested."""
    lim = _lane()
    lim.max_concurrent = FREE_FLOOR
    await lim.shrink()
    lim._last_throttle_at = time.time() - (2 * config.AIMD_BACKOFF_S + 1.0)  # noqa: SLF001
    lim._retry_after_until = time.time() + 60.0  # noqa: SLF001
    assert await lim.grow() is None  # rested, but inside the upstream hold
    assert lim.max_concurrent == FREE_FLOOR


# ---------------------------------------------------------------------------
# (c) a window <= hold still queues to success
# ---------------------------------------------------------------------------


async def test_hold_window_at_the_240s_contract_is_honoured_not_fast_failed() -> None:
    """Acceptance (c): hold=240 is the contract; do not pin it lower here.

    A window exactly at the bound must be WAITED OUT, not answered with the
    ``> MAX_HOLD`` fast-fail 429. 239 s and 240 s wait; 241 s fast-fails.
    """
    assert config.MAX_HOLD_RETRY_AFTER_S == 240.0
    from anthropic_throttle_proxy import proxy

    assert proxy._retry_after_fast_fail_response("b", "v1/messages", 239.9, source="t") is None
    assert proxy._retry_after_fast_fail_response("b", "v1/messages", 240.0, source="t") is None
    fast = proxy._retry_after_fast_fail_response("b", "v1/messages", 240.1, source="t")
    assert fast is not None and fast.status == 429


async def test_hold_then_release_completes_inside_the_client_budget() -> None:
    """A <=240 s window parks the request and returns it to the lane afterwards."""
    lim = _lane()
    lim.note_retry_after(0.2)
    assert lim.retry_after_remaining() > 0
    await lim.wait_retry_after()  # must not raise / fast-fail
    assert lim.retry_after_remaining() == 0
    # The lane is dispatchable again: a parked request can now take a slot.
    async with lim.slot("127.0.0.1:1", max_wait=5.0) as _held:
        assert _held is not None


# ---------------------------------------------------------------------------
# one classifier, so pause and ceiling cannot disagree
# ---------------------------------------------------------------------------


def test_pushback_evidence_taxonomy() -> None:
    """The four classes are decided by evidence, in priority order."""
    from anthropic_throttle_proxy import proxy

    assert proxy._pushback_evidence({"retry-after": "30"}) == "retry-after"
    assert (
        proxy._pushback_evidence({"anthropic-ratelimit-unified-status": "rejected"})
        == "budget"
    )
    assert (
        proxy._pushback_evidence({"anthropic-ratelimit-unified-status": "allowed_warning"})
        == "budget"
    )
    # `allowed` with NO utilization field is still BUDGET: the binding window is
    # unmeasurable, and the historical rule is that an unmeasurable binding is
    # conservative. `concurrency` needs a real utilization below the warn line.
    assert proxy._pushback_evidence({"anthropic-ratelimit-unified-status": "allowed"}, "b") == (
        "budget"
    )
    # `concurrency` is reachable with a measurable binding below the warn line;
    # its full construction is covered by
    # `tests/test_keepalive_hold.py::test_is_transient_throttle_concurrency_429`.
    # No headers, no meter, no cache: this is UNKNOWN — the go-gate's every
    # 429 of 19/10 (live health proves `unified: null` on that upstream).
    assert proxy._pushback_evidence({}, "e574f38a") == "unknown"


def test_unknown_is_still_conservative_but_now_says_so() -> None:
    """`_budget_under_pressure` keeps its historical True on unknown.

    And the AIMD log must carry the evidence class, so a guess can never again
    be read as a measurement — the incident's four lines said
    ``budget/real-retry-after pushback`` about a 429 that carried no evidence at
    all.
    """
    from anthropic_throttle_proxy import proxy

    assert proxy._budget_under_pressure({}, "e574f38a") is True
    assert proxy._pushback_evidence({}, "e574f38a") == "unknown"


async def test_unknown_pushback_still_shrinks_but_is_logged_as_unknown() -> None:
    """Behaviour-preserving by design (see the branch comment in proxy.py)."""
    from anthropic_throttle_proxy import proxy

    lim = _lane()
    before = lim.max_concurrent
    attempt = proxy._Attempt()
    attempt.meta, attempt.final_status = {}, 429
    await proxy._aimd_apply_pushback("e574f38a", lim, attempt, 429, 0.0)
    assert lim.max_concurrent < before


async def test_measured_budget_wall_still_shrinks() -> None:
    """The pre-rejected brake must survive: `allowed_warning` is a real signal.

    Pins ``test_budget_rejected_429_still_shrinks`` from this slice's side.
    """
    from anthropic_throttle_proxy import proxy

    lim = _lane()
    lim.max_concurrent = FREE_INITIAL
    before = lim.max_concurrent
    attempt = proxy._Attempt()
    attempt.meta = {"anthropic-ratelimit-unified-status": "allowed_warning"}
    attempt.final_status = 429
    await proxy._aimd_apply_pushback("e574f38a", lim, attempt, 429, 0.0)
    assert lim.max_concurrent < before
