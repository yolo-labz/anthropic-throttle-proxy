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
    "openai": "🧠",
    "chinese-frontier": "⚡",
    "github": "🐙",
}

_lock = threading.Lock()
_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}  # path -> (stat, result)


def config_path() -> Path:
    return Path(os.environ.get(PATH_ENV) or DEFAULT_PATH)


def _validate_defaults(defaults: Any) -> None:
    """Fail-closed check of the optional ``defaults`` block."""
    if not isinstance(defaults, dict):
        raise ValueError("defaults must be a mapping")
    emojis = defaults.get("emoji_by_family", {})
    if not isinstance(emojis, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in emojis.items()
    ):
        raise ValueError("defaults.emoji_by_family must map strings to strings")
    # OPTIONAL presentation knobs (consumed by the dashboard's display layer:
    # families hidden from the active board, whether the primary row shows).
    # Validated only when present: a config written before these keys existed
    # must keep loading unchanged (backwards compatible). No default hide —
    # absent keys hide nothing.
    if "hidden_families" in defaults:
        hidden = defaults["hidden_families"]
        if not isinstance(hidden, list) or any(not isinstance(f, str) for f in hidden):
            raise ValueError("defaults.hidden_families must be a list of strings")
    if "show_primary" in defaults and not isinstance(defaults["show_primary"], bool):
        raise ValueError("defaults.show_primary must be a boolean")


def _validate_subscription(entry: Any, index: int, ids: set[str]) -> None:
    """Fail-closed check of one subscription entry; ``ids`` accumulates for uniqueness."""
    if not isinstance(entry, dict):
        raise ValueError(f"subscriptions[{index}] must be a mapping")
    for key in ("id", "label", "family"):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise ValueError(f"subscriptions[{index}].{key} must be a non-empty string")
    for key in ("emoji", "plan", "provider", "lane", "bearer", "identity"):
        if key in entry and not isinstance(entry[key], str):
            raise ValueError(f"subscriptions[{index}].{key} must be a string")
    if entry["id"] in ids:
        raise ValueError(f"subscriptions[{index}].id must be unique")
    ids.add(entry["id"])


def _validate(raw: Any) -> dict[str, Any]:
    """Fail-closed shape check. Raises ValueError with the offending key."""
    if not isinstance(raw, dict):
        raise ValueError("fleet-ui config must be a mapping")
    subs = raw.get("subscriptions")
    if not isinstance(subs, list):
        raise ValueError("fleet-ui config: 'subscriptions' must be a list")
    _validate_defaults(raw.get("defaults", {}))
    ids: set[str] = set()
    for i, s in enumerate(subs):
        _validate_subscription(s, i, ids)
    return raw


def _stat_stamp(path: Path) -> tuple[int, int] | None:
    """The mtime/ctime identity of the config file, or None when unreadable."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_ctime_ns)


def _unreadable(
    path: Path, key: str, cached: tuple[tuple[int, int], dict[str, Any]] | None, explicit: bool
) -> dict[str, Any]:
    """The last good parse (or an empty board) plus the unreadable marker."""
    parsed = cached[1] if cached else {"subscriptions": [], "defaults": {}}
    return {
        **parsed,
        "config_path": key,
        "config_error": f"config missing or unreadable at {path}" if cached or explicit else None,
    }


def _parse(path: Path) -> dict[str, Any]:
    """Read, bound, validate and shape one config file (raises on any of those)."""
    # ponytail: a small operator file, not a corpus; bound parsing work
    # on the UI event loop. Larger inventories need off-thread loading.
    with path.open(encoding="utf-8") as source:
        text = source.read(32_769)
    if len(text) > 32_768:
        raise ValueError("fleet-ui config exceeds 32768 characters")
    raw = _validate(yaml.safe_load(text))
    raw_defaults = raw.get("defaults", {}) or {}
    return {
        "subscriptions": raw.get("subscriptions") or [],
        "defaults": {
            "emoji_by_family": {
                **_DEFAULT_EMOJI,
                **(raw_defaults.get("emoji_by_family") or {}),
            },
            # Review B1: _validate accepts these keys, so load() must
            # preserve them. Dropping them here silently disabled
            # accounts_hidden()/show_primary for every loaded config
            # while the load test only looked at subscriptions+emoji.
            "hidden_families": list(raw_defaults.get("hidden_families") or []),
            "show_primary": raw_defaults.get("show_primary", True),
        },
    }


def _error_text(exc: Exception) -> str:
    """Operator-safe failure text: a YAML parser error can echo file contents."""
    if type(exc) is ValueError:
        return str(exc)[:200]
    return f"{type(exc).__name__}: cannot load config"


def load(path: Path | None = None) -> dict[str, Any]:
    """Load the YAML config (mtime-cached). Returns
    {"subscriptions": [...], "defaults": {...}, "config_error": str | None,
     "config_path": str}."""
    explicit = path is not None or bool(os.environ.get(PATH_ENV))
    path = path or config_path()
    key = str(path)
    with _lock:
        cached = _cache.get(key)
        stamp = _stat_stamp(path)
        if stamp is None:
            return _unreadable(path, key, cached, explicit)
        if cached is not None and cached[0] == stamp:
            return {**cached[1], "config_path": key}
        try:
            parsed = _parse(path)
            error = None
        except (OSError, ValueError, yaml.YAMLError, RecursionError) as e:
            parsed = cached[1] if cached else {"subscriptions": [], "defaults": {}}
            error = _error_text(e)
        result = {**parsed, "config_path": key, "config_error": error}
        _cache[key] = (stamp, result)
        return result


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def _row_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every key a config entry may use to find a live row."""
    by_key: dict[str, dict[str, Any]] = {}
    for r in rows:
        rid = str(r.get("id") or "")
        by_key[rid] = r
        by_key.setdefault(f"lane:{rid}", r)
        lane_id = str(r.get("lane_id") or "")
        if lane_id:
            by_key.setdefault(f"lane:{lane_id}", r)
        bid = str(r.get("bearer_id") or "")
        if bid:
            by_key.setdefault(f"bearer:{bid}", r)
        email = str(r.get("identity") or "")
        if email:
            by_key.setdefault(f"identity:{email}", r)
    return by_key


