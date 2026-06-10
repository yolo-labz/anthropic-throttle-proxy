"""Optional account labels: map local credential files to live bearer hashes.

Multi-account hosts run one Claude Code credential file per account
(``~/.claude/.credentials.json``, ``~/.claude-b/.credentials.json``, ...).
``THROTTLE_ACCOUNT_CRED_PATHS`` names them::

    THROTTLE_ACCOUNT_CRED_PATHS=A:/home/u/.claude/.credentials.json,B:/home/u/.claude-b/.credentials.json

Each account's CURRENT bearer hash is derived with the same digest as
:func:`ratelimit._bearer_id` (sha256 of the full ``Bearer <token>`` header
value, first 8 hex), so the dashboard can label live bearers and render a
per-account usage panel. The access token rotates ~8h, so the hash is
recomputed whenever the credential file's mtime/size changes — between
changes a snapshot costs one ``stat()`` per account.

UI-only surface: the hot path never imports this module. The raw token is
hashed and dropped — only the 8-hex prefix survives, matching invariant #2
(bearer token never logged).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from . import config

WINDOW_7D_S = 7 * 86400

# Below this much elapsed 7d-cycle time, pace extrapolation is noise
# (util/elapsed-fraction explodes right after a window reset).
_MIN_PACE_ELAPSED_S = 3600.0

# Pace at/above this multiple of budget-linear burn gets the warn styling.
# Mirrors claude-account-watch's fleet-pace threshold (paceWarnPercent 115).
PACE_WARN = 1.15

# A token expired longer than this is not "claude will self-refresh on next
# call" territory — it means the refresh pipeline (token broker) is failing.
# Mirrors claude-account-pick's b_usable grace.
_TOKEN_GRACE_S = 90 * 60

# path -> (mtime_ns, size, bearer_id, expires_at_ms, error)
_cache: dict[str, tuple[int, int, str | None, int | None, str | None]] = {}


def parse_spec(raw: str) -> list[tuple[str, str]]:
    """Parse ``LABEL:path,LABEL:path`` into ordered (label, path) pairs.

    Malformed entries (no colon, empty label/path) are skipped; duplicate
    labels keep the first occurrence. Never raises — a bad env var must not
    take down the dashboard.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in raw.split(","):
        label, sep, path = entry.strip().partition(":")
        label, path = label.strip(), path.strip()
        if not sep or not label or not path or label in seen:
            continue
        seen.add(label)
        out.append((label, path))
    return out


