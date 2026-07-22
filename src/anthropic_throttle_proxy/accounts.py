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
import json
import os
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from . import config
from . import limiter as _limiter
from .ratelimit import _bearer_id

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
    bid = _token_bearer_id(token)
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


def _fields_from_cache_usage(usage: dict[str, Any], now: float) -> dict[str, Any]:
    """Build display fields from an AGED persisted /api/oauth/usage reading.

    Mirrors ``_fields_from_endpoint_usage`` but stamps ``src="cache"`` — the
    lowest-precedence tier that unblanks a budget-locked, un-routed account
    after a proxy restart wiped the in-memory endpoint cache (#128). Every
    window still routes through ``_window_view``, so a rolled-over window
    renders "0% · reset" (honest) while an open one shows real aged util +
    true reset. ``_fields_from_endpoint_usage`` is not reused directly only
    because it hard-codes ``src="endpoint"``.
    """
    fields = _fields_from_endpoint_usage(usage, now)
    fields["src"] = "cache"
    return fields


def _bearer_uncapped_retry_after(bearer: dict[str, Any] | None, now: float) -> float:
    """Remaining seconds on the bearer's UNCAPPED live Retry-After window.

    Reads the raw ``anthropic-ratelimit`` ``retry-after`` captured verbatim into
    ``last_ratelimit`` (e.g. 201223 s for a budget lock) — NOT the 900 s-capped
    persisted limiter state ``_retry_after_remaining_for_token`` reads, which
    would expire ~15 min after a restart and leave a still-locked account blank.
    """
    lr = (bearer or {}).get("last_ratelimit")
    ra = _parse_retry_after(lr.get("retry-after")) if isinstance(lr, dict) else None
    return ra if ra is not None else 0.0


