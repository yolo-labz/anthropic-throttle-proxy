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

Dashboard surfaces only expose the 8-hex bearer prefix. The optional hot-path
account router also reads the token so it can rewrite the upstream
Authorization header, but still never logs or exports the raw value.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime
from typing import Any

import aiohttp

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

# path -> (mtime_ns, size, bearer_id, expires_at_ms, error, access_token)
_cache: dict[str, tuple[int, int, str | None, int | None, str | None, str | None]] = {}


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


def _digest_cred(path: str) -> tuple[str | None, int | None, str | None, str | None]:
    """Read one credentials file → (bearer_id, expires_at_ms, error, token).

    The access token is hashed exactly as the proxy hashes the incoming
    ``Authorization`` header (``Bearer <token>``). The dashboard callers drop
    the token; the opt-in account router consumes it for upstream auth rewrite.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            oauth = json.load(fh).get("claudeAiOauth") or {}
    except OSError:
        return None, None, "credentials file unreadable", None
    except (ValueError, AttributeError):
        return None, None, "credentials file malformed", None
    token = oauth.get("accessToken")
    if not token or not isinstance(token, str):
        return None, None, "no access token in credentials", None
    bid = hashlib.sha256(f"Bearer {token}".encode("utf-8", "replace")).hexdigest()[:8]
    expires = oauth.get("expiresAt")
    expires_ms = int(expires) if isinstance(expires, (int, float)) else None
    return bid, expires_ms, None, token


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
            bid, expires_ms, error, token = _digest_cred(path)
            cached = (*key, bid, expires_ms, error, token)
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


def _fresh_endpoint_entry(path: str, now: float) -> dict[str, Any] | None:
    """Fresh cached usage endpoint entry for routing/UI reuse, never fetching."""
    entry = _endpoint_cache.get(path)
    if entry is None:
        return None
    fetched = entry.get("fetched")
    if not isinstance(fetched, (int, float)) or now - fetched > ENDPOINT_STALE_MAX_S:
        return None
    return entry


def routing_snapshot(now: float | None = None) -> list[dict[str, Any]]:
    """Resolve configured accounts for hot-path routing.

    Returns only currently usable credentials with their bearer hash and raw
    access token. A token with an explicit past ``expiresAt`` is skipped; the
    token broker should refresh the file before the router uses it again.
    """
    now_s = now if now is not None else datetime.now().timestamp()
    now_ms = int(now_s * 1000)
    usable: list[dict[str, Any]] = []
    for acct in account_snapshot():
        if acct["error"] or not acct["bearer_id"]:
            continue
        cached = _cache.get(acct["path"])
        token = cached[5] if cached is not None else None
        if not token:
            continue
        expires_at = acct.get("expires_at")
        if isinstance(expires_at, int) and expires_at <= now_ms:
            continue
        usable.append(
            {**acct, "token": token, "endpoint": _fresh_endpoint_entry(acct["path"], now_s)}
        )
    return usable


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


def _endpoint_usage(
    endpoint: dict[str, dict[str, Any]] | None, path: str, now: float
) -> tuple[dict[str, Any] | None, str | None]:
    """Usable endpoint usage for a path (fresh enough), plus its error note."""
    entry = (endpoint or {}).get(path)
    if entry is None:
        return None, None
    usage = entry.get("usage")
    if usage is not None and _fresh_endpoint_entry(path, now) is not None:
        return usage, entry.get("err")
    return None, entry.get("err")


def _fields_from_endpoint_usage(usage: dict[str, Any], now: float) -> dict[str, Any]:
    """Build display fields from a fresh /api/oauth/usage reading."""
    # The endpoint has no status field; at/over 100% the window IS rejecting — style it so.
    status_5h = "rejected" if (usage["util_5h"] or 0) >= 1.0 else None
    status_7d = "rejected" if (usage["util_7d"] or 0) >= 1.0 else None
    pace, eta = _pace_eta(usage["util_7d"], usage["reset_7d"], now)
    sonnet_raw, opus_raw = usage["sonnet"], usage["opus"]
    return {
        "src": "endpoint",
        "win5": _window_view(usage["util_5h"], usage["reset_5h"], status_5h, now),
        "win7": _window_view(usage["util_7d"], usage["reset_7d"], status_7d, now),
        "pace": pace,
        "eta": eta,
        "sonnet": _window_view(sonnet_raw["util"], sonnet_raw["reset"], None, now)
        if sonnet_raw
        else None,
        "opus": _window_view(opus_raw["util"], opus_raw["reset"], None, now) if opus_raw else None,
        "extra": usage["extra"],
    }


def _fields_from_proxy_headers(bearer: dict[str, Any] | None, now: float) -> dict[str, Any]:
    """Build display fields from this proxy's last-seen unified headers."""
    unified = (bearer or {}).get("unified") or {}
    pace, eta = _pace_eta(unified.get("util_7d"), unified.get("reset_7d"), now)
    return {
        "src": "proxy" if unified else "none",
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
        "eta": eta,
        "sonnet": None,
        "opus": None,
        "extra": None,
    }


