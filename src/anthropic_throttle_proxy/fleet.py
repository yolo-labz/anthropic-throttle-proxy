"""Optional fleet strip: sibling-proxy ``/__throttle/health`` cross-fetch.

``THROTTLE_FLEET_HEALTH`` names sibling proxies (e.g. the z.ai coding-plan
instance on ``:8766``) so the Anthropic-side dashboard renders the whole
fleet in one pane::

    THROTTLE_FLEET_HEALTH=z.ai:http://127.0.0.1:8766/__throttle/health

UI-only surface: the hot path never imports this module. Every fetch is
failure-tolerant — a down sibling renders as ``ok=False`` and never breaks
the dashboard render (a broken /ui must not break /v1/messages, invariant
#1's larger cousin). TTL-cached + single-flight so a 2 s dashboard poll
collapses to ≤1 cross-fetch per sibling per TTL window.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from . import config

# Cross-fetch TTL. The dashboard partial re-renders every 2 s; without a cache
# that would be one sibling hit per render per sibling. 5 s halves the load on
# the sibling proxy and coalesces concurrent renders (two browser tabs).
TTL_S = 5.0
_FETCH_TIMEOUT_S = 1.5

# url -> (fetched_epoch, view). Pure in-memory; a process restart re-warms on
# the next dashboard render. Read by tests via ``_cache`` (cleared in fixtures).
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_locks: dict[str, asyncio.Lock] = {}


def parse_spec(raw: str) -> list[tuple[str, str]]:
    """Parse ``LABEL:url,LABEL:url`` into ordered (label, url) pairs.

    Mirrors :func:`accounts.parse_spec`: malformed entries (no colon, empty
    label/url, duplicate labels) are skipped; never raises — a bad env var
    must not take down the dashboard.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in raw.split(","):
        label, sep, url = entry.strip().partition(":")
        label, url = label.strip(), url.strip()
        # Restore the scheme the partition consumed (label is the bare prefix
        # before the first colon; the URL keeps its own http:// colon). parse
        # as "name|url" would be cleaner, but accounts.py uses the same
        # colon-first shape, so stay consistent.
        if not sep or not label or not url:
            continue
        if label in seen:
            continue
        seen.add(label)
        out.append((label, url))
    return out


def _parse_health(body: Any) -> dict[str, Any]:
    """Flatten the sibling's ``/__throttle/health`` JSON to display fields.

    Only the stable, fleet-relevant fields are kept — the dashboard does not
    need the full per-bearer tree (that lives on the sibling's own /ui).
    """
    if not isinstance(body, dict):
        return {"ok": False, "status": 200, "err": "non-json health body"}
    return {
        "ok": True,
        "status": 200,
        "inflight": int(body.get("inflight", 0)),
        "queued": int(body.get("queued", 0)),
        "served": int(body.get("served", 0)),
        "max_concurrent": int(body.get("max_concurrent", 0)),
        "queue_mode": str(body.get("queue_mode", "")),
        "upstream": str(body.get("upstream", "")),
        "upstream_egress_ok": bool(body.get("upstream_egress_ok", False)),
        "client_disconnects": int(body.get("client_disconnects", 0)),
        "upstream_retries": int(body.get("upstream_retries", 0)),
    }


async def _fetch_json(url: str) -> tuple[int, Any]:
    """One GET against a sibling health endpoint. Returns (status, body|None).

    0 status = transport error / timeout. The token-less health endpoint is
    public loopback, so no Authorization header is needed.
    """
    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url) as resp,
        ):
            if resp.status != 200:
                return resp.status, None
            return 200, await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError):
        return 0, None


async def _refresh_one(name: str, url: str, now: float) -> dict[str, Any]:
    """TTL-gated, single-flight refresh of one sibling's health → display row."""
    cached = _cache.get(url)
    if cached is not None and now - cached[0] < TTL_S:
        return {"name": name, "url": url, **cached[1]}
    lock = _locks.setdefault(url, asyncio.Lock())
    async with lock:
        cached = _cache.get(url)
        if cached is not None and now - cached[0] < TTL_S:
            return {"name": name, "url": url, **cached[1]}
        status, body = await _fetch_json(url)
        if status == 200 and body is not None:
            view = _parse_health(body)
        else:
            view = {
                "ok": False,
                "status": status,
                "err": "sibling unreachable" if status == 0 else f"http {status}",
            }
        _cache[url] = (now, view)
        return {"name": name, "url": url, **view}


async def refresh(now: float) -> list[dict[str, Any]]:
    """Refresh every configured sibling; return display rows in env order."""
    pairs = parse_spec(config.FLEET_HEALTH_URLS)
    if not pairs:
        return []
    rows = await asyncio.gather(*(_refresh_one(name, url, now) for name, url in pairs))
    return list(rows)