def _locked_in(fields: dict[str, Any], bearer: dict[str, Any] | None, now: float) -> str | None:
    """Duration string a budget-locked account is locked FOR, or None.

    A budget-locked account with no usable usage must still show it IS locked
    (not six "—"). Source priority: the persisted/endpoint 7d reset (``win7``
    ``reset_in``) when present, ELSE the bearer's uncapped live Retry-After — a
    DURATION STRING, never a percentage.
    """
    win7 = fields.get("win7")
    if isinstance(win7, dict) and win7.get("reset_in"):
        return win7["reset_in"]
    remaining = _bearer_uncapped_retry_after(bearer, now)
    return _fmt_duration(remaining) if remaining > 0 else None


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
    current bearer → ``cache`` — an AGED persisted endpoint reading (survives a
    restart that wiped the in-memory cache; #128) → ``none``. Accounts whose
    current bearer the proxy has not seen still render — "B invisible" was
    exactly the 10/06 blind spot, and a budget-locked un-routed account after a
    restart is the #128 blind spot (six "—" + a cold-poll 429 note).
    """
    by_id = {b["bearer_id"]: b for b in bearers}
    out: list[dict[str, Any]] = []
    for acct in account_snapshot():
        bearer = by_id.get(acct["bearer_id"]) if acct["bearer_id"] else None
        usage, endpoint_err = _endpoint_usage(endpoint, acct["path"], now)
        if usage is not None:
            fields = _fields_from_endpoint_usage(usage, now)
        else:
            fields = _fields_from_proxy_headers(bearer, now)
            if fields["src"] == "none":
                # No fresh endpoint, no unified header — fall back to the AGED
                # persisted reading so a restart-blanked account still renders.
                cached = _endpoint_cache.get(acct["path"])
                cached_usage = cached.get("usage") if isinstance(cached, dict) else None
                if isinstance(cached_usage, dict):
                    fields = _fields_from_cache_usage(cached_usage, now)
                    fetched = cached.get("fetched")
                    if isinstance(fetched, (int, float)):
                        fields["cache_age"] = _fmt_duration(now - fetched)
        email, email_verified = guard_email(acct["path"])
        out.append(
            {
                "label": acct["label"],
                "bearer_id": acct["bearer_id"],
                "error": acct["error"],
                "seen": bearer is not None,
                "email": email,
                # identity_state provenance — a local .claude.json label must
                # not be treated as a probed identity (promote-swap false alarm)
                "verified": email_verified,
                "endpoint_err": endpoint_err,
                "token": _token_view(acct["expires_at"], now),
                "pace_warn": fields["pace"] is not None and fields["pace"] >= PACE_WARN,
                "locked_in": _locked_in(fields, bearer, now),
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

_DIRECT_OAUTH_BASE = "https://api.anthropic.com"
_USAGE_PATH = "/api/oauth/usage"
_PROFILE_PATH = "/api/oauth/profile"
_OAUTH_BETA = "oauth-2025-04-20"


def _oauth_base() -> str:
    """Base URL for the OAuth account endpoints (usage/profile).

    A LOCAL tier (has account credentials) routes these polls through its OWN
    loopback so they pass the per-bearer fair-queue semaphore. A DIRECT call
    bypasses admission and bursts alongside that account's real traffic,
    tripping its concurrency cap (429) — pane-19 finding 09/07: every direct
    ``/api/oauth/usage`` 429s while proxy-routed calls return data. The central
    tier (no accounts) has no local semaphore to gain and calls direct. The GET
    is not ``POST /v1/messages`` so account-routing never rewrites its bearer —
    the poll still reports the token's OWN account.
    """
    if config.ACCOUNT_CRED_PATHS:
        return f"http://127.0.0.1:{config.LISTEN_PORT}"
    return _DIRECT_OAUTH_BASE


# Fetch cadence mirrors claude-account-pick: 90s TTL collapses dashboard
# polling to ≤1 request per account; readings older than the stale ceiling
# are no better than the proxy's last-seen view and stop overriding it.
ENDPOINT_TTL_S = 90.0
ENDPOINT_STALE_MAX_S = 1800.0
_FETCH_TIMEOUT_S = 6.0

# path -> {"fetched": epoch, "usage": parsed|None, "err": str|None}
_endpoint_cache: dict[str, dict[str, Any]] = {}
# Seeded from disk once so a just-restarted proxy has an AGED usage reading to
# fall back on for a budget-locked, un-routed account (otherwise the cold 429
# poll blanks the panel — issue #128). Set True after the first load attempt.
_endpoint_cache_loaded = False
# path -> earliest-next-poll epoch. Set after a FAILED usage poll so a
# throttled/dead account is not re-polled on every dashboard render. Without
# it, the failure branch below leaves ``fetched`` stale, the TTL gate never
# suppresses, and the HTMX panel's ~2 s auto-refresh drives one poll per
# render — a self-inflicted 429 loop that steals loopback slots from real
# traffic during exactly the storm the operator is trying to ride out
# (observed 10/07: 46 min of 2 s-cadence retry-after-fast-fail on a bearer
# already in a 2 700 s Retry-After window). Kept SEPARATE from ``fetched`` so
# staleness display (``account_view``) stays honest — this gates *attempts*,
# not *freshness*.
_endpoint_backoff: dict[str, float] = {}
# path -> (cred mtime_ns, email). Keyed by credential mtime so a re-/login
# (the 11/06 contamination vector) invalidates the email instantly — the
# 24h-style time cache the picker first shipped hid a collision for up to a
# day (Codex finding, fixed the same way there).
_email_cache: dict[str, tuple[int, str]] = {}
# path -> (cred mtime_ns, locally-persisted account email). Fallback identity
# for the distinctness guard: the authoritative profile email (_email_cache)
# needs a LIVE token, so a DEAD account (expired + refresh revoked) never
# resolves it — exactly when a duplicate-account collision does its damage
# (09/07 outage: two dirs both on pedrobalbino@pm.me, both expired, the
# collision invisible to the network identity). The CLI persists the account
# email next to the credential (``<dir>/.claude.json`` → oauthAccount.
# emailAddress); reading it keeps the collision detectable with no token.
_local_identity_cache: dict[str, tuple[int, int, str]] = {}  # (cred_mtime, ident_mtime, email)
_endpoint_locks: dict[str, asyncio.Lock] = {}
# Per-path singleflight for force_verify_email: suspected-set churn can spawn
# overlapping verification tasks that would otherwise probe the SAME credential
# concurrently through the loopback (the #87 self-429 class — Codex MAJOR).
_verify_locks: dict[str, asyncio.Lock] = {}

JsonResult = tuple[int, dict[str, Any] | None] | tuple[int, dict[str, Any] | None, float | None]


def _load_endpoint_cache() -> None:
    """Seed ``_endpoint_cache`` from disk once. Never raises into callers.

    Mirrors ``limiter._load_retry_after_state``: a corrupt/missing file is a
    no-op (empty seed). Only ``{path: {"fetched", "usage"}}`` is restored —
    "err" is transient and a token was never persisted (invariant #2). Aged
    entries seed ``account_view``'s cache tier; the endpoint-fresh gate
    (``_fresh_endpoint_entry``) still ages them out of the endpoint tier.
    """
    global _endpoint_cache_loaded
    _endpoint_cache_loaded = True
    try:
        raw = json.loads(config.ENDPOINT_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    for path, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        fetched, usage = entry.get("fetched"), entry.get("usage")
        if isinstance(fetched, (int, float)) and isinstance(usage, dict):
            _endpoint_cache.setdefault(str(path), {"fetched": float(fetched), "usage": usage})


def _persist_endpoint_cache() -> None:
    """Atomically persist the endpoint cache. Never raises into callers.

    Copies ``limiter._persist_retry_after_state``'s tmp-write + ``os.replace``.
    Persists ONLY ``{path: {"fetched", "usage"}}`` — "err" is transient and the
    token is never written (usage numbers only, keyed by cred PATH).
    """
    try:
        live = {
            p: {"fetched": e["fetched"], "usage": e["usage"]}
            for p, e in _endpoint_cache.items()
            if isinstance(e.get("usage"), dict) and isinstance(e.get("fetched"), (int, float))
        }
        path = config.ENDPOINT_CACHE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(live, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, ValueError) as exc:
        config.log(f"endpoint-cache-write-error path={config.ENDPOINT_CACHE_FILE} err={exc!r}")


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
    out["limits"] = _parse_limits(body.get("limits"))
    # The weekly per-model (scoped) meter — the one that flips Fable→Sonnet as
    # the client-side model mix shifts; spec-2/3 model-aware routing keys on it.
    # Prefer the ACTIVE scoped entry (Codex MEDIUM: multiple weekly_scoped
    # entries are possible; the binding one is what routing/warnings care about),
    # falling back to the first when none is flagged active.
    scoped_limits = [lim for lim in out["limits"] if lim["kind"] == "weekly_scoped"]
    out["scoped"] = next((lim for lim in scoped_limits if lim["is_active"]), None) or (
        scoped_limits[0] if scoped_limits else None
    )
    return out


def _parse_limits(raw: Any) -> list[dict[str, Any]]:
    """Parse the ``/api/oauth/usage`` ``limits[]`` array (ccusage shape).

    Each entry: ``kind`` (session|weekly_all|weekly_scoped), ``group``,
    ``util`` (0..1 from the 0-100 ``percent``), ``severity``
    (normal|warning|critical — the endpoint's OWN classification, richer than a
    hardcoded threshold), ``is_active`` (which window binds now), and ``model``
    (display name, only present on ``weekly_scoped``). Never raises on schema
    churn — unknown/malformed entries are skipped.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        model = None
        scope = entry.get("scope")
        if isinstance(scope, dict):
            m = scope.get("model")
            if isinstance(m, dict) and isinstance(m.get("display_name"), str):
                model = m["display_name"]
        out.append(
            {
                "kind": str(entry.get("kind") or ""),
                "group": str(entry.get("group") or ""),
                "util": _pct_fraction(entry.get("percent")),
                "severity": str(entry.get("severity") or ""),
                "is_active": bool(entry.get("is_active")),
                "model": model,
            }
        )
    return out


def _read_token(path: str) -> str | None:
    """Access token from a credentials file — caller sends + drops it."""
    try:
        with open(path, encoding="utf-8") as fh:
            token = (json.load(fh).get("claudeAiOauth") or {}).get("accessToken")
    except (OSError, ValueError, AttributeError):
        return None
    return token if isinstance(token, str) and token else None


def _token_bearer_id(token: str) -> str:
    """Bearer id for a raw OAuth access token."""
    return _bearer_id({"Authorization": f"Bearer {token}"})


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After header seconds, or None when absent/malformed."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, parsed.timestamp() - datetime.now(tz=UTC).timestamp())


def _json_result_parts(result: JsonResult) -> tuple[int, dict[str, Any] | None, float | None]:
    """Accept old 2-tuples from tests and new 3-tuples from real HTTP."""
    status, body = result[0], result[1]
    retry_after = result[2] if len(result) >= 3 else None
    return status, body, retry_after


def _failed_poll_backoff_until(now: float, retry_after: float | None) -> float:
    """Next usage-poll attempt time after a failed fetch."""
    return now + max(ENDPOINT_TTL_S, retry_after or 0.0)


# Poll cadence while the bearer's MESSAGES Retry-After window is active. The
# messages window can legitimately run for hours (budget exhaustion until the
# 5h/7d reset), but the usage endpoint is a SEPARATE rate-limit domain whose
# own 429s are already honored via _endpoint_backoff — and its readings are the
# one signal that can contradict a stale window. Skipping polls for the whole
# window (the pre-#103 behavior) starved the panel/router of exactly that
# evidence for 58 h on 13-14/07.
_RETRY_AFTER_GATE_CAP_S = 300.0

# Clear a messages Retry-After window on contradicting usage evidence only when
# it still has at least this long to run. Short windows are input-bucket rate
# pushback (60-120 s observed) that utilization figures say nothing about —
# those must expire on their own. Hours-long windows are budget-scale and the
# 5h/7d utilization IS their ground truth.
_RETRY_AFTER_CLEAR_MIN_REMAINING_S = 600.0


def _retry_after_remaining_for_token(token: str, now: float) -> float:
    """Persisted proxy Retry-After remaining for this credential token."""
    until = _limiter._load_retry_after_state().get(_token_bearer_id(token), 0.0)
    return max(0.0, float(until) - now) if isinstance(until, (int, float)) else 0.0


def _maybe_clear_stale_retry_after(token: str, usage: dict[str, Any], now: float) -> None:
    """Drop a long messages window contradicted by fresh usage evidence.

    A budget window is noted at 100% exhaustion and ends at the reset epoch,
    but the rolling 5h/7d usage decays underneath it. Fresh sub-exhaustion
    utilization on BOTH windows means the account serves again; holding the
    window concentrates the fleet on the remaining accounts (13-14/07: two
    accounts at 91-92% blocked ~58 h → one fresh account absorbed everything,
    tripped its concurrency cap, and overflow fast-failed). Unknown utilization
    is never coerced to "fine" — both windows must report a number below 1.0.
    The next real 429 re-notes an honest window, so a wrong clear costs one
    upstream round-trip.
    """
    util_5h, util_7d = usage.get("util_5h"), usage.get("util_7d")
    if not isinstance(util_5h, (int, float)) or not isinstance(util_7d, (int, float)):
        return
    if util_5h >= 1.0 or util_7d >= 1.0:
        return
    bid = _token_bearer_id(token)
    until = _limiter._load_retry_after_state().get(bid, 0.0)
    lim = config.bearer_limiters.get(bid)
    if lim is not None:
        until = max(until, lim._retry_after_until)
    if until - now < _RETRY_AFTER_CLEAR_MIN_REMAINING_S:
        return
    cleared = _limiter.clear_retry_after(bid)
    if cleared > 0:
        config.log(
            f"retry-after-cleared bid={bid} remaining={int(cleared)}s "
            f"reason=usage-decay util_5h={util_5h:.2f} util_7d={util_7d:.2f}"
        )


async def _get_json(url: str, token: str) -> JsonResult:
    """One authenticated GET. Returns (status, body|None[, retry_after]); 0 = transport error."""
    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S)
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": _OAUTH_BETA}
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers) as resp,
        ):
            if resp.status != 200:
                return resp.status, None, _parse_retry_after(resp.headers.get("retry-after"))
            body = await resp.json(content_type=None)
            return 200, body if isinstance(body, dict) else None, None
    except (TimeoutError, aiohttp.ClientError):
        return 0, None


