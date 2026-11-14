"""Closed-account and parked-request facts for the account rows (display only).

The proxy already knows when an account is closed — the limiter honours the
upstream's Retry-After until ``retry_after_until`` — but the page only read the
*header* (``last_ratelimit``), which a long-closed account no longer carries. On
13/11/2026 07:04 the gmail account had been closed for 37 h with ten requests
parked on it, and the page showed a 36.7 h-old ``0.99 allowed_warning`` meter
saying "resets in 10h 56m": nothing on it said *closed*, nothing said how old
the reading was in a unit a person reads, and the parked requests sat only in
the per-token table.

Pure functions over the view the dashboard already collects; no new state, no
request-path change. Health carries the parked COUNT per bearer, not the age of
the oldest parked request, so no age is shown for it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .runway import _BRT, _hm

# ponytail: a Retry-After shorter than this is a cooldown blip (concurrency
# cooldown is 2 s), not a closed account; raise it if blips start reading as closures.
CLOSED_MIN_S = 60.0


def closed_until(limiter: dict | None, unified: dict | None, now: float) -> dict[str, str] | None:
    """``{"at": "13/11 18:00", "in": "10 h 56 min"}`` while the account is closed.

    Closed = the limiter is holding a Retry-After window, or the last unified
    reading said ``rejected`` and its reset is still ahead. The later of the two
    is when traffic can flow again.
    """
    ends: list[float] = []
    until = (limiter or {}).get("retry_after_until")
    if isinstance(until, (int, float)) and until - now >= CLOSED_MIN_S:
        ends.append(float(until))
    u = unified or {}
    reset = u.get("reset")
    if u.get("status") == "rejected" and isinstance(reset, (int, float)) and reset > now:
        ends.append(float(reset))
    if not ends:
        return None
    end = max(ends)
    # Retry-After lands a fraction before the epoch it names (17:59:59.5 for 18:00).
    at = datetime.fromtimestamp(round(end / 60) * 60, _BRT).strftime("%d/%m %H:%M")
    return {"at": at, "in": _hm(end - now)}


def age_text(age_s: object) -> str | None:
    """``36 h 45 min`` for a reading age in seconds; ``None`` when unknown."""
    if isinstance(age_s, bool) or not isinstance(age_s, (int, float)) or age_s < 0:
        return None
    return _hm(float(age_s))


def annotate_rows(rows: list[dict], bearers: list[dict[str, Any]]) -> None:
    """Carry each bearer's closure and parked count onto its subscription row."""
    by_id = {b["bearer_id"]: b for b in bearers}
    for row in rows:
        row["sample_age_text"] = age_text(row.get("sample_age_s"))
        bearer = by_id.get(row.get("bearer_id") or "")
        if bearer is None:
            row["closed"], row["parked"] = None, None
            continue
        row["closed"] = bearer.get("closed")
        row["parked"] = {"queued": bearer.get("queued", 0), "inflight": bearer.get("inflight", 0)}
        if row["closed"]:
            # Same reasoning as the refused-window rows: a pace or runway next to
            # a closed account is a prediction about something that already happened.
            row["pace"], row["pace_warn"], row["eta"], row["runway"] = None, False, "", None
