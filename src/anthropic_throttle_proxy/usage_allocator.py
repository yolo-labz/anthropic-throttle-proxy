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
    waits = []
    for s in ahead:
        util = (util_by_label or {}).get(s["label"], s.get("util_7d"))
        reset = (reset_by_label or {}).get(s["label"], s.get("reset_7d"))
        if util is None or reset is None:
            continue
        waits.append(catchup_wait_s(float(util), float(reset), now, burst))
    wait = min(waits) if waits else cap_s
    retry_after = max(1, min(int(cap_s), math.ceil(wait)))
    return "defer", retry_after


def decide_client_share(
    client_id: str,
    tokens_by_client: Mapping[str, int],
    shares: Mapping[str, float],
    *,
    fleet_ahead: bool,
) -> str:
    """``admit`` or ``defer`` for one client against its weekly share.

    Only MEASURED tokens count. A client with no measured usage is never deferred
    for lack of data. Borrowing is allowed unless the fleet is over schedule.
    """
    share = shares.get(client_id)
    if share is None:
        return "admit"
    total = sum(v for v in tokens_by_client.values() if v > 0)
    if total <= 0:
        return "admit"
    used = max(0, tokens_by_client.get(client_id, 0))
    if used / total > share and fleet_ahead:
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