def account_view(
    bearers: list[dict[str, Any]],
    now: float,
    endpoint: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Merge credential files, endpoint truth, and per-bearer proxy state.

    Source precedence per account (the ``src`` field names the winner):
    ``endpoint`` — fresh ``/api/oauth/usage`` reading (account-scoped server
    truth; immune to idle-account staleness and token rotation) →
    ``proxy`` — this proxy's last-seen unified headers for the account's
    current bearer → ``none``. Accounts whose current bearer the proxy has
    not seen still render — "B invisible" was exactly the 10/06 blind spot.
    """
    by_id = {b["bearer_id"]: b for b in bearers}
    out: list[dict[str, Any]] = []
    for acct in account_snapshot():
        bearer = by_id.get(acct["bearer_id"]) if acct["bearer_id"] else None
        usage, endpoint_err = _endpoint_usage(endpoint, acct["path"], now)
        fields = (
            _fields_from_endpoint_usage(usage, now)
            if usage is not None
            else _fields_from_proxy_headers(bearer, now)
        )
        out.append(
            {
                "label": acct["label"],
                "bearer_id": acct["bearer_id"],
                "error": acct["error"],
                "seen": bearer is not None,
                "email": account_email(acct["path"]),
                "endpoint_err": endpoint_err,
                "token": _token_view(acct["expires_at"], now),
                "pace_warn": fields["pace"] is not None and fields["pace"] >= PACE_WARN,
                **fields,
            }
        )
    return out


def bearer_labels() -> dict[str, str]:
    """Reverse map ``bearer_id → account label`` for annotating bearer rows."""
    return {a["bearer_id"]: a["label"] for a in account_snapshot() if a["bearer_id"]}


# --- /api/oauth/usage endpoint truth (PR #55) -------------------------------
# The bearer merge above shows what THIS PROXY last saw — blind to idle
# accounts (frozen last-seen snapshots) and to token rotation, the exact
# 10-11/06 picker incidents. ``GET /api/oauth/usage`` is account-scoped
# server-side truth keyed by the credential itself; NixOS PR #909 moved
# claude-account-pick onto it, and this section gives the dashboard the same
# eyes (plus the profile email, which exposes the 11/06 identity-collapse
# failure where both credential files held the SAME account).
#
# UI-only: refreshed from the dashboard render path and a slow background
# loop in ``attach_ui`` — never from the proxy hot path. The access token is
# read, sent as a header, and dropped (invariant #2: never logged, never
# stored beyond the request).

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
_OAUTH_BETA = "oauth-2025-04-20"

# Fetch cadence mirrors claude-account-pick: 90s TTL collapses dashboard
# polling to ≤1 request per account; readings older than the stale ceiling
# are no better than the proxy's last-seen view and stop overriding it.
ENDPOINT_TTL_S = 90.0
ENDPOINT_STALE_MAX_S = 1800.0
_FETCH_TIMEOUT_S = 6.0

# path -> {"fetched": epoch, "usage": parsed|None, "err": str|None}
_endpoint_cache: dict[str, dict[str, Any]] = {}
# path -> (cred mtime_ns, email). Keyed by credential mtime so a re-/login
# (the 11/06 contamination vector) invalidates the email instantly — the
# 24h-style time cache the picker first shipped hid a collision for up to a
# day (Codex finding, fixed the same way there).
_email_cache: dict[str, tuple[int, str]] = {}
_endpoint_locks: dict[str, asyncio.Lock] = {}


def _iso_epoch(value: Any) -> int | None:
    """ISO-8601 (fractional seconds + offset) → epoch seconds, or None."""
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


def _pct_fraction(value: Any) -> float | None:
    """Endpoint percent (0-100) → internal fraction 0..1, or None.

    The unified response HEADERS carry fractions while this endpoint carries
    percent — the scale drifted across observers once already, so coerce
    defensively rather than assume.
    """
    if not isinstance(value, (int, float)):
        return None
    return max(0.0, float(value)) / 100.0


def _parse_usage(body: dict[str, Any]) -> dict[str, Any]:
    """Endpoint JSON → internal usage dict. Unknown keys / null buckets ok.

    The schema visibly churns (nullable buckets, experimental keys like
    ``tangelo``) — parse only what the panel renders and ignore the rest.
    """

    def window(name: str) -> tuple[float | None, int | None]:
        bucket = body.get(name)
        if not isinstance(bucket, dict):
            return None, None
        return _pct_fraction(bucket.get("utilization")), _iso_epoch(bucket.get("resets_at"))

    util_5h, reset_5h = window("five_hour")
    util_7d, reset_7d = window("seven_day")
    out: dict[str, Any] = {
        "util_5h": util_5h,
        "reset_5h": reset_5h,
        "util_7d": util_7d,
        "reset_7d": reset_7d,
    }
    for key, name in (("sonnet", "seven_day_sonnet"), ("opus", "seven_day_opus")):
        util, reset = window(name)
        out[key] = {"util": util, "reset": reset} if util is not None else None
    extra = body.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used = extra.get("used_credits")
        out["extra"] = {
            "used": float(used) if isinstance(used, (int, float)) else None,
            "currency": str(extra.get("currency") or ""),
        }
    else:
        out["extra"] = None
    return out


def _read_token(path: str) -> str | None:
    """Access token from a credentials file — caller sends + drops it."""
    try:
        with open(path, encoding="utf-8") as fh:
            token = (json.load(fh).get("claudeAiOauth") or {}).get("accessToken")
    except (OSError, ValueError, AttributeError):
        return None
    return token if isinstance(token, str) and token else None


async def _get_json(url: str, token: str) -> tuple[int, dict[str, Any] | None]:
    """One authenticated GET. Returns (status, body|None); 0 = transport error."""
    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S)
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": _OAUTH_BETA}
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers) as resp,
        ):
            if resp.status != 200:
                return resp.status, None
            body = await resp.json(content_type=None)
            return 200, body if isinstance(body, dict) else None
    except (TimeoutError, aiohttp.ClientError):
        return 0, None


async def _refresh_email(path: str, token: str) -> None:
    """Profile email, re-fetched only when the credential file changed."""
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return
    cached = _email_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return
    status, body = await _get_json(PROFILE_URL, token)
    if status == 200 and body:
        email = (body.get("account") or {}).get("email")
        if isinstance(email, str) and email:
            _email_cache[path] = (mtime, email)


async def _refresh_one(path: str, now: float) -> None:
    """TTL-gated, single-flight refresh of one account's endpoint entry."""
    entry = _endpoint_cache.get(path)
    if entry is not None and now - entry["fetched"] < ENDPOINT_TTL_S:
        return
    lock = _endpoint_locks.setdefault(path, asyncio.Lock())
    async with lock:
        entry = _endpoint_cache.get(path)
        if entry is not None and now - entry["fetched"] < ENDPOINT_TTL_S:
            return
        token = _read_token(path)
        if token is None:
            return
        status, body = await _get_json(USAGE_URL, token)
        if status == 200 and body is not None:
            _endpoint_cache[path] = {"fetched": now, "usage": _parse_usage(body), "err": None}
            await _refresh_email(path, token)
        elif status == 401:
            # Credential invalid server-side — surface it and DROP the stale
            # numbers (a dead account's old readings must not style the panel
            # as healthy). The bearer-view fallback still renders.
            _endpoint_cache[path] = {
                "fetched": now,
                "usage": None,
                "err": "credential rejected (401) — refresh pipeline?",
            }
        elif entry is not None:
            # 429/5xx/timeout: keep serving the stale entry (the panel ages
            # it out at the stale ceiling) but mark the failure.
            entry["err"] = f"usage endpoint unavailable ({status or 'timeout'})"
        else:
            _endpoint_cache[path] = {
                "fetched": 0.0,
                "usage": None,
                "err": f"usage endpoint unavailable ({status or 'timeout'})",
            }


async def refresh_endpoint(now: float) -> dict[str, dict[str, Any]]:
    """Refresh all configured accounts; return ``path → cache entry``."""
    paths = [path for _, path in parse_spec(config.ACCOUNT_CRED_PATHS)]
    if paths:
        await asyncio.gather(*(_refresh_one(p, now) for p in paths))
    return {p: _endpoint_cache[p] for p in paths if p in _endpoint_cache}


def account_email(path: str) -> str | None:
    """Best-known profile email for a credential path (may be absent)."""
    cached = _email_cache.get(path)
    return cached[1] if cached else None


def identity_state(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    """Cross-account identity verdict for the panel banner.

    ``collapsed`` when ≥2 accounts have a KNOWN email and they are all the
    same — the 11/06 failure where a hand-/login wrote account B's credential
    into A's directory and every "switch" became cosmetic.
    """
    emails = [a["email"] for a in accounts if a.get("email")]
    collapsed = len(emails) >= 2 and len(set(emails)) == 1
    return {
        "collapsed": collapsed,
        "email": emails[0] if collapsed else None,
        "known": len(emails),
    }
