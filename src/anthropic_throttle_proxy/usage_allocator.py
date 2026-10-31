"""Weekly pacing and client-share allocation (spec 377, phase 1).

Pure decision rules; no I/O and no proxy imports, so every rule is testable
without a server. The proxy feeds in the routing snapshot and the 7d unified
readings it already holds, and acts on the verdicts only when the mode says so.

Pacing: an account's allowed 7d utilisation is the elapsed fraction of its
weekly window plus a burst allowance. Accounts above that line are "ahead".
Ahead accounts are spill-only: they are routed to only when every account is
ahead. Every key stays finite, and the lane never fully blocks: when all
accounts are ahead, priority calls still dispatch and non-priority calls get a
503 whose Retry-After is the catch-up time of the least-ahead account.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .project_share import DIRECT as DIRECT_PROJECT
from .project_share import sanitize

WEEK_S = 7 * 24 * 3600.0
# One 6 h slice of the week. Hot-tunable through THROTTLE_WEEKLY_BURST.
DEFAULT_BURST = 1.0 / 28.0
MODES = ("off", "observe", "enforce")


def parse_mode(raw: str | None) -> str:
    value = (raw or "off").strip().lower()
    return value if value in MODES else "off"


def parse_burst(raw: str | None) -> float:
    """A finite burst in [0, 1] of the week; anything else keeps the default."""
    try:
        value = float((raw or "").strip())
    except ValueError:
        return DEFAULT_BURST
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return DEFAULT_BURST
    return value


def parse_shares(raw: str | None) -> dict[str, float]:
    """``name:0.4,other:0.3`` → finite shares in (0, 1]. Bad entries are dropped."""
    shares: dict[str, float] = {}
    for item in (raw or "").split(","):
        name, sep, value = item.partition(":")
        name = name.strip()
        if not sep or not name:
            continue
        try:
            share = float(value)
        except ValueError:
            continue
        if math.isfinite(share) and 0.0 < share <= 1.0:
            shares[name] = share
    return shares


def allowed_7d(now: float, reset_7d: float, burst: float | None = None) -> float | None:
    """Elapsed fraction of the 7d window plus burst; None when the reset is unknown or past."""
    if reset_7d is None or reset_7d <= now:
        return None
    if burst is None:
        burst = DEFAULT_BURST
    elapsed = min(1.0, max(0.0, 1.0 - (reset_7d - now) / WEEK_S))
    return elapsed + burst


def account_state(
    label: str,
    util_7d: float | None,
    reset_7d: float | None,
    now: float,
    burst: float | None = None,
) -> dict[str, Any]:
    allowed = allowed_7d(now, reset_7d, burst)
    if util_7d is None or allowed is None:
        return {
            "label": label,
            "util_7d": util_7d,
            "reset_7d": reset_7d,
            "allowed_7d": allowed,
            "ahead_by": None,
            "state": "unknown",
        }
    ahead_by = round(util_7d - allowed, 6)
    return {
        "label": label,
        "util_7d": util_7d,
        "reset_7d": reset_7d,
        "allowed_7d": round(allowed, 6),
        "ahead_by": ahead_by,
        "state": "ahead" if ahead_by > 0 else "on_schedule",
    }


def catchup_wait_s(
    util_7d: float, reset_7d: float, now: float, burst: float | None = None
) -> float:
    """Seconds until ``allowed_7d`` reaches ``util_7d`` again (or the window resets)."""
    if burst is None:
        burst = DEFAULT_BURST
    catch_at = reset_7d - WEEK_S + (util_7d - burst) * WEEK_S
    if catch_at >= reset_7d:
        return max(1.0, reset_7d - now)
    return max(1.0, catch_at - now)


def decide_pacing(
    states: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    priority: bool,
    now: float,
    cap_s: float,
    reset_by_label: Mapping[str, float] | None = None,
    util_by_label: Mapping[str, float] | None = None,
    burst: float | None = None,
) -> tuple[str, Any]:
    """Return ``(verdict, detail)``.

    verdicts: ``route`` (detail = labels to route among, or None for no filter),
    ``defer`` (detail = honest Retry-After seconds). ``observe`` and ``off``
    always return ``route`` with no filter, so they cannot change a decision.
    """
    if mode != "enforce":
        return "route", None
    ahead = [s for s in states if s.get("state") == "ahead"]
    if not ahead:
        return "route", None
    if len(ahead) < len(states):
        # Spill-only: keep every account that is on schedule or unknown.
        return "route", [s["label"] for s in states if s.get("state") != "ahead"]
    if priority:
        return "route", None
    return "defer", catchup_retry(
        ahead,
        now=now,
        cap_s=cap_s,
        reset_by_label=reset_by_label,
        util_by_label=util_by_label,
        burst=burst,
    )


def catchup_retry(
    ahead: Sequence[Mapping[str, Any]],
    *,
    now: float,
    cap_s: float,
    reset_by_label: Mapping[str, float] | None = None,
    util_by_label: Mapping[str, float] | None = None,
    burst: float | None = None,
) -> int:
    """Honest Retry-After: the soonest catch-up among ``ahead`` accounts, capped."""
    waits = []
    for s in ahead:
        util = (util_by_label or {}).get(s["label"], s.get("util_7d"))
        reset = (reset_by_label or {}).get(s["label"], s.get("reset_7d"))
        if util is None or reset is None:
            continue
        waits.append(catchup_wait_s(float(util), float(reset), now, burst))
    wait = min(waits) if waits else cap_s
    return max(1, min(int(cap_s), math.ceil(wait)))


# --- phase 2: attribution, measured tokens, multi-window burn ---------------

TOKEN_WINDOW_HOURS = 7 * 24
MAX_PROJECTS = 256  # bounds the ledger; overflow collapses into OVERFLOW
OVERFLOW = "_overflow"
ACTIVE_S = 30 * 60.0  # same "active" horizon as project_share
BURN_LONG_S = 24 * 3600.0
BURN_SHORT_S = 6 * 3600.0
BURN_MIN_SPAN_S = 60.0
BURN_SAMPLE_GAP_S = 60.0  # one stored reading per minute keeps 25 h in a small deque
SAMPLE_KEEP_S = 25 * 3600.0


def weekly_project(project_header: object, workspace_label: object) -> str:
    """Attribution: ``x-anthropic-throttle-project``, else the workspace label, else ``_direct``.

    ``_direct`` is never deferred, so an absent or unusable value falls through to it.
    Both inputs go through the project sanitizer, so ``_direct`` can only come from absence.
    """
    for raw in (project_header, workspace_label):
        if isinstance(raw, str) and raw.strip():
            slug = sanitize(raw)
            if slug != DIRECT_PROJECT:
                return slug
    return DIRECT_PROJECT


class WeeklyTokens:
    """Measured tokens per project over the trailing 7 d, in hourly buckets.

    Only counts the caller supplies (real upstream usage) are stored. Zero or
    absent usage is never recorded, so unknown is never treated as free.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, dict[int, int]] = {}
        self._last_seen: dict[str, float] = {}

    def add(self, project: str, tokens: int, now: float) -> None:
        if type(tokens) is not int or tokens <= 0:
            return
        name = (
            project if project in self._buckets or len(self._buckets) < MAX_PROJECTS else OVERFLOW
        )
        hour = int(now // 3600)
        self._last_seen[name] = now
        bucket = self._buckets.setdefault(name, {})
        bucket[hour] = bucket.get(hour, 0) + tokens
        floor = hour - TOKEN_WINDOW_HOURS
        for old in [h for h in bucket if h <= floor]:
            del bucket[old]

    def totals(self, now: float) -> dict[str, int]:
        floor = int(now // 3600) - TOKEN_WINDOW_HOURS
        out = {}
        for name, bucket in self._buckets.items():
            total = sum(v for h, v in bucket.items() if h > floor)
            if total > 0:
                out[name] = total
        return out

    def active(self, now: float) -> set[str]:
        """Projects with measured usage in the last ``ACTIVE_S`` (contention test)."""
        return {name for name, t in self._last_seen.items() if now - t <= ACTIVE_S}


class WeekSamples:
    """Downsampled per-account 7d readings ``(t, util_7d, reset_7d)`` for burn projection."""

    def __init__(self) -> None:
        self._series: dict[str, list[tuple[float, float, float]]] = {}

    def observe(self, key: str, t: float, util: object, reset: object) -> None:
        if not isinstance(util, (int, float)) or not isinstance(reset, (int, float)):
            return
        if not (math.isfinite(util) and math.isfinite(reset)):
            return
        series = self._series.setdefault(key, [])
        if series and t - series[-1][0] < BURN_SAMPLE_GAP_S and series[-1][2] == reset:
            return
        series.append((t, float(util), float(reset)))
        cutoff = t - SAMPLE_KEEP_S
        while series and series[0][0] < cutoff:
            series.pop(0)

    def samples(self, key: str) -> list[tuple[float, float, float]]:
        return list(self._series.get(key, []))

    def keys(self) -> list[str]:
        return list(self._series)


def burn_projection(
    samples: Sequence[tuple[float, float, float]], now: float, window_s: float
) -> float | None:
    """Projected 7d utilisation at reset, from the slope over the last ``window_s``.

    Only same-epoch samples count, and the window needs real span. Returns None
    (never a guess) when the reset has passed or the span is too short. A
    falling meter projects flat at the current reading.
    """
    if not samples:
        return None
    t_last, u_last, reset = samples[-1]
    if reset <= now:
        return None
    pts = [s for s in samples if s[2] == reset and now - window_s <= s[0] <= t_last]
    if len(pts) < 2:
        return None
    t0, u0, _ = pts[0]
    if t_last - t0 < BURN_MIN_SPAN_S:
        return None
    slope = max(0.0, (u_last - u0) / (t_last - t0))
    return u_last + slope * (reset - now)


def burn_alert(samples: Sequence[tuple[float, float, float]], now: float) -> dict[str, Any]:
    """Multi-window burn (SRE workbook): fire only when BOTH 24 h and 6 h projections exceed 1.0.

    The long window stops a single 5 h spike from firing; the short window stops a
    long-past excursion from firing after the burn has already eased.
    """
    long_p = burn_projection(samples, now, BURN_LONG_S)
    short_p = burn_projection(samples, now, BURN_SHORT_S)
    firing = long_p is not None and short_p is not None and long_p > 1.0 and short_p > 1.0
    return {"long_24h": _r(long_p), "short_6h": _r(short_p), "firing": firing}


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def decide_client_share(
    client_id: str,
    tokens_by_client: Mapping[str, int],
    shares: Mapping[str, float],
    *,
    fleet_ahead: bool,
    contended: bool = True,
) -> str:
    """``admit`` or ``defer`` for one client against its weekly share.

    Only MEASURED tokens count. A client with no measured usage is never deferred
    for lack of data. A client may borrow an idle share: it is deferred only when
    the fleet is ahead of schedule AND another configured client is active
    (``contended``), so an idle sibling's share is lent rather than hoarded.
    """
    share = shares.get(client_id)
    if share is None:
        return "admit"
    total = sum(v for v in tokens_by_client.values() if v > 0)
    if total <= 0:
        return "admit"
    used = max(0, tokens_by_client.get(client_id, 0))
    if used / total > share and fleet_ahead and contended:
        return "defer"
    return "admit"


def publish(
    states: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    burst: float,
    shares: Mapping[str, float],
) -> dict[str, Any]:
    """Read-only view for /__throttle/admission. Pure: never changes a decision.

    ``fleet_pace`` is measured used over elapsed-of-window across KNOWN accounts
    (1.0 = on schedule). Unknown accounts are left out, never counted as zero.
    """
    used = 0.0
    elapsed = 0.0
    for s in states:
        if s.get("state") == "unknown" or s.get("util_7d") is None:
            continue
        used += float(s["util_7d"])
        elapsed += float(s["allowed_7d"]) - burst
    fleet_pace = round(used / elapsed, 4) if elapsed > 1e-9 else None
    return {
        "mode": mode,
        "burst": round(burst, 6),
        "accounts": [dict(s) for s in states],
        "fleet_pace": fleet_pace,
        "fleet_ahead": any(s.get("state") == "ahead" for s in states),
        "client_shares": {
            "configured": dict(shares),
            "enforced": False,
            "phase": "observe-only",
        },
    }
