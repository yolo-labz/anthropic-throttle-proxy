"""Per-project share of the 5 h window: attribution, pace state, weighted order.

Design: ``experiments/ai-stack-solver/research/allocator-ideation.md`` §3/§6.
The week is not scarce (≈15 % of capacity used); the 5 h burst window is. So
this module only ever does three things:

1. **Attribution.** A request MAY carry ``x-anthropic-throttle-project: <slug>``
   (stamped by the pi lane extension). The value is sanitized; an absent or
   unusable header is project ``_direct`` — which also covers Claude Code.
2. **Measurement.** Per-project list-$ inside the current 5 h window, plus a
   pace projection ``û`` of the window's utilization at reset.
3. **Advisory ordering.** In AMBER/RED only, a waiter of a project that is over
   its weighted max-min share yields its turn in the queue to an under-share
   project. Nothing is refused, nothing is dropped, an in-flight request is
   never touched, and ``_direct`` is never deferred. In GREEN this module
   changes nothing: the limiter's dispatch path is byte-for-byte the old one.

Explicitly NOT here (ideation §9 "do NOT build"): weekly quotas, $/turn/token
budgets, any refusal path, per-project bearers.

Single-account assumption (ponytail): the meter samples come from whichever
bearer served the request; the fleet runs one Claude bearer (666a53af). With
several bearers the samples would interleave — upgrade path: key samples and
spend by bearer id.
"""

from __future__ import annotations

import collections
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PROJECT_HEADER: Final[str] = "x-anthropic-throttle-project"
DIRECT: Final[str] = "_direct"

WINDOW_S: Final[float] = 5 * 3600.0
WEEK_S: Final[float] = 7 * 86400.0
ACTIVE_S: Final[float] = 30 * 60.0  # "active" = a turn in the last 30 min (§3 set A)
SLOPE_S: Final[float] = 30 * 60.0  # û slope horizon (§3 r̂)
USABLE: Final[float] = 0.9  # U = gateway brake target
AMBER_AT: Final[float] = 0.70
RED_AT: Final[float] = 0.90
WEIGHTS: Final[tuple[int, ...]] = (1, 2, 3, 4)
# below this utilization a list-$ → meter calibration is noise, so it is not made
MIN_CALIBRATION_UTIL: Final[float] = 0.05
# the over-share set is recomputed at most this often (it scans the 5 h ledger)
REFRESH_S: Final[float] = 5.0

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
_MAX_SLUG = 64


def sanitize(raw: object) -> str:
    """Header value → project slug; anything unusable is ``_direct``.

    Lowercase, ``[a-z0-9._-]``, at most 64 chars. A client cannot claim the
    reserved ``_direct`` bucket's exemption with a look-alike: the leading
    underscore is stripped, so ``_direct`` can only come from an absent header.
    """
    if not isinstance(raw, str):
        return DIRECT
    slug = _SLUG_RE.sub("-", raw.strip().lower()).strip("-._")[:_MAX_SLUG].strip("-._")
    return slug or DIRECT


def project_of(headers: object) -> str:
    """The request's project (``headers`` is any mapping with ``.get``)."""
    get = getattr(headers, "get", None)
    return sanitize(get(PROJECT_HEADER) if get else None)


@dataclass(frozen=True)
class WindowView:
    """Everything the dashboard card shows; ``None`` renders as an em dash."""

    util_5h: float | None
    reset_5h: float | None
    projected: float | None  # û at reset
    state: str  # GREEN | AMBER | RED | UNKNOWN
    util_7d: float | None
    reset_7d: float | None
    since_window_start: bool  # did this process see the whole window?
    total_usd: float
    capacity_usd: float | None  # C5h list-$ estimate, None if not calibratable
    rows: tuple[ProjectRow, ...]
    week_usd: float = 0.0
    since_week_start: bool = False
    week_capacity_usd: float | None = None  # None = meter uncalibrated
    week_meter_regressed: bool = False  # u_7d fell inside one reset epoch


@dataclass(frozen=True)
class ProjectRow:
    project: str
    usd: float
    used: float | None  # share of the physical window (used_p); None if uncalibrated
    entitlement: float | None  # g_p; None when the project is not active
    weight: int
    over: bool


