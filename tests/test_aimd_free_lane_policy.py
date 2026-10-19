"""AIMD policy contract for the FREE lanes (OpenCode go :8770 / zen :8774).

Spec 333 slice A. The free-lane incident (19/10/2026 ~18:57-19:06, go gate) was
not a bug in one knob — it was the AIMD *policy* resting in the wrong place:

* one budget pushback (429) shrank the shared bearer ``e574f38a`` and, with
  ``THROTTLE_AIMD_MIN=1``, a second/third collapse pinned it at ``max_concurrent=1``;
* with 171-303 s generations, one slot turned every other arrival into a
  pre-queue-depth 503 (``est_wait`` 210-355 s > ``max_wait`` 180 s);
* pi panes died in retry loops while the upstream was merely metering.

The policy these tests pin (see ``docs/AIMD-POLICY-FREE-LANES.md``):

1. the floor is a RESTING PLACE, not a limit — sustained pushback must leave the
   lane at ``AIMD_MIN`` (live: 2 on the go gate), never below;
2. one isolated budget 429 costs one multiplicative step (0.7), not a collapse;
3. recovery from the floor is evidence-driven: an isolated shrink ramps back on
   ``AIMD_RAMP_AFTER_FAST`` successes, a storm needs ``AIMD_RAMP_AFTER``;
4. the operator's hard ceiling always wins over the floor (a hot-tuned ceiling
   below ``AIMD_MIN`` must not be raised back by a 429).

``shrink``/``grow`` are the exact functions ``proxy._aimd_feedback`` /
``proxy._aimd_grow`` call on 429/503 and on served 2xx, so these unit tests
exercise the production decision path without an HTTP round trip.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest

from anthropic_throttle_proxy import config, limiter, pacing

# Live free-lane values (19/10/2026): warm start 12, floor 2, hard ceiling 1024.
FREE_HARD_CEILING = 1024
FREE_INITIAL = 12
FREE_FLOOR = 2


@pytest.fixture(autouse=True)
async def _isolate(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[None]:
    """Free-lane AIMD knobs + a clean registry, on this test's own loop."""
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    monkeypatch.setattr(config, "MAX_CONCURRENT", FREE_HARD_CEILING, raising=True)
    monkeypatch.setattr(config, "QUEUE_MODE", "off", raising=True)
    monkeypatch.setattr(config, "AIMD_MIN", FREE_FLOOR, raising=True)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", FREE_INITIAL, raising=True)
    monkeypatch.setattr(config, "AIMD_DECREASE", 0.7, raising=True)
    monkeypatch.setattr(config, "AIMD_BACKOFF_S", 30.0, raising=True)
    monkeypatch.setattr(config, "AIMD_RAMP_AFTER", 10, raising=True)
    monkeypatch.setattr(config, "AIMD_RAMP_AFTER_FAST", 5, raising=True)
    monkeypatch.setattr(config, "AIMD_STORM_THRESHOLD", 3, raising=True)
    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    yield
    config.bearer_limiters.clear()
    config.bearer_state.clear()


def _free_lane(
    queue_mode: str = "fair", hard_max: int = FREE_HARD_CEILING
) -> limiter.FairBearerLimiter:
    return limiter.FairBearerLimiter(hard_max, queue_mode, bearer_id="free-lane")


def _elapse_cooldown(lim: limiter.FairBearerLimiter) -> None:
    """Open the AIMD cooldown gate without aging out the shrink RECENCY.

    ``_shrink_history`` timestamps stay untouched on purpose: they are what
    ``_effective_ramp_after`` reads to choose FAST vs SLOW recovery, and a real
    wall-clock wait long enough to clear ``AIMD_BACKOFF_S`` would push them out
    of the ``2 x AIMD_BACKOFF_S`` lookback at the same moment.
    """
    lim._last_throttle_at -= config.AIMD_BACKOFF_S + 1.0  # noqa: SLF001


