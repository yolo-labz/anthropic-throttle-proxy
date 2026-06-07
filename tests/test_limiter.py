"""Direct unit tests for the FairBearerLimiter and the helpers around it.

The proxy app suite exercises the limiter end-to-end via aiohttp, but several
defensive branches (cancellation cleanup, RR rotation skips, mode promotion,
hard-max retune clamps, observe-mode AIMD gating) only fire in narrow runtime
races. Hitting them through real HTTP would be flaky; calling the helpers
directly with crafted state is deterministic and keeps coverage honest.

All tests share an autouse ``_isolate`` fixture that pins the AIMD knobs and
clears the per-bearer registries between cases, so one test cannot poison
another via the module-global ``config.bearer_limiters`` / ``config.bearer_state``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator

import pytest

from anthropic_throttle_proxy import config, limiter, pacing


@pytest.fixture(autouse=True)
async def _isolate(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[None]:
    """Reset registries + AIMD knobs and bind the registry lock to this loop.

    ``bearer_limiter_lock`` is normally bound once from ``main()`` on the
    running loop. Each test gets a fresh loop, so we re-bind here. Ditto the
    ``pacing._dispatch_lock`` so any helper that touches it does not crash.
    """
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    monkeypatch.setattr(config, "MAX_CONCURRENT", 8, raising=True)
    monkeypatch.setattr(config, "QUEUE_MODE", "off", raising=True)
    monkeypatch.setattr(config, "AIMD_MIN", 1, raising=True)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 4, raising=True)
    monkeypatch.setattr(config, "AIMD_DECREASE", 0.7, raising=True)
    monkeypatch.setattr(config, "AIMD_BACKOFF_S", 30, raising=True)
    monkeypatch.setattr(config, "AIMD_RAMP_AFTER", 10, raising=True)
    monkeypatch.setattr(config, "AIMD_RAMP_AFTER_FAST", 5, raising=True)
    monkeypatch.setattr(config, "AIMD_STORM_THRESHOLD", 3, raising=True)
    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    yield
    config.bearer_limiters.clear()
    config.bearer_state.clear()


# ---------------------------------------------------------------------------
# _initial_live_cap + FairBearerLimiter ctor
# ---------------------------------------------------------------------------


def test_initial_live_cap_clamps_above_hard_max() -> None:
    # hard_max=2, AIMD_INITIAL=4 → should clamp down to hard_max.
    config.AIMD_INITIAL_CONCURRENT = 4
    config.AIMD_MIN = 1
    assert limiter._initial_live_cap(2) == 2


def test_initial_live_cap_floors_at_aimd_min() -> None:
    # hard_max=10, AIMD_INITIAL=0 → AIMD_MIN floor wins.
    config.AIMD_INITIAL_CONCURRENT = 0
    config.AIMD_MIN = 3
    assert limiter._initial_live_cap(10) == 3


def test_ctor_sets_observe_and_queue_flags_for_each_mode() -> None:
    off = limiter.FairBearerLimiter(8, "off")
    assert (off.queue_enabled, off.observe_enabled) == (False, False)
    obs = limiter.FairBearerLimiter(8, "observe")
    assert (obs.queue_enabled, obs.observe_enabled) == (False, True)
    fair = limiter.FairBearerLimiter(8, "fair")
    assert (fair.queue_enabled, fair.observe_enabled) == (True, True)
    rea = limiter.FairBearerLimiter(8, "reactive")
    assert (rea.queue_enabled, rea.observe_enabled) == (True, True)


# ---------------------------------------------------------------------------
# set_queue_mode (lines 117-119)
# ---------------------------------------------------------------------------


def test_set_queue_mode_transitions_flags() -> None:
    lim = limiter.FairBearerLimiter(8, "off")
    lim.set_queue_mode("fair")
    assert lim.queue_mode == "fair"
    assert lim.queue_enabled is True
    assert lim.observe_enabled is True
    lim.set_queue_mode("observe")
    assert lim.queue_enabled is False
    assert lim.observe_enabled is True
    lim.set_queue_mode("off")
    assert lim.queue_enabled is False
    assert lim.observe_enabled is False


# ---------------------------------------------------------------------------
# shrink / grow observe-mode gating (line 140 + grow off-mode None)
# ---------------------------------------------------------------------------


async def test_shrink_returns_none_in_off_mode() -> None:
    lim = limiter.FairBearerLimiter(8, "off")
    assert await lim.shrink() is None
    # max_concurrent must NOT have moved — off mode is dead silent.
    assert lim.max_concurrent == limiter._initial_live_cap(8)


async def test_shrink_decreases_in_observe_mode() -> None:
    lim = limiter.FairBearerLimiter(8, "observe")
    start = lim.max_concurrent
    new_max = await lim.shrink()
    assert new_max is not None
    assert new_max < start
    assert new_max >= config.AIMD_MIN


async def test_shrink_floor_holds_at_aimd_min() -> None:
    lim = limiter.FairBearerLimiter(8, "fair")
    lim.max_concurrent = 1
    new_max = await lim.shrink()
    assert new_max == config.AIMD_MIN


async def test_grow_returns_none_in_off_mode() -> None:
    lim = limiter.FairBearerLimiter(8, "off")
    assert await lim.grow() is None


# ---------------------------------------------------------------------------
# note_retry_after / wait_retry_after (line 197)
# ---------------------------------------------------------------------------


def test_note_retry_after_zero_or_negative_returns_existing_window() -> None:
    lim = limiter.FairBearerLimiter(8, "fair")
    # No prior window: zero or negative is a no-op and returns the stored 0.0.
    assert lim.note_retry_after(0) == 0.0
    assert lim.note_retry_after(-5) == 0.0
    assert lim._retry_after_until == 0.0


def test_note_retry_after_only_extends_window() -> None:
    lim = limiter.FairBearerLimiter(8, "fair")
    first = lim.note_retry_after(60)
    # A SHORTER follow-up retry-after must NOT shrink the window.
    second = lim.note_retry_after(1)
    assert second == first


def test_retry_after_remaining_clamps_to_zero() -> None:
    lim = limiter.FairBearerLimiter(8, "fair")
    assert lim.retry_after_remaining() == 0.0


async def test_wait_retry_after_no_op_when_clear() -> None:
    lim = limiter.FairBearerLimiter(8, "fair")
    # Should return immediately — no Retry-After window pending.
    await asyncio.wait_for(lim.wait_retry_after(), timeout=0.1)


# ---------------------------------------------------------------------------
# _retune_limiter_hard_max (line 48 + early-return)
# ---------------------------------------------------------------------------


async def test_retune_no_change_short_circuits() -> None:
    lim = limiter.FairBearerLimiter(8, "fair")
    pre = lim.max_concurrent
    # Same hard_max + no live_floor → early return without touching state.
    await limiter._retune_limiter_hard_max("bid", lim, 8)
    assert lim.max_concurrent == pre
    assert lim.hard_max == 8


async def test_retune_shrinks_max_concurrent_when_hard_max_drops() -> None:
    lim = limiter.FairBearerLimiter(8, "fair")
    lim.max_concurrent = 8
    await limiter._retune_limiter_hard_max("bid", lim, 4)
    assert lim.hard_max == 4
    assert lim.max_concurrent == 4  # line 48: clamped to new ceiling


async def test_retune_with_live_floor_warm_starts_above_initial_cap() -> None:
    lim = limiter.FairBearerLimiter(8, "fair")
    lim.max_concurrent = 1
    await limiter._retune_limiter_hard_max("bid", lim, 8, live_floor=6)
    assert lim.max_concurrent == 6


async def test_retune_existing_limiters_iterates_registry() -> None:
    a = limiter.FairBearerLimiter(8, "fair")
    b = limiter.FairBearerLimiter(8, "fair")
    a.max_concurrent = 8
    b.max_concurrent = 8
    config.bearer_limiters["a"] = a
    config.bearer_limiters["b"] = b
    await limiter.retune_existing_limiters(2)
    assert a.hard_max == 2 and b.hard_max == 2
    assert a.max_concurrent == 2 and b.max_concurrent == 2


# ---------------------------------------------------------------------------
# _get_bearer_limiter — promotion + retune branches (lines 355-357)
# ---------------------------------------------------------------------------


async def test_get_bearer_limiter_allocates_then_reuses() -> None:
    a = await limiter._get_bearer_limiter("bid", queue_mode="fair", max_concurrent=4)
    b = await limiter._get_bearer_limiter("bid", queue_mode="fair", max_concurrent=4)
    assert a is b  # cached path
    assert "bid" in config.bearer_state


async def test_get_bearer_limiter_promotes_off_to_fair() -> None:
    lim = await limiter._get_bearer_limiter("bid", queue_mode="off", max_concurrent=4)
    assert lim.queue_enabled is False
    # Same bearer, new mode "fair" → promote (lines 355-357).
    again = await limiter._get_bearer_limiter("bid", queue_mode="fair", max_concurrent=4)
    assert again is lim
    assert again.queue_mode == "fair"
    assert again.queue_enabled is True


async def test_get_bearer_limiter_does_not_downgrade_fair_to_off() -> None:
    lim = await limiter._get_bearer_limiter("bid", queue_mode="fair", max_concurrent=4)
    # Asking for "off" while a queue exists must NOT downgrade — queued
    # futures would never be woken if the dispatcher disappeared.
    again = await limiter._get_bearer_limiter("bid", queue_mode="off", max_concurrent=4)
    assert again is lim
    assert again.queue_mode == "fair"
    assert again.queue_enabled is True


async def test_get_bearer_limiter_retunes_on_lookup_when_hard_max_changed() -> None:
    lim = await limiter._get_bearer_limiter("bid", queue_mode="fair", max_concurrent=8)
    lim.max_concurrent = 8
    again = await limiter._get_bearer_limiter("bid", queue_mode="fair", max_concurrent=2)
    assert again is lim
    assert lim.hard_max == 2
    assert lim.max_concurrent == 2


# ---------------------------------------------------------------------------
# acquire / release happy paths (off mode + fair mode)
# ---------------------------------------------------------------------------


async def test_acquire_and_release_off_mode_just_tracks_inflight() -> None:
    lim = limiter.FairBearerLimiter(8, "off")
    await lim.acquire("c1")
    assert lim.inflight == 1
    await lim.release()
    assert lim.inflight == 0


async def test_acquire_and_release_fair_mode_with_one_client() -> None:
    lim = limiter.FairBearerLimiter(2, "fair")
    await lim.acquire("c1")
    assert lim.inflight == 1
    await lim.release()
    assert lim.inflight == 0


# ---------------------------------------------------------------------------
# _remove_pending (lines 264-279)
# ---------------------------------------------------------------------------


async def test_remove_pending_unknown_client_returns_false() -> None:
    lim = limiter.FairBearerLimiter(2, "fair")
    fut = asyncio.get_running_loop().create_future()
    # Client deque does not exist (264-266 branch).
    assert lim._remove_pending("ghost", fut) is False


async def test_remove_pending_fut_not_in_queue_returns_false() -> None:
    lim = limiter.FairBearerLimiter(2, "fair")
    loop = asyncio.get_running_loop()
    queued = loop.create_future()
    other = loop.create_future()
    lim._queues["c1"] = __import__("collections").deque([queued])
    lim._rr_order.append("c1")
    # `other` is not in the deque → ValueError → returns False (267-272 branch).
    assert lim._remove_pending("c1", other) is False
    assert "c1" in lim._queues  # deque still has `queued`, not pruned


async def test_remove_pending_happy_path_prunes_empty_deque() -> None:
    lim = limiter.FairBearerLimiter(2, "fair")
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    import collections

    lim._queues["c1"] = collections.deque([fut])
    lim._rr_order.append("c1")
    assert lim._remove_pending("c1", fut) is True
    assert "c1" not in lim._queues
    assert "c1" not in lim._rr_order


async def test_remove_pending_tolerates_rr_order_already_pruned() -> None:
    """The ValueError-on-rr_order-remove branch (lines 277-278)."""
    lim = limiter.FairBearerLimiter(2, "fair")
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    import collections

    lim._queues["c1"] = collections.deque([fut])
    # Intentionally NOT in _rr_order — exercises the ValueError pass.
    assert lim._remove_pending("c1", fut) is True
    assert "c1" not in lim._queues


# ---------------------------------------------------------------------------
# acquire cancellation paths (lines 241-243, _cancel_cleanup 252-256)
# ---------------------------------------------------------------------------


async def test_acquire_cancelled_while_queued_cleans_up() -> None:
    """Caller cancellation while parked in the deque must reraise + prune."""
    lim = limiter.FairBearerLimiter(1, "fair")
    # Saturate the live cap so the next acquire is forced to queue.
    await lim.acquire("primary")
    assert lim.inflight == 1

    task = asyncio.create_task(lim.acquire("c1"))
    # Let the task reach `await fut` inside acquire.
    for _ in range(5):
        await asyncio.sleep(0)
    assert "c1" in lim._queues  # parked

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Cleanup must have removed the future from the deque + RR order.
    assert "c1" not in lim._queues
    assert "c1" not in lim._rr_order
    # The primary slot must still be held — cancellation does NOT release it.
    assert lim.inflight == 1


async def test_cancel_cleanup_dispatched_branch_releases_slot() -> None:
    """Direct call to _cancel_cleanup with a dispatched-but-cancelled future.

    Models the race where ``_try_dispatch`` already called ``set_result(None)``
    + ``inflight += 1`` on the future, but the awaiting task is cancelled
    before observing the result. Lines 252-256 must roll inflight back.
    """
    lim = limiter.FairBearerLimiter(2, "fair")
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut.set_result(None)  # done, not cancelled, no exception
    lim.inflight = 1
    await lim._cancel_cleanup("c1", fut)
    assert lim.inflight == 0


# ---------------------------------------------------------------------------
# _try_dispatch (lines 293, 297, 301)
# ---------------------------------------------------------------------------


async def test_try_dispatch_skips_empty_or_missing_client_queue() -> None:
    """Stale RR entry without a corresponding deque must be skipped (line 293)."""
    lim = limiter.FairBearerLimiter(2, "fair")
    lim._rr_order.append("ghost")  # no _queues entry
    lim._try_dispatch()
    assert lim.inflight == 0
    assert "ghost" not in lim._rr_order


async def test_try_dispatch_re_appends_client_with_more_queued() -> None:
    """A client with >1 pending future is rotated to the tail (line 297)."""
    lim = limiter.FairBearerLimiter(2, "fair")
    loop = asyncio.get_running_loop()
    f1 = loop.create_future()
    f2 = loop.create_future()
    import collections

    lim._queues["c1"] = collections.deque([f1, f2])
    lim._rr_order.append("c1")
    lim.max_concurrent = 1  # only 1 slot available → dispatch f1 only
    lim._try_dispatch()
    assert lim.inflight == 1
    assert f1.done() and not f2.done()
    assert lim._rr_order[-1] == "c1"  # re-appended for next round


async def test_try_dispatch_skips_cancelled_future() -> None:
    """A cancelled future must be popped without bumping inflight (line 301)."""
    lim = limiter.FairBearerLimiter(2, "fair")
    loop = asyncio.get_running_loop()
    cancelled = loop.create_future()
    cancelled.cancel()
    import collections

    lim._queues["c1"] = collections.deque([cancelled])
    lim._rr_order.append("c1")
    lim._try_dispatch()
    assert lim.inflight == 0  # skipped, not dispatched


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_snapshot_returns_expected_keys() -> None:
    lim = limiter.FairBearerLimiter(8, "fair")
    snap = lim.snapshot()
    assert {
        "inflight",
        "max_concurrent",
        "hard_max",
        "queue_mode",
        "queue_enabled",
        "observe_enabled",
        "last_throttle_at",
        "successes_since_throttle",
        "retry_after_until",
        "queued_total",
        "queued_per_client",
        "rr_order",
        "recent_shrinks",
        "storm_mode",
        "effective_ramp_after",
    } <= set(snap.keys())
    assert snap["queue_mode"] == "fair"
    assert snap["queued_total"] == 0
    # Fresh limiter has no shrink history → SLOW (clean) path active.
    # FAST is a *recovery* signal, only applied while ≥1 shrink is still in
    # the 2× BACKOFF_S lookback window AND the storm threshold not crossed.
    assert snap["recent_shrinks"] == 0
    assert snap["storm_mode"] is False
    assert snap["effective_ramp_after"] == config.AIMD_RAMP_AFTER


# ---------------------------------------------------------------------------
# Adaptive ramp (PR #53) — _shrink_history + _effective_ramp_after + snapshot
# ---------------------------------------------------------------------------


async def test_shrink_appends_to_history_in_observe_mode() -> None:
    """Each shrink appends one timestamp to the sliding history."""
    lim = limiter.FairBearerLimiter(8, "observe")
    pre = len(lim._shrink_history)
    await lim.shrink()
    assert len(lim._shrink_history) == pre + 1
    # Timestamp must equal the recorded last_throttle_at (single source of truth).
    assert lim._shrink_history[-1] == lim._last_throttle_at


async def test_shrink_does_not_record_history_in_off_mode() -> None:
    """`off` mode is dead silent — no AIMD signal, no history mutation."""
    lim = limiter.FairBearerLimiter(8, "off")
    await lim.shrink()
    assert len(lim._shrink_history) == 0


def test_effective_ramp_after_is_slow_with_no_shrinks() -> None:
    """Clean state (recent==0) keeps the SLOW threshold — FAST is recovery-only.

    Backward-compat invariant: a freshly-allocated limiter must behave
    identically to pre-PR-53 builds (ramp at AIMD_RAMP_AFTER successes), so
    that ``test_clean_successes_grow_from_initial_cap`` and any other test
    that relies on the SLOW default with zero prior shrinks keeps passing.
    """
    lim = limiter.FairBearerLimiter(8, "fair")
    assert lim._effective_ramp_after() == config.AIMD_RAMP_AFTER


def test_effective_ramp_after_is_fast_below_threshold() -> None:
    """STORM_THRESHOLD-1 recent shrinks must still resolve FAST."""
    lim = limiter.FairBearerLimiter(8, "fair")
    now = 1_000_000.0
    # 2 recent shrinks (threshold = 3) → still FAST.
    lim._shrink_history.extend([now - 1.0, now - 2.0])
    assert lim._effective_ramp_after(now) == config.AIMD_RAMP_AFTER_FAST


def test_effective_ramp_after_is_slow_at_threshold() -> None:
    """Exactly STORM_THRESHOLD recent shrinks promotes to SLOW."""
    lim = limiter.FairBearerLimiter(8, "fair")
    now = 1_000_000.0
    lim._shrink_history.extend([now - 0.5, now - 1.0, now - 2.0])
    assert lim._effective_ramp_after(now) == config.AIMD_RAMP_AFTER


def test_effective_ramp_after_decays_when_history_ages_out() -> None:
    """Storm-mode auto-clears when timestamps exit the 2× BACKOFF_S window.

    Aged-out history means recent==0 → three-state semantics resolve to
    SLOW (clean state), NOT FAST (FAST is the isolated-recovery middle
    band, 1 ≤ recent < STORM_THRESHOLD).
    """
    lim = limiter.FairBearerLimiter(8, "fair")
    now = 1_000_000.0
    # All 3 shrinks are OUTSIDE the 2 × AIMD_BACKOFF_S = 60 s cutoff
    # (lookback was widened in PR #53 so the FAST band is reachable in
    # practice — see ``_recent_shrinks`` docstring).
    aged = now - (2 * config.AIMD_BACKOFF_S + 1)
    lim._shrink_history.extend([aged - 1.0, aged - 2.0, aged - 3.0])
    assert lim._recent_shrinks(now) == 0
    assert lim._effective_ramp_after(now) == config.AIMD_RAMP_AFTER


def test_effective_ramp_after_with_storm_threshold_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STORM_THRESHOLD=1 disables the adaptive path — every shrink is a storm."""
    monkeypatch.setattr(config, "AIMD_STORM_THRESHOLD", 1, raising=True)
    lim = limiter.FairBearerLimiter(8, "fair")
    now = 1_000_000.0
    lim._shrink_history.append(now - 1.0)
    assert lim._effective_ramp_after(now) == config.AIMD_RAMP_AFTER


