"""Optional GitHub Copilot panel: per-org plan + seats from the REST API.

``THROTTLE_COPILOT_ORGS=org1,org2`` + a classic PAT with ``read:org`` in
``THROTTLE_COPILOT_TOKEN`` (or ``GITHUB_TOKEN``) reads each org's Copilot
billing summary::

    GET https://api.github.com/orgs/{org}/copilot/billing

The individual-user Copilot usage API does not exist (verified 01/07/2026:
every usage-metrics endpoint is enterprise/org-level and 403s without the
"Copilot usage metrics" enterprise policy). So this panel honestly shows
**subscription + seats** (plan_type, seat breakdown, feature flags) — not
per-day completions. A 404/403 org renders as ``no access`` rather than
faking zero usage.

UI-only: TTL-cached (billing changes slowly) and failure-tolerant — never
imported by the hot path, never breaks the dashboard render.
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import config, ui_http

API = "https://api.github.com"
_BILLING = "/orgs/{org}/copilot/billing"

# Billing changes at seat-assignment cadence, not per-request. 300 s keeps the
# GitHub API load at ≤1 call per org per 5 min regardless of dashboard poll
# rate (the partial re-renders every 2 s).
TTL_S = 300.0
# Failures (403/404/timeout) re-fetch on a shorter window: GitHub 403 covers
# BOTH "token lacks read:org" AND "secondary rate limit" (transient). Serving
# the misleading "lacks read:org" for 300 s after a transient blip would push
# an operator to rotate a perfectly good PAT. 30 s is short enough to recover
# fast, long enough to not hammer during an outage.
FAILURE_TTL_S = 30.0
_FETCH_TIMEOUT_S = 6.0

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_locks: dict[str, asyncio.Lock] = {}


def parse_orgs(raw: str) -> list[str]:
    """Comma-separated org list → ordered, de-duplicated, non-empty slugs."""
    out: list[str] = []
    seen: set[str] = set()
    for org in raw.split(","):
        slug = org.strip()
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _parse_billing(body: Any) -> dict[str, Any]:
    """Billing JSON → display dict. Type-tolerant: a non-dict seat_breakdown
    (schema drift / proxy mangling) coerces to empty, not AttributeError.
    """
    if not isinstance(body, dict):
        return {"ok": False, "err": "non-json billing body"}
    seats = body.get("seat_breakdown")
    if not isinstance(seats, dict):
        seats = {}

    def _int(key: str) -> int:
        try:
            return int(seats.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "ok": True,
        "plan_type": str(body.get("plan_type") or "—"),
        "seats_total": _int("total"),
        "seats_active": _int("active_this_cycle"),
        "seats_inactive": _int("inactive_this_cycle"),
        "seats_pending": _int("pending_invitation"),
        "seat_management": str(body.get("seat_management_setting") or "—"),
        "ide_chat": str(body.get("ide_chat") or "—"),
        "cli": str(body.get("cli") or "—"),
        "platform_chat": str(body.get("platform_chat") or "—"),
    }


async def _fetch_billing(org: str, token: str) -> tuple[int, Any]:
    """One authenticated GET. Thin wrapper over :func:`ui_http.get_json` so
    tests monkeypatch this seam. ``body`` is None on any parse failure so the
    caller never sees a raw exception from a malformed response.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "anthropic-throttle-proxy/ui",
    }
    return await ui_http.get_json(
        API + _BILLING.format(org=org), headers=headers, timeout_s=_FETCH_TIMEOUT_S
    )


def _cache_hit(key: str, now: float) -> dict[str, Any] | None:
    """Return a fresh-enough cached view, or None. Encodes the failure-TTL:
    a cached failure re-fetches on the shorter window so a transient 403
    doesn't mislead for the full 300 s success TTL.
    """
    cached = _cache.get(key)
    if cached is None:
        return None
    ttl = FAILURE_TTL_S if not cached[1].get("ok") else TTL_S
    return cached[1] if now - cached[0] < ttl else None


async def _refresh_one(org: str, token: str, now: float) -> dict[str, Any]:
    """TTL-gated, single-flight refresh of one org's billing → display row."""
    hit = _cache_hit(org, now)
    if hit is not None:
        return {"org": org, **hit}
    lock = _locks.setdefault(org, asyncio.Lock())
    async with lock:
        hit = _cache_hit(org, now)
        if hit is not None:
            return {"org": org, **hit}
        status, body = await _fetch_billing(org, token)
        if status == 200 and isinstance(body, dict):
            view = _parse_billing(body)
        elif status == 200:
            view = {"ok": False, "status": 200, "err": "non-json billing body"}
        elif status == 401:
            view = {"ok": False, "status": 401, "err": "bad/expired token — refresh the PAT"}
        elif status == 403:
            view = {"ok": False, "status": 403, "err": "lacks read:org (or secondary rate limit)"}
        elif status == 404:
            view = {"ok": False, "status": 404, "err": "no Copilot for this org"}
        else:
            view = {
                "ok": False,
                "status": status,
                "err": "unreachable" if status == 0 else f"http {status}",
            }
        _cache[org] = (now, view)
        return {"org": org, **view}


async def refresh(now: float) -> list[dict[str, Any]]:
    """Refresh every configured org; return display rows in env order.

    No-op (empty list) when ``THROTTLE_COPILOT_ORGS`` is unset OR the token is
    absent — the panel stays hidden, matching how an unset account panel hides.
    """
    orgs = parse_orgs(config.COPILOT_ORGS)
    if not orgs or not config.COPILOT_TOKEN:
        return []
    rows = await asyncio.gather(*(_refresh_one(o, config.COPILOT_TOKEN, now) for o in orgs))
    return list(rows)
