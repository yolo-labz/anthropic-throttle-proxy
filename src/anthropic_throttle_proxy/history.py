"""A 60-minute rolling history of the signals the dashboard has to plot.

The proxy knew every current value and none of its own past: `served 5,258`
answers *what*, never *is this a busy hour, a storm, or a dead one*. Every
question an operator actually arrives with — did the 429 storm end, is the
queue draining, did the AIMD cap recover after the last shrink — is a question
about the last half hour (`docs/DASHBOARD-DESIGN.md`, S4.1).

Shape: a 360-slot ring at 10 s resolution (~30 KB, process-local, lost on
restart — the same lifetime as every other counter here). Requests report
themselves through :func:`observe` as they finish; the gauges that only exist
as an instantaneous reading (queue depth, live cap) are sampled by the UI's
background loop through :func:`record`, which closes the current bucket.

No dependency on ``proxy`` — the sampler passes readings in, so this module
stays importable from the hot path without a cycle.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import NamedTuple

__all__ = ["RESOLUTION_S", "WINDOW_S", "Point", "observe", "record", "series", "level_since"]

RESOLUTION_S = 10.0
WINDOW_S = 3600.0
POINTS = int(WINDOW_S / RESOLUTION_S)

# Statuses that mean "the upstream pushed back" — the errors signal. 529 is
# Anthropic's own overload rather than our usage, but from the operator's seat
# it is still a request that did not land, so it counts here (the Providers
# table is where the two are told apart).
_PUSHBACK = frozenset({408, 429, 500, 502, 503, 504, 529})


class Point(NamedTuple):
    """One closed 10-second bucket."""

    t: float
    served: int
    errors: int
    queued: int
    inflight: int
    cap: int
    p50: float | None
    p95: float | None


_ring: deque[Point] = deque(maxlen=POINTS)
# Open bucket — accumulates until the next `record()` closes it.
_served = 0
_errors = 0
# Bounded on purpose: an unbounded list would grow without limit if the
# sampler task never starts (an app that attaches the routes but never runs
# on_startup) or dies, and `record()` sorts it on the event loop. 4096
# samples per 10 s bucket is far past this proxy's ceiling and sorts in well
# under a millisecond; past it the oldest sample in the bucket is dropped.
# ponytail: a drop biases the quantiles toward the tail of a huge bucket. The
# upgrade path is a fixed-bin histogram, which we do not need at this volume.
_DURATION_SAMPLES = 4096
_durations: deque[float] = deque(maxlen=_DURATION_SAMPLES)
# track → (level, the instant it was first observed), so the status strip can
# say "THROTTLED for 12m" instead of an undated verdict (S4.4). Keyed by track
# because two consumers read two different level vocabularies — see
# :func:`level_since`.
_level_at: dict[str, tuple[str, float]] = {}


def observe(status: int | None, duration: float) -> None:
    """Record one finished request into the open bucket.

    Called from the proxy's per-request bookkeeping, so it must stay O(1) and
    never raise: a dashboard nicety cannot be allowed to fail a request.
    """
    global _served, _errors
    _served += 1
    # Membership, not a cast: a non-int status is simply not pushback, and a
    # ValueError raised here would escape into request bookkeeping.
    if status in _PUSHBACK:
        _errors += 1
    _durations.append(duration)


def record(queued: int, inflight: int, cap: int, now: float | None = None) -> Point:
    """Close the open bucket with the current gauge readings and ring it."""
    global _served, _errors
    if now is None:
        now = time.time()
    ds = sorted(_durations)
    point = Point(
        t=now,
        served=_served,
        errors=_errors,
        queued=queued,
        inflight=inflight,
        cap=cap,
        p50=_quantile(ds, 0.50),
        p95=_quantile(ds, 0.95),
    )
    _ring.append(point)
    _served = 0
    _errors = 0
    _durations.clear()
    return point


def _quantile(ordered: list[float], q: float) -> float | None:
    """Nearest-rank quantile of an already-sorted list (None when empty).

    Nearest rank is ``ceil(q × n)``. The rounded form this started as put p50
    of ``[0, 1]`` at ``1`` — wrong for every even sample count.
    """
    if not ordered:
        return None
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def series() -> list[Point]:
    """Every closed bucket, oldest first."""
    return list(_ring)


def level_since(level: str, now: float | None = None, *, track: str = "fleet") -> float:
    """Seconds the caller has been at ``level``; 0 the first time it is seen.

    Idempotent per level: calling it every render keeps the timestamp of the
    TRANSITION, not of the last call.

    ``track`` names an independent vocabulary. The dashboard strip reads
    ``idle|healthy|pacing|throttled`` and the statusline endpoint reads
    ``down|exhausted|throttled|queued|warn|ok`` — two alphabets over one
    process. Sharing a single slot would make each statusline poll look like a
    transition to the strip and reset its ``THROTTLED for 12m`` duration at
    render cadence, which is exactly the signal that duration exists to carry.
    """
    if now is None:
        now = time.time()
    seen = _level_at.get(track)
    if seen is None or seen[0] != level:
        _level_at[track] = (level, now)
        return 0.0
    return max(0.0, now - seen[1])


def reset() -> None:
    """Drop all history — tests only."""
    global _served, _errors
    _ring.clear()
    _served = 0
    _errors = 0
    _durations.clear()
    _level_at.clear()