def _age_all_timestamps(lim: limiter.FairBearerLimiter, seconds: float) -> None:
    """Move the throttle stamp AND the shrink history ``seconds`` into the past.

    Models real elapsed time, which for a free lane means one generation: the
    history lookback is ``2 x AIMD_BACKOFF_S`` (60 s) while a generation on
    these lanes measured 171-303 s.
    """
    lim._last_throttle_at -= seconds  # noqa: SLF001
    lim._shrink_history = type(lim._shrink_history)(  # noqa: SLF001
        (ts - seconds for ts in lim._shrink_history),
        maxlen=config.AIMD_STORM_THRESHOLD_MAX,
    )


# ---------------------------------------------------------------------------
# 1. warm start + floor under sustained pushback
# ---------------------------------------------------------------------------


def test_free_lane_warm_start_is_initial_cap_not_ceiling() -> None:
    """A restart must not open 1024 wide; AIMD discovers width from traffic."""
    lim = _free_lane()
    assert lim.max_concurrent == FREE_INITIAL
    assert lim.hard_max == FREE_HARD_CEILING


async def test_floor_pins_free_lane_under_sustained_pushback() -> None:
    """8 consecutive 429s must REST at the floor — never collapse to 1.

    This is the 19/10 incident in miniature: the pre-fix policy (floor 1) ended
    at ``max_concurrent=1``, and with 171-303 s generations one slot is a
    503 factory.
    """
    lim = _free_lane()
    seen = [await lim.shrink() for _ in range(8)]
    assert seen[:4] == [8, 5, 3, 2]
    assert lim.max_concurrent == FREE_FLOOR
    assert all(cap >= FREE_FLOOR for cap in seen)


async def test_single_budget_429_costs_one_step_not_a_collapse() -> None:
    """One pushback is metering, not a verdict: 12 -> 8, well above the floor."""
    lim = _free_lane()
    assert await lim.shrink() == 8
    assert lim.max_concurrent > FREE_FLOOR


async def test_shrink_still_cuts_at_least_one_slot_above_the_floor() -> None:
    """The multiplicative cut must not stall when 0.7 rounds back to the same value."""
    config.AIMD_MIN = 1
    lim = limiter.FairBearerLimiter(FREE_HARD_CEILING, "fair", bearer_id="paid-lane")
    lim.max_concurrent = 2
    assert await lim.shrink() == 1


async def test_observe_mode_shrinks_counters_and_still_respects_the_floor() -> None:
    """`observe` moves AIMD counters without a queue — the floor applies there too."""
    lim = _free_lane(queue_mode="observe")
    for _ in range(5):
        await lim.shrink()
    assert lim.max_concurrent == FREE_FLOOR


# ---------------------------------------------------------------------------
# 2. operator hard ceiling beats the floor
# ---------------------------------------------------------------------------


async def test_pushback_never_raises_cap_above_a_hot_tuned_hard_ceiling() -> None:
    """Regression pin: a 429 must not become a capacity GRANT.

    ``/ui/config`` (``config._schedule_existing_limiter_retune``) can drop
    CLAUDE_API_THROTTLE_MAX below the floor; ``_retune_limiter_hard_max`` clamps
    the live cap down, and the next 429 used to re-inflate it to ``AIMD_MIN`` —
    above the ceiling it had just been clamped to, with ``grow()`` unable to
    correct it (it only ramps below ``hard_max``).
    """
    lim = _free_lane()
    await limiter._retune_limiter_hard_max("free-lane", lim, 1)  # noqa: SLF001
    assert (lim.hard_max, lim.max_concurrent) == (1, 1)

    for _ in range(3):
        assert await lim.shrink() == 1
        assert lim.max_concurrent <= lim.hard_max

    _elapse_cooldown(lim)
    for _ in range(20):
        await lim.grow()
    assert lim.max_concurrent == 1