async def _refresh_email(path: str, token: str, expect_mtime: int | None = None) -> None:
    """Profile email, re-fetched only when the credential file changed.

    ``expect_mtime`` is the credential mtime observed when ``token`` was READ.
    The probe result is cached only while the file still matches it — and is
    re-checked AFTER the network round-trip — so a rotation landing mid-probe
    can never certify the OLD token's email against the NEW file (Codex MAJOR:
    the guard would mark a wrong identity as verified).
    """
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return
    if expect_mtime is not None and mtime != expect_mtime:
        return  # credential rewritten since the token was read — don't certify
    cached = _email_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return
    status, body, _retry_after = _json_result_parts(
        await _get_json(_oauth_base() + _PROFILE_PATH, token)
    )
    if status == 200 and body:
        email = (body.get("account") or {}).get("email")
        if isinstance(email, str) and email:
            try:
                if os.stat(path).st_mtime_ns != mtime:
                    return  # rotated mid-probe — this email belongs to the OLD token
            except OSError:
                return
            _email_cache[path] = (mtime, email)


async def _refresh_one(path: str, now: float) -> None:
    """TTL-gated, single-flight refresh of one account's endpoint entry."""
    if not _endpoint_cache_loaded:
        _load_endpoint_cache()  # seed aged readings before the first cold poll
    entry = _endpoint_cache.get(path)
    if entry is not None and now - entry["fetched"] < ENDPOINT_TTL_S:
        return
    if now < _endpoint_backoff.get(path, 0.0):
        return  # backing off after a recent poll failure — serve the cached entry
    lock = _endpoint_locks.setdefault(path, asyncio.Lock())
    async with lock:
        entry = _endpoint_cache.get(path)
        if entry is not None and now - entry["fetched"] < ENDPOINT_TTL_S:
            return
        if now < _endpoint_backoff.get(path, 0.0):
            return
        try:
            token_mtime = os.stat(path).st_mtime_ns
        except OSError:
            token_mtime = None
        token = _read_token(path)
        if token is None:
            return
        retry_after_remaining = _retry_after_remaining_for_token(token, now)
        status, body, retry_after = _json_result_parts(
            await _get_json(_oauth_base() + _USAGE_PATH, token)
        )
        if status == 200 and body is not None:
            usage = _parse_usage(body)
            _endpoint_cache[path] = {"fetched": now, "usage": usage, "err": None}
            _persist_endpoint_cache()  # aged fallback survives a restart (#128)
            if retry_after_remaining > 0:
                # Messages window active on this bearer: poll anyway (separate
                # rate-limit domain, and the only evidence that can contradict
                # a stale window) but at a relaxed cadence so a long window is
                # never hammered by UI auto-refresh.
                _endpoint_backoff[path] = now + min(
                    max(ENDPOINT_TTL_S, retry_after_remaining), _RETRY_AFTER_GATE_CAP_S
                )
            else:
                _endpoint_backoff.pop(path, None)  # recovered — resume normal TTL cadence
            _maybe_clear_stale_retry_after(token, usage, now)
            await _refresh_email(path, token, expect_mtime=token_mtime)
        elif status == 401:
            # Credential invalid server-side — surface it and DROP the stale
            # numbers (a dead account's old readings must not style the panel
            # as healthy). The bearer-view fallback still renders.
            _endpoint_cache[path] = {
                "fetched": now,
                "usage": None,
                "err": "credential rejected (401) — refresh pipeline?",
            }
            _persist_endpoint_cache()  # tombstone: a dead cred cannot re-seed
            # aged numbers after a restart (persist drops the usage-less entry)
            _endpoint_backoff[path] = _failed_poll_backoff_until(now, retry_after)
        elif entry is not None:
            # 429/5xx/timeout: keep serving the stale entry (the panel ages
            # it out at the stale ceiling) but mark the failure and back off so
            # the dashboard's auto-refresh cannot re-poll a throttled account
            # every render. Honor a proxy/upstream Retry-After when present;
            # otherwise a 90 s retry cadence rediscovers long OAuth windows and
            # creates the same 429 noise every UI refresh cycle.
            entry["err"] = f"usage endpoint unavailable ({status or 'timeout'})"
            _endpoint_backoff[path] = _failed_poll_backoff_until(now, retry_after)
        else:
            _endpoint_cache[path] = {
                "fetched": 0.0,
                "usage": None,
                "err": f"usage endpoint unavailable ({status or 'timeout'})",
            }
            _endpoint_backoff[path] = _failed_poll_backoff_until(now, retry_after)