class ProjectShare:
    """Process-local ledger. Bounded: one entry per priced request, 5 h."""

    def __init__(self, weights_path: Path | None = None, max_events: int = 200_000) -> None:
        self._lock = threading.Lock()
        self._spend: collections.deque[tuple[float, str, float]] = collections.deque(
            maxlen=max_events
        )
        self._last_seen: dict[str, float] = {}
        self._util: collections.deque[tuple[float, float, float]] = collections.deque(
            maxlen=4096
        )  # (t, util_5h, reset_5h)
        self._week: tuple[float, float] | None = None  # (util_7d, reset_7d)
        self._week_regressed: float | None = None  # reset epoch whose u_7d fell
        self._started = _now()
        self._weights_path = weights_path
        self._weights: dict[str, int] = self._load_weights()
        self._over: frozenset[str] = frozenset()
        self._ordering = False
        self._refreshed_at = float("-inf")

    # --- attribution -------------------------------------------------------
    def record_spend(self, project: str, usd: float, now: float | None = None) -> None:
        t = _now() if now is None else now
        with self._lock:
            self._last_seen[project] = t
            if usd > 0:
                self._spend.append((t, project, usd))

    def observe_meter(self, unified: object, now: float | None = None) -> None:
        """Feed the authoritative unified-ratelimit view of a served response."""
        if not isinstance(unified, dict):
            return
        t = _now() if now is None else now
        u5, r5 = _num(unified.get("util_5h")), _num(unified.get("reset_5h"))
        u7, r7 = _num(unified.get("util_7d")), _num(unified.get("reset_7d"))
        with self._lock:
            if u5 is not None and r5 is not None:
                self._util.append((t, u5, r5))
            if u7 is not None and r7 is not None:
                prev = self._week
                if prev is not None and prev[1] == r7 and u7 < prev[0] - 0.01:
                    # Non-monotone meter (seen 0.61 -> 0.05 in one epoch): any
                    # list-$ -> capacity fit for this week would be fiction.
                    self._week_regressed = r7
                self._week = (u7, r7)
        # ponytail: the refresh rescans the 5 h ledger on the event loop — 9.8 ms
        # at 20k events (20x the measured ~1k turns per window), at most once
        # per REFRESH_S. Upgrade path if volume grows: incremental per-project
        # sums keyed by window, dropping the rescan.
        if t - self._refreshed_at >= REFRESH_S:
            self._refresh_ordering(t)

    # --- weights (the only admin knob) ------------------------------------
    def weight(self, project: str) -> int:
        return self._weights.get(project, 1)

    def set_weight(self, project: str, weight: int) -> int:
        slug = sanitize(project)
        if slug == DIRECT:
            raise ValueError("empty project slug")
        if weight not in WEIGHTS:
            raise ValueError(f"weight must be one of {WEIGHTS}")
        with self._lock:
            if weight == 1:
                self._weights.pop(slug, None)
            else:
                self._weights[slug] = weight
            snapshot = dict(self._weights)
        self._save_weights(snapshot)
        self._refresh_ordering(_now())
        return weight

    def weights(self) -> dict[str, int]:
        with self._lock:
            return dict(self._weights)

    # --- the decision -------------------------------------------------------
    def view(self, now: float | None = None) -> WindowView:
        t = _now() if now is None else now
        with self._lock:
            samples = list(self._util)
            spend = list(self._spend)
            last_seen = dict(self._last_seen)
            week = self._week
            weights = dict(self._weights)
            regressed = self._week_regressed
        u, reset = (samples[-1][1], samples[-1][2]) if samples else (None, None)
        projected = _project(samples, t)
        state = _state(u, projected)
        start = reset - WINDOW_S if reset is not None else t - WINDOW_S
        per = _spend_since(spend, start)
        total = sum(per.values())
        whole = self._started <= start
        capacity = _calibrate(total, u, whole)
        week_start = week[1] - WEEK_S if week else t - WEEK_S
        week_usd = sum(_spend_since(spend, week_start).values())
        week_whole = self._started <= week_start
        week_bad = week is not None and regressed == week[1]
        week_capacity = None if week_bad else _calibrate(week_usd, week and week[0], week_whole)
        active = {p for p, ts in last_seen.items() if t - ts <= ACTIVE_S}
        rows = _share_rows(per, active, weights, capacity)
        return WindowView(
            util_5h=u,
            reset_5h=reset,
            projected=projected,
            state=state,
            util_7d=week[0] if week else None,
            reset_7d=week[1] if week else None,
            since_window_start=whole,
            total_usd=total,
            capacity_usd=capacity,
            rows=rows,
            week_usd=week_usd,
            since_week_start=week_whole,
            week_capacity_usd=week_capacity,
            week_meter_regressed=week_bad,
        )

    def defer_active(self) -> bool:
        """Is advisory ordering on (AMBER/RED with an over-share project)?"""
        return self._ordering

    def defer(self, project: str | None) -> bool:
        """Should a waiter of ``project`` yield its turn? Hot path: no lock, O(1).

        True only while ordering is active (AMBER/RED) AND the project is
        over-share. ``_direct`` (Claude Code / unlabelled) is never deferred.
        """
        return self._ordering and project is not None and project in self._over

    def _refresh_ordering(self, now: float) -> None:
        self._refreshed_at = now
        view = self.view(now)
        over = frozenset(r.project for r in view.rows if r.over and r.project != DIRECT)
        self._over = over
        self._ordering = view.state in ("AMBER", "RED") and bool(over)

    # --- persistence ----------------------------------------------------------
    def _load_weights(self) -> dict[str, int]:
        path = self._weights_path
        if path is None:
            return {}
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            sanitize(k): v
            for k, v in raw.items()
            if isinstance(v, int) and v in WEIGHTS and v != 1 and sanitize(k) != DIRECT
        }

    def _save_weights(self, weights: dict[str, int]) -> None:
        path = self._weights_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(weights, sort_keys=True))
        os.replace(tmp, path)