async def test_may_grow_uses_fast_threshold_after_isolated_shrink() -> None:
    """After an isolated shrink, FAST=5 successes are enough to ramp.

    Hypothesis: under FAST conditions, _may_grow becomes True at exactly
    AIMD_RAMP_AFTER_FAST successes, NOT at the slow AIMD_RAMP_AFTER.

    Note on cooldown: previously this test set ``AIMD_BACKOFF_S = 0`` to
    skip the post-shrink cooldown, but doing so collapses the
    ``_recent_shrinks`` lookback window (``cutoff = now - 2*0 = now``), so
    the just-recorded shrink timestamp ages out instantly and FAST becomes
    unreachable. Instead we keep the default BACKOFF_S=30 and zero
    ``_last_throttle_at`` directly — that bypasses the cooldown gate while
    leaving the recency window intact, isolating the FAST-threshold
    invariant from the cooldown invariant.
    """
    lim = limiter.FairBearerLimiter(8, "fair")
    lim.max_concurrent = 4  # below hard_max so growth IS allowed
    # Single isolated shrink — still well under STORM_THRESHOLD=3.
    await lim.shrink()
    # Bypass the post-shrink cooldown without zeroing AIMD_BACKOFF_S.
    lim._last_throttle_at = 0.0
    # Sanity: shrink IS recent + count below storm threshold → FAST mode.
    assert lim._effective_ramp_after() == config.AIMD_RAMP_AFTER_FAST
    # Each grow() call increments _successes_since_throttle by 1 BEFORE checking
    # _may_grow. Below FAST threshold, no bump.
    for _ in range(config.AIMD_RAMP_AFTER_FAST - 1):
        assert await lim.grow() is None
    # Nth call hits the FAST threshold + bumps.
    bumped = await lim.grow()
    assert bumped is not None
    assert bumped == lim.max_concurrent
    # Sanity: SLOW threshold (10) was NOT required.
    assert config.AIMD_RAMP_AFTER_FAST < config.AIMD_RAMP_AFTER