async def refresh_endpoint(now: float) -> dict[str, dict[str, Any]]:
    """Refresh all configured accounts; return ``path → cache entry``."""
    paths = [path for _, path in parse_spec(config.ACCOUNT_CRED_PATHS)]
    if paths:
        await asyncio.gather(*(_refresh_one(p, now) for p in paths))
    return {p: _endpoint_cache[p] for p in paths if p in _endpoint_cache}


def _local_identity(path: str) -> str | None:
    """Locally-persisted account email for a credential path, or None.

    Read from the sibling ``.claude.json`` (``oauthAccount.emailAddress``),
    cached on BOTH files' mtimes (credential AND ``.claude.json``): a re-/login
    rewrites both, but tracking both invalidates the moment the identity file
    changes even if the credential mtime somehow lags (adversarial-review MEDIUM
    #1 — the guard is a correctness surface, so it must never serve a stale
    email). Re-parsing on a ``.claude.json`` session-state write is cheap and
    tolerable because this is OFF the request hot path — only the UI/health/
    identity callers use it, never ``routing_snapshot``. Never raises.
    """
    try:
        cred_mtime = os.stat(path).st_mtime_ns
    except OSError:
        return None
    ident_path = os.path.join(os.path.dirname(path), ".claude.json")
    try:
        ident_mtime = os.stat(ident_path).st_mtime_ns
    except OSError:
        return None
    cached = _local_identity_cache.get(path)
    if cached is not None and cached[0] == cred_mtime and cached[1] == ident_mtime:
        return cached[2]
    try:
        with open(ident_path, encoding="utf-8") as fh:
            account = json.load(fh).get("oauthAccount") or {}
    except (OSError, ValueError, AttributeError):
        return None
    email = account.get("emailAddress")
    if isinstance(email, str) and email:
        _local_identity_cache[path] = (cred_mtime, ident_mtime, email)
        return email
    return None