def _spend_since(spend: list[tuple[float, str, float]], start: float) -> dict[str, float]:
    per: dict[str, float] = collections.defaultdict(float)
    for ts, project, usd in spend:
        if ts >= start:
            per[project] += usd
    return per


def _calibrate(usd: float, util: float | None, saw_whole_period: bool) -> float | None:
    """List-$ capacity of a period = $ seen / meter, or ``None`` if not knowable.

    Only valid when this process saw the whole period and the meter is high
    enough that the ratio is not noise; otherwise the honest answer is unknown.
    """
    if util is None or util < MIN_CALIBRATION_UTIL or not saw_whole_period or usd <= 0:
        return None
    return usd / util


def _share_rows(
    per: dict[str, float], active: set[str], weights: dict[str, int], capacity: float | None
) -> tuple[ProjectRow, ...]:
    """used_p vs g_p = U·w_p/Σ_active w_q, largest spend first (allocator §3)."""
    weight_sum = sum(weights.get(p, 1) for p in active)
    rows: list[ProjectRow] = []
    for project in sorted(set(per) | active, key=lambda p: (-per.get(p, 0.0), p)):
        w = weights.get(project, 1)
        g = USABLE * w / weight_sum if project in active and weight_sum else None
        used = per.get(project, 0.0) / capacity if capacity else None
        over = used is not None and g is not None and used > g
        rows.append(ProjectRow(project, per.get(project, 0.0), used, g, w, over))
    return tuple(rows)


def _project(samples: list[tuple[float, float, float]], now: float) -> float | None:
    """û = u + r̂·(t_reset − t), r̂ from same-window samples of the last 30 min.

    ``None`` (never a guess) without two same-window samples ≥ 60 s apart. A
    falling meter (non-monotone, seen 0.61 → 0.05) clamps the slope at 0.
    """
    if not samples:
        return None
    t_last, u_last, reset = samples[-1]
    window = [s for s in samples if s[2] == reset and t_last - s[0] <= SLOPE_S]
    t_first, u_first, _ = window[0]
    if t_last - t_first < 60.0:
        return None
    slope = max(0.0, (u_last - u_first) / (t_last - t_first))
    return u_last + slope * max(0.0, reset - now)


def _state(util: float | None, projected: float | None) -> str:
    if util is None:
        return "UNKNOWN"
    if util >= RED_AT or (projected is not None and projected > RED_AT):
        return "RED"
    basis = projected if projected is not None else util
    return "AMBER" if basis > AMBER_AT else "GREEN"


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        out = float(value)
    except ValueError:
        return None
    return None if math.isnan(out) else out


def _now() -> float:
    return time.time()


def _default_weights_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "anthropic-throttle-proxy" / "project-weights.json"


SHARE = ProjectShare(weights_path=_default_weights_path())