def _digest_cred(path: str) -> tuple[str | None, int | None, str | None]:
    """Read one credentials file → (bearer_id, expires_at_ms, error).

    The access token is hashed exactly as the proxy hashes the incoming
    ``Authorization`` header (``Bearer <token>``) and immediately discarded.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            oauth = json.load(fh).get("claudeAiOauth") or {}
    except OSError:
        return None, None, "credentials file unreadable"
    except (ValueError, AttributeError):
        return None, None, "credentials file malformed"
    token = oauth.get("accessToken")
    if not token or not isinstance(token, str):
        return None, None, "no access token in credentials"
    bid = hashlib.sha256(f"Bearer {token}".encode("utf-8", "replace")).hexdigest()[:8]
    expires = oauth.get("expiresAt")
    expires_ms = int(expires) if isinstance(expires, (int, float)) else None
    return bid, expires_ms, None


def account_snapshot() -> list[dict[str, Any]]:
    """Resolve configured accounts to their current bearer hashes.

    Returns ``[{label, path, bearer_id, expires_at, error}]`` in env order;
    empty list when ``THROTTLE_ACCOUNT_CRED_PATHS`` is unset (the central
    Dokku tier has no credential files — the feature stays invisible there).
    """
    out: list[dict[str, Any]] = []
    for label, path in parse_spec(config.ACCOUNT_CRED_PATHS):
        try:
            st = os.stat(path)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            _cache.pop(path, None)
            out.append(
                {
                    "label": label,
                    "path": path,
                    "bearer_id": None,
                    "expires_at": None,
                    "error": "credentials file missing",
                }
            )
            continue
        cached = _cache.get(path)
        if cached is None or cached[:2] != key:
            bid, expires_ms, error = _digest_cred(path)
            cached = (*key, bid, expires_ms, error)
            _cache[path] = cached
        out.append(
            {
                "label": label,
                "path": path,
                "bearer_id": cached[2],
                "expires_at": cached[3],
                "error": cached[4],
            }
        )
    return out


def _fmt_duration(seconds: float) -> str:
    """Humanize a positive duration: ``3d 4h`` / ``2h 13m`` / ``45m`` / ``<1m``."""
    s = int(seconds)
    if s < 60:
        return "<1m"
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _window_view(
    util: float | None, reset: int | None, status: str | None, now: float
) -> dict[str, Any] | None:
    """One unified window → display dict, or None when never observed.

    The proxy stores the LAST-SEEN reading per bearer; once the window's
    reset epoch passes, that reading describes the previous cycle. Mirror
    claude-account-pick's semantics: show 0% with a ``reset`` marker instead
    of the stale peak (the 10/06 outage hid behind a frozen stale reading).
    """
    if util is None:
        return None
    stale = reset is not None and reset <= now
    return {
        "pct": 0 if stale else min(100, round(util * 100)),
        "stale": stale,
        "rejected": (not stale) and status == "rejected",
        "reset_in": _fmt_duration(reset - now) if reset is not None and not stale else None,
    }


def _pace_eta(
    util_7d: float | None, reset_7d: int | None, now: float
) -> tuple[float | None, str | None]:
    """7d burn pace (1.0 = exactly on budget) + projected exhaustion time.

    ``pace = utilization ÷ elapsed-cycle-fraction``; ETA extrapolates the same
    linear burn to 100% and is only shown when it lands BEFORE the cycle
    resets (pace > 1) — on-budget accounts never exhaust early by definition.
    """
    if util_7d is None or reset_7d is None or reset_7d <= now:
        return None, None
    elapsed = WINDOW_7D_S - (reset_7d - now)
    if elapsed < _MIN_PACE_ELAPSED_S or util_7d <= 0:
        return None, None
    pace = util_7d / (elapsed / WINDOW_7D_S)
    eta_epoch = (reset_7d - WINDOW_7D_S) + elapsed / util_7d
    eta = _fmt_duration(eta_epoch - now) if eta_epoch < reset_7d else None
    return round(pace, 2), eta


def _token_view(expires_at_ms: int | None, now: float) -> dict[str, str] | None:
    """Credential freshness → ``{state, detail}`` for the accounts table."""
    if expires_at_ms is None:
        return None
    delta = expires_at_ms / 1000.0 - now
    if delta > 0:
        return {"state": "fresh", "detail": f"expires in {_fmt_duration(delta)}"}
    if -delta <= _TOKEN_GRACE_S:
        return {"state": "expiring", "detail": f"expired {_fmt_duration(-delta)} ago"}
    return {
        "state": "expired",
        "detail": f"expired {_fmt_duration(-delta)} ago — refresh pipeline?",
    }


def account_view(bearers: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """Merge the credential-file snapshot with live per-bearer proxy state.

    ``bearers`` is the JSON-safe list `_collect_view` builds (each dict has
    ``bearer_id`` + ``unified``). Accounts whose current bearer the proxy has
    not seen yet (e.g. B parked) still render — with ``seen=False`` and no
    window data — because "B invisible" was exactly the 10/06 blind spot.
    """
    by_id = {b["bearer_id"]: b for b in bearers}
    out: list[dict[str, Any]] = []
    for acct in account_snapshot():
        bearer = by_id.get(acct["bearer_id"]) if acct["bearer_id"] else None
        unified = (bearer or {}).get("unified") or {}
        pace, eta = _pace_eta(unified.get("util_7d"), unified.get("reset_7d"), now)
        out.append(
            {
                "label": acct["label"],
                "bearer_id": acct["bearer_id"],
                "error": acct["error"],
                "seen": bearer is not None,
                "token": _token_view(acct["expires_at"], now),
                "win5": _window_view(
                    unified.get("util_5h"),
                    unified.get("reset_5h"),
                    unified.get("status_5h") or unified.get("status"),
                    now,
                ),
                "win7": _window_view(
                    unified.get("util_7d"), unified.get("reset_7d"), unified.get("status_7d"), now
                ),
                "pace": pace,
                "pace_warn": pace is not None and pace >= PACE_WARN,
                "eta": eta,
            }
        )
    return out


def bearer_labels() -> dict[str, str]:
    """Reverse map ``bearer_id → account label`` for annotating bearer rows."""
    return {a["bearer_id"]: a["label"] for a in account_snapshot() if a["bearer_id"]}