def guard_email(path: str) -> tuple[str | None, bool]:
    """Best-known account email for a credential path + whether it is VERIFIED.

    Prefers the authoritative profile email, but ONLY while its cached
    credential mtime still matches: after a re-/login the network email is stale
    until the endpoint refresher re-fetches, and serving it would let the guard
    miss or falsely report a collision (Codex MAJOR). On a mtime mismatch, drop
    the stale entry and fall back to the locally-persisted identity, which
    tracks the current file — also the path that keeps a DEAD account's
    duplicate collision detectable (the case the network-only path missed 09/07).

    ``verified=True`` means the email was probed from the CURRENT credential
    (profile cache, mtime-fresh). ``verified=False`` means it is the local
    ``.claude.json`` label — which LIES when something rewrites the credential
    without the label: claude-account-promote swaps ``.credentials.json``
    between stores and leaves both ``.claude.json`` behind (10/07: swap at
    10:22:07 → guard mis-read A+B→pm.me for ~2.6 h while live uuid probes
    showed three distinct accounts). Collision verdicts must not alarm on
    unverified emails without probing first — see ``identity_state``.
    """
    cached = _email_cache.get(path)
    if cached is not None:
        try:
            fresh = os.stat(path).st_mtime_ns == cached[0]
        except OSError:
            fresh = False  # credential vanished → treat the cached email as stale
        if fresh:
            return cached[1], True
        _email_cache.pop(path, None)  # stale (credential rewritten) — refetched later
    return _local_identity(path), False


