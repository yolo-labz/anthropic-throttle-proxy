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
import os
from typing import Any

import aiohttp

from . import config

API = "https://api.github.com"
_BILLING = "/orgs/{org}/copilot/billing"

# Billing changes at seat-assignment cadence, not per-request. 300 s keeps the
# GitHub API load at ≤1 call per org per 5 min regardless of dashboard poll
# rate (the partial re-renders every 2 s).
TTL_S = 300.0
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
    """Billing JSON → display dict. Tolerates schema drift (nullable buckets)."""
    if not isinstance(body, dict):
        return {"ok": False, "err": "non-json billing body"}
    seats = body.get("seat_breakdown") or {}
    return {
        "ok": True,
        "plan_type": str(body.get("plan_type") or "—"),
        "seats_total": int(seats.get("total") or 0),
        "seats_active": int(seats.get("active_this_cycle") or 0),
        "seats_inactive": int(seats.get("inactive_this_cycle") or 0),
        "seats_pending": int(seats.get("pending_invitation") or 0),
        "seat_management": str(body.get("seat_management_setting") or "—"),
        "ide_chat": str(body.get("ide_chat") or "—"),
        "cli": str(body.get("cli") or "—"),
        "platform_chat": str(body.get("platform_chat") or "—"),
    }


async def _fetch_billing(org: str, token: str) -> tuple[int, Any]:
    """One authenticated GET. Returns (status, body|None); 0 = transport error."""
    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "anthropic-throttle-proxy/ui",
    }
    url = API + _BILLING.format(org=org)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers) as resp,
        ):
            if resp.status != 200:
                return resp.status, None
            return 200, await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError):
        return 0, None


async def _refresh_one(org: str, token: str, now: float) -> dict[str, Any]:
    """TTL-gated, single-flight refresh of one org's billing → display row."""
    cached = _cache.get(org)
    if cached is not None and now - cached[0] < TTL_S:
        return {"org": org, **cached[1]}
    lock = _locks.setdefault(org, asyncio.Lock())
    async with lock:
        cached = _cache.get(org)
        if cached is not None and now - cached[0] < TTL_S:
            return {"org": org, **cached[1]}
        status, body = await _fetch_billing(org, token)
        if status == 200 and body is not None:
            view = _parse_billing(body)
        elif status in (401, 403):
            view = {"ok": False, "status": status, "err": "token lacks read:org"}
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
    token = os.environ.get("THROTTLE_COPILOT_TOKEN", "") or config.COPILOT_TOKEN
    if not orgs or not token:
        return []
    rows = await asyncio.gather(*(_refresh_one(o, token, now) for o in orgs))
    return list(rows)