async def test_may_grow_uses_slow_threshold_under_storm() -> None:
    """During an active storm, FAST=5 successes are NOT enough — SLOW=10 holds.

    Inject the storm signal (recent shrink timestamps) directly rather than
    calling :meth:`shrink` STORM_THRESHOLD times, because ``shrink`` ALSO
    multiplicatively decreases ``max_concurrent`` (4→2→1→1 with the default
    DECREASE=0.7, MIN=1) — that ceiling-clamp side effect would conflate with
    the ramp-threshold invariant we want to verify here. Setting
    ``_last_throttle_at = 0.0`` opens the BACKOFF_S cooldown gate so the only
    remaining ramp gate is the adaptive threshold itself.
    """
    lim = limiter.FairBearerLimiter(8, "fair")
    lim.max_concurrent = 4
    # Inject STORM_THRESHOLD recent shrinks directly — bypasses shrink()'s
    # multiplicative-decrease that would otherwise floor max_concurrent to MIN.
    now = time.time()
    lim._shrink_history.extend([now - 0.3, now - 0.2, now - 0.1])
    # Cooldown wide open so the only gate left is the adaptive ramp threshold.
    lim._last_throttle_at = 0.0
    assert lim._effective_ramp_after() == config.AIMD_RAMP_AFTER
    # FAST-threshold successes must NOT bump under storm — SLOW gate holds.
    for _ in range(config.AIMD_RAMP_AFTER_FAST):
        assert await lim.grow() is None
    # Ceiling unchanged because we never crossed SLOW=AIMD_RAMP_AFTER.
    assert lim.max_concurrent == 4