def account_email(path: str) -> str | None:
    """Best-known account email for a credential path (display convenience)."""
    return guard_email(path)[0]


async def force_verify_email(path: str) -> str | None:
    """Probe ``/api/oauth/profile`` for THIS credential now, bypassing caches.

    The verify-before-warn collision path: a SUSPECTED duplicate (one member's
    email is only a local label) must be confirmed against the live token
    before the guard alarms or the promote rail refuses swaps on it. Returns
    the verified email, or None when the token is unreadable or the probe
    failed (dead account / throttled) — the caller decides how loudly to warn
    about an unverified suspicion.

    Singleflight per path: overlapping verification tasks (suspected-set churn)
    serialize here, and a probe another task just completed is reused instead
    of re-hitting the endpoint. Token + mtime are snapshotted together so a
    rotation mid-probe can never certify the old token's email as the new
    file's verified identity (``_refresh_email`` re-checks after the probe).
    """
    lock = _verify_locks.setdefault(path, asyncio.Lock())
    async with lock:
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return None
        cached = _email_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]  # already verified for the CURRENT credential
        token = _read_token(path)
        if token is None:
            return None
        _email_cache.pop(path, None)  # force _refresh_email past its mtime short-circuit
        await _refresh_email(path, token, expect_mtime=mtime)
        cached = _email_cache.get(path)
        return cached[1] if cached is not None else None