async def test_floor_still_floors_when_it_fits_under_the_ceiling() -> None:
    """The ceiling clamp must not weaken the floor in the normal regime."""
    lim = _free_lane()
    await limiter._retune_limiter_hard_max("free-lane", lim, 6)  # noqa: SLF001
    for _ in range(6):
        await lim.shrink()
    assert lim.max_concurrent == FREE_FLOOR


# ---------------------------------------------------------------------------
# 3. probe-up recovery: an idle bearer at the floor regains width
# ---------------------------------------------------------------------------


async def test_idle_bearer_at_floor_regains_width_after_fast_ramp() -> None:
    """One isolated 429 ⇒ FAST ramp: AIMD_RAMP_AFTER_FAST clean 2xx earn a slot."""
    lim = _free_lane()
    await lim.shrink()
    _elapse_cooldown(lim)
    assert lim._effective_ramp_after() == config.AIMD_RAMP_AFTER_FAST  # noqa: SLF001

    for _ in range(config.AIMD_RAMP_AFTER_FAST - 1):
        assert await lim.grow() is None
    assert await lim.grow() == 9  # 8 + 1, one slot earned


async def test_bearer_at_floor_recovers_on_the_slow_ramp() -> None:
    """A floor reached through a STORM recovers conservatively, not at FAST.

    The floor is a resting place, not a dead end: served 2xx still earn width
    back, but a bearer that just took >= STORM_THRESHOLD hits ramps on the SLOW
    threshold so the lane does not oscillate into the same wall.
    """
    lim = _free_lane()
    for _ in range(8):
        await lim.shrink()
    assert lim.max_concurrent == FREE_FLOOR
    _elapse_cooldown(lim)
    assert lim._effective_ramp_after() == config.AIMD_RAMP_AFTER  # noqa: SLF001

    for _ in range(config.AIMD_RAMP_AFTER - 1):
        assert await lim.grow() is None
    assert await lim.grow() == FREE_FLOOR + 1


async def test_free_lane_generation_outlives_the_fast_ramp_window() -> None:
    """Measured free-lane shape: FAST is unreachable in practice, SLOW is the ramp.

    ``_recent_shrinks`` only counts shrinks inside ``2 x AIMD_BACKOFF_S`` = 60 s,
    and ``recent == 0`` reads as SLOW. A free-lane generation measured 171-303 s,
    so by the time the first clean 2xx lands the isolated shrink has aged out and
    the bearer ramps at ``AIMD_RAMP_AFTER`` (10). Pinned because it decides which
    knob actually buys recovery width on these lanes: the FLOOR and
    ``AIMD_RAMP_AFTER``, not ``AIMD_RAMP_AFTER_FAST``.
    """
    lim = _free_lane()
    await lim.shrink()  # one isolated 429
    _age_all_timestamps(lim, 200.0)  # one generation of real time
    assert lim._recent_shrinks() == 0  # noqa: SLF001
    assert lim._effective_ramp_after() == config.AIMD_RAMP_AFTER  # noqa: SLF001


async def test_storm_pushback_requires_the_slow_ramp() -> None:
    """A storm (>= STORM_THRESHOLD shrinks) must not ramp at the FAST threshold."""
    lim = _free_lane()
    for _ in range(config.AIMD_STORM_THRESHOLD):
        await lim.shrink()
    _elapse_cooldown(lim)
    assert lim._effective_ramp_after() == config.AIMD_RAMP_AFTER  # noqa: SLF001

    before = lim.max_concurrent
    for _ in range(config.AIMD_RAMP_AFTER - 1):
        assert await lim.grow() is None
    assert await lim.grow() == before + 1


async def test_recovery_never_exceeds_the_hard_ceiling() -> None:
    lim = _free_lane(hard_max=FREE_FLOOR)
    _elapse_cooldown(lim)
    for _ in range(3 * config.AIMD_RAMP_AFTER):
        await lim.grow()
    assert lim.max_concurrent == FREE_FLOOR
