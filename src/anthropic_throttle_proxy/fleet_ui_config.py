"""Declarative fleet-UI config — what the dashboard's Subscriptions table
renders, driven by a YAML file instead of hardcoded Python.

Design (Pedro, 12/09/2026: "we should be able to configure the front end
using yaml"): one entry per subscription the operator wants on the board —
id, human label, emoji, family, plan, and which live source feeds its meters
(the lane-report row `lane:codex:c` or a proxy bearer `bearer:666a53af`).
Rows merge with live observations at render time; a configured row whose
source is missing renders honestly as "no reading" — a configured row never
disappears because a probe blinked.

Resolution order per render: the YAML is re-read only when its mtime moved
(the dashboard renders per request; a stat per render is cheap), and any
parse/validation failure falls back to the LAST GOOD config plus a
`config_error` marker — a bad edit must degrade the table, not blank it.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path.home() / ".local/state/anthropic-throttle-proxy/fleet-ui.yaml"
PATH_ENV = "FLEET_UI_CONFIG"

_DEFAULT_EMOJI = {
    "anthropic": "✳️",
    "openai": "🅒",
    "chinese-frontier": "⚡",
    "github": "🐙",
}

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}  # path -> (mtime, parsed)


def config_path() -> Path:
    return Path(os.environ.get(PATH_ENV) or DEFAULT_PATH)


def _validate(raw: Any) -> dict[str, Any]:
    """Fail-closed shape check. Raises ValueError with the offending key."""
    if not isinstance(raw, dict):
        raise ValueError("fleet-ui config must be a mapping")
    subs = raw.get("subscriptions")
    if not isinstance(subs, list):
        raise ValueError("fleet-ui config: 'subscriptions' must be a list")
    for i, s in enumerate(subs):
        if not isinstance(s, dict):
            raise ValueError(f"subscriptions[{i}] must be a mapping")
        for key in ("id", "label", "family"):
            if not isinstance(s.get(key), str) or not s[key]:
                raise ValueError(f"subscriptions[{i}].{key} must be a non-empty string")
    return raw


def load(path: Path | None = None) -> dict[str, Any]:
    """Load the YAML config (mtime-cached). Returns
    {"subscriptions": [...], "defaults": {...}, "config_error": str | None,
     "config_path": str}."""
    global _cache
    path = path or config_path()
    key = str(path)
    with _lock:
        cached = _cache.get(key)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            parsed = cached[1] if cached else {"subscriptions": [], "defaults": {}}
            return {
                **parsed,
                "config_path": key,
                "config_error": None if cached else f"config missing at {path}",
            }
        if cached is not None and cached[0] == mtime:
            return {**cached[1], "config_path": key}
        try:
            raw = _validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            parsed = {
                "subscriptions": raw.get("subscriptions") or [],
                "defaults": {
                    "emoji_by_family": {
                        **_DEFAULT_EMOJI,
                        **(raw.get("defaults", {}) or {}).get("emoji_by_family", {}),
                    }
                },
            }
            error = None
        except Exception as e:  # noqa: BLE001 — a bad edit must degrade, not blank
            parsed = cached[1] if cached else {"subscriptions": [], "defaults": {}}
            error = f"{type(e).__name__}: {e}"[:200]
        _cache[key] = (mtime, parsed)
        return {**parsed, "config_path": key, "config_error": error}


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def decorate(rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """Merge the declarative config onto the live rows.

    A configured subscription finds its live row (by id, or lane id) and
    receives the config's label/emoji/family/plan — live data (meters, status,
    pace) still feeds the row. Configured rows with no live match append with
    an honest "no reading" state; live rows absent from the config keep their
    default emoji and sort after the configured ones. Config order is render
    order.
    """
    subs = config.get("subscriptions") or []
    emoji_by_family = (config.get("defaults") or {}).get("emoji_by_family", {})
    by_key: dict[str, dict[str, Any]] = {}
    for r in rows:
        rid = str(r.get("id") or "")
        by_key[rid] = r
        lane_id = str(r.get("lane_id") or "")
        if lane_id:
            by_key.setdefault(f"lane:{lane_id}", r)
        bid = str(r.get("bearer_id") or "")
        if bid:
            by_key.setdefault(f"bearer:{bid}", r)
        email = str(r.get("identity") or "")
        if email:
            by_key.setdefault(f"identity:{email}", r)

    decorated: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for entry in subs:
        keys = [
            entry.get("id"),
            entry.get("lane"),
            f"bearer:{entry.get('bearer')}" if entry.get("bearer") else None,
            f"identity:{entry.get('identity')}" if entry.get("identity") else None,
        ]
        match = next((by_key[k] for k in keys if k and k in by_key), None)
        if match is None:
            decorated.append(
                {
                    "id": entry["id"],
                    "identity": entry.get("identity") or entry["id"],
                    "provider": entry.get("provider") or entry["id"],
                    "icon": entry.get("emoji")
                    or emoji_by_family.get(entry.get("family") or "", "🤖"),
                    "sub": entry.get("identity") or "",
                    "family": entry.get("family") or "",
                    "plan": entry.get("plan") or "",
                    "meters": [
                        {"label": "status", "pct": None, "reset_in": "", "note": "no reading"}
                    ],
                    "pace": None,
                    "pace_warn": False,
                    "eta": "",
                    "status": "unknown",
                    "status_icon": "🤖",
                    "detail": "configured; no live reading",
                    "billing": None,
                    "configured": True,
                }
            )
            continue
        consumed.add(id(match))
        # The live id (lane id / bearer id) is identity used by the router and
        # the tests — the config decorates presentation only and must never
        # rewrite it. The config's own id is just the lookup key.
        match["label"] = entry.get("label") or match.get("label") or entry["id"]
        if entry.get("emoji"):
            match["icon"] = entry["emoji"]
        if entry.get("family"):
            match["family"] = entry["family"]
        if entry.get("plan"):
            match["plan"] = entry["plan"]
        if entry.get("identity"):
            match["identity"] = entry["identity"]
        match["configured"] = True
        decorated.append(match)

    # Unconfigured rows keep their own icon; the family default only fills a
    # genuinely empty one (a live row that already carries an emoji — e.g. the
    # lane report's — must not be overwritten by the YAML defaults).
    for r in rows:
        if id(r) not in consumed:
            if not r.get("icon"):
                r["icon"] = emoji_by_family.get(r.get("family") or "", "🤖")
            decorated.append(r)
    return {"rows": decorated, "config_error": config.get("config_error")}