def identity_state(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    """Cross-account identity verdict for the panel banner + health surface.

    ``collapsed`` when ≥2 accounts have a KNOWN email and they are all the
    same — the 11/06 failure where a hand-/login wrote account B's credential
    into A's directory and every "switch" became cosmetic. ``duplicates`` maps
    each shared email to the labels resolving to it — the richer signal the
    09/07 outage needed (two dirs both on pedrobalbino@pm.me while a third was
    distinct: not fully ``collapsed`` but still a mutually-revoking collision).
    Distinct-account routing (FR-005) requires ``duplicates`` to stay empty.

    Rows may carry ``verified`` (``guard_email`` provenance; absent = True for
    back-compat). A shared-email group with ANY unverified member lands in
    ``suspected``, not ``duplicates``: an unverified email is a ``.claude.json``
    label that lies after a promote credential swap (10/07 false alarm), so the
    guard must probe the live tokens before treating the group as a real
    mutually-revoking collision. ``collapsed`` likewise requires the group to
    be fully verified — while a suspicion is pending, the state is UNKNOWN,
    not collapsed and not distinct.
    """
    by_email: dict[str, list[str]] = {}
    unverified: set[str] = set()
    for a in accounts:
        email = a.get("email")
        if email:
            label = str(a.get("label") or "?")
            by_email.setdefault(email, []).append(label)
            if not a.get("verified", True):
                unverified.add(label)
    known = sum(len(labels) for labels in by_email.values())
    groups = {e: sorted(labels) for e, labels in by_email.items() if len(labels) > 1}
    duplicates = {
        e: labels for e, labels in groups.items() if not any(lb in unverified for lb in labels)
    }
    suspected = {
        e: labels for e, labels in groups.items() if any(lb in unverified for lb in labels)
    }
    collapsed = known >= 2 and len(by_email) == 1 and not suspected
    return {
        "collapsed": collapsed,
        "email": next(iter(by_email)) if collapsed else None,
        "known": known,
        "distinct": len(by_email),
        "duplicates": duplicates,
        "suspected": suspected,
    }