def _entry_match(entry: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> dict | None:
    """The live row a config entry names, by id, lane id, bearer or identity."""
    keys = [
        entry.get("id"),
        entry.get("lane"),
        f"bearer:{entry.get('bearer')}" if entry.get("bearer") else None,
        f"identity:{entry.get('identity')}" if entry.get("identity") else None,
    ]
    return next((by_key[k] for k in keys if k and k in by_key), None)


def _placeholder_row(entry: dict[str, Any], emoji_by_family: dict[str, str]) -> dict[str, Any]:
    """A configured row with no live match: honest "no reading" state."""
    return {
        "id": entry["id"],
        "label": entry["label"],
        "identity": entry.get("identity") or entry["id"],
        "provider": entry.get("provider") or entry["id"],
        "icon": entry.get("emoji") or emoji_by_family.get(entry.get("family") or "", "🤖"),
        "sub": entry.get("identity") or "",
        "family": entry.get("family") or "",
        # Nothing observed: no plan, the configured string is only
        # a caption. plan_conflict stays False — there is no
        # observed plan to disagree with.
        "plan": "",
        "plan_caption": entry.get("plan") or "",
        "plan_conflict": False,
        "meters": [{"label": "status", "pct": None, "reset_in": "", "note": "no reading"}],
        "pace": None,
        "pace_warn": False,
        "eta": "",
        "status": "unknown",
        "status_icon": "❔",
        "detail": "configured; no live reading",
        "billing": None,
        "configured": True,
    }


def _merge_entry(match: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Apply the config's label/emoji/family to a copy, and caption its plan."""
    match = dict(match)
    match["label"] = entry["label"]
    if entry.get("emoji"):
        match["icon"] = entry["emoji"]
    if entry.get("family"):
        match["family"] = entry["family"]
    # The observed plan is the measurement and stays in ``plan``; the
    # configured plan is an annotation beside it. Overwriting the reading
    # with the YAML once rendered a probe-reported plan the row never
    # actually had — the one thing a dashboard must not do.
    configured_plan = entry.get("plan") or ""
    observed_plan = str(match.get("plan") or "")
    match["plan_caption"] = configured_plan
    match["plan_conflict"] = bool(
        configured_plan and observed_plan and configured_plan != observed_plan
    )
    match["configured"] = True
    return match


def _unconfigured_rows(
    rows: list[dict[str, Any]], consumed: set[int], emoji_by_family: dict[str, str]
) -> list[dict[str, Any]]:
    """Live rows the config never named.

    They keep their own icon; the family default only fills a genuinely empty
    one (a live row that already carries an emoji — e.g. the lane report's —
    must not be overwritten by the YAML defaults).
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        if id(r) not in consumed:
            r = dict(r)
            if not r.get("icon"):
                r["icon"] = emoji_by_family.get(r.get("family") or "", "🤖")
            out.append(r)
    return out


def decorate(rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """Merge the declarative config onto the live rows.

    A configured subscription finds its live row (by id, or lane id) and
    receives the config's label/emoji/family — live data (meters, status,
    pace) still feeds the row. The config's plan NEVER overwrites an observed
    one: the meter reading is the measurement, the YAML is at best an
    annotation, so the configured plan lands in ``plan_caption`` and
    ``plan_conflict`` flags a configured plan that disagrees with the
    observed one. Configured rows with no live match append with an honest
    "no reading" state and an EMPTY ``plan`` (nothing was observed — the
    YAML string is the caption, not a reading); live rows absent from the
    config keep their default emoji and sort after the configured ones.
    Config order is render order. Source rows are copied, never mutated.

    Matching is ``_entry_match``, the no-reading row ``_placeholder_row``,
    the merge ``_merge_entry``, the leftovers ``_unconfigured_rows``.
    """
    subs = config.get("subscriptions") or []
    emoji_by_family = (config.get("defaults") or {}).get("emoji_by_family", {})
    by_key = _row_index(rows)
    decorated: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for entry in subs:
        match = _entry_match(entry, by_key)
        if match is None:
            decorated.append(_placeholder_row(entry, emoji_by_family))
            continue
        if id(match) in consumed:
            continue
        consumed.add(id(match))
        decorated.append(_merge_entry(match, entry))
    decorated.extend(_unconfigured_rows(rows, consumed, emoji_by_family))
    return {"rows": decorated, "config_error": config.get("config_error")}