async def test_storm_mode_recovers_when_window_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once shrink timestamps age past 2× BACKOFF_S, storm flag clears.

    Under three-state semantics, full storm clearance returns to *clean
    state* (``recent_shrinks == 0`` ⇒ SLOW), NOT FAST. FAST is a recovery
    signal only available in the ``1 ≤ recent < STORM_THRESHOLD`` band —
    with zero recent shrinks we are back at the conservative pre-adaptive
    default, preserving backward compat with
    ``test_clean_successes_grow_from_initial_cap``. The FAST band itself
    is exercised by ``test_isolated_shrink_uses_fast_ramp``.

    Falsifier for the design: if ``storm_mode`` stayed True with zero
    recent shrinks, the storm flag would be sticky and the adaptive ramp
    would never recover. The assertion ``snap["storm_mode"] is False``
    is the load-bearing check; the SLOW assertion just locks in the
    three-state semantics.
    """
    monkeypatch.setattr(config, "AIMD_BACKOFF_S", 1, raising=True)
    lim = limiter.FairBearerLimiter(8, "fair")
    # Inject 3 shrinks that are already aged-out (older than 2× BACKOFF_S=2s).
    aged = time.time() - 5.0
    lim._shrink_history.extend([aged, aged - 0.1, aged - 0.2])
    # No live recent shrinks → clean state → SLOW (default), storm_mode False.
    assert lim._effective_ramp_after() == config.AIMD_RAMP_AFTER
    snap = lim.snapshot()
    assert snap["recent_shrinks"] == 0
    assert snap["storm_mode"] is False


def test_shrink_history_deque_bounded_by_maxlen() -> None:
    """Pathological storm cannot grow _shrink_history without bound."""
    lim = limiter.FairBearerLimiter(8, "fair")
    cap = lim._shrink_history.maxlen
    assert cap is not None
    # Append 2× cap entries — deque must drop the oldest.
    for i in range(cap * 2):
        lim._shrink_history.append(float(i))
    assert len(lim._shrink_history) == cap
