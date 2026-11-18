"""Per-account quota runway: when does the 7d meter reach 100 % at the current pace.

The older ``accounts._pace_eta`` divides utilisation by the elapsed cycle, i.e. it
averages the burn over the whole week. When a fleet decides to burn quota now
that average says "no exhaustion" while the meter climbs 0.35/h (11/11/2026).
This module answers from the *recent* slope instead, using the per-bearer
``(t, util_7d, reset_7d)`` series the proxy already keeps for weekly pacing
(``proxy._WEEK_SAMPLES``, one reading per minute). No sampler, no file read.

Robustness: a Theil–Sen slope (median of pairwise slopes) over the last
``WINDOW_S`` seconds, using only samples after the most recent decrease or reset
change, with at least ``MIN_DISTINCT`` distinct readings over ``MIN_SPAN_S``.
Anything short of that renders "—" with the reason, never a guessed number.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Final

WINDOW_S: Final[float] = 30 * 60.0
MIN_SPAN_S: Final[float] = 10 * 60.0
MIN_DISTINCT: Final[int] = 3
_BRT = timezone(timedelta(hours=-3))  # Brazil has had no DST since 2019.

Sample = tuple[float, float, float]  # (t, util_7d 0..1, reset_7d epoch)


def _after_last_drop(samples: Sequence[Sample]) -> list[Sample]:
    """Samples since the last decrease or reset-epoch change (a new baseline)."""
    start = 0
    for i in range(1, len(samples)):
        if samples[i][1] < samples[i - 1][1] or samples[i][2] != samples[i - 1][2]:
            start = i
    return list(samples[start:])


def _theil_sen(pts: Sequence[Sample]) -> tuple[float, float]:
    """(slope per second, fitted utilisation at the last sample's time)."""
    slopes = [
        (b[1] - a[1]) / (b[0] - a[0])
        for i, a in enumerate(pts)
        for b in pts[i + 1 :]
        if b[0] > a[0]
    ]
    slope = median(slopes)
    t_last = pts[-1][0]
    return slope, median(u - slope * (t - t_last) for t, u, _ in pts)


def _hm(seconds: float) -> str:
    minutes = max(0, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def _none(reason: str) -> dict[str, Any]:
    return {"state": "none", "text": "—", "reason": reason, "at": None}


def runway(samples: Sequence[Sample], now: float, *, stale: bool) -> dict[str, Any]:
    """``{state, text, reason, at}``; state is ``runs_out`` | ``lasts`` | ``none``."""
    if stale:
        return _none("quota reading is stale")
    pts = [s for s in _after_last_drop(samples) if s[0] >= now - WINDOW_S]
    if len({round(u, 6) for _, u, _ in pts}) < MIN_DISTINCT or pts[-1][0] - pts[0][0] < MIN_SPAN_S:
        return _none("too few readings in the last 30 min")
    slope, u_fit = _theil_sen(pts)
    if slope <= 0:
        return _none("meter not rising")
    t_last, _, reset = pts[-1]
    at = t_last + max(0.0, 1.0 - u_fit) / slope
    if at >= reset:
        return {"state": "lasts", "text": "lasts to reset", "reason": "", "at": at}
    when = datetime.fromtimestamp(at, _BRT).strftime("%d/%m %H:%M")
    text = f"runs out at {when} (in {_hm(at - now)})"
    return {"state": "runs_out", "text": text, "reason": "", "at": at}
