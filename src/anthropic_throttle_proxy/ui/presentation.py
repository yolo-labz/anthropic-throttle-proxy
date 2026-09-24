"""Display-only projection; never an admission or credential policy."""

from copy import deepcopy
from datetime import UTC, datetime
from math import isfinite


def _family(value: str) -> str:
    name = value.strip().lower()
    return "anthropic" if name == "claude" else name


def _reset_at(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        return ""
    try:
        return datetime.fromtimestamp(value, UTC).strftime("%d/%m/%Y %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return ""


def accounts_hidden(config: dict) -> bool:
    return "anthropic" in {
        _family(name) for name in (config.get("defaults") or {}).get("hidden_families", [])
    }


def _hidden_families(defaults: dict) -> set[str]:
    return {_family(name) for name in defaults.get("hidden_families", [])}


def _visible_subscriptions(rows: list[dict], hidden: set[str]) -> list[dict]:
    return [row for row in rows if _family(row.get("family") or "") not in hidden]


def _visible_lane_registry(lanes: dict, hidden: set[str]) -> list[dict]:
    return [
        row
        for row in lanes.get("registry", [])
        if not any(_family(row.get(key) or "") in hidden for key in ("family", "provider", "id"))
    ]


def _subscriptions_only_status() -> dict:
    """Placeholder verdict for the ``show_primary: false`` board."""
    return {
        "level": "idle",
        "verdict": "SUBSCRIPTIONS",
        "binding": None,
        "since": "",
        "detail": "Read-only subscription meters; model eligibility is not verified",
    }


def _hide_primary(result: dict) -> None:
    """Drop the primary lane's evidence when the operator hides the local board."""
    result["providers"] = [p for p in result.get("providers", []) if p.get("kind") == "sibling"]
    result["bearers"], result["signals"] = [], []
    result["identity"], result["last_advisor"] = {}, None
    result["status"] = _subscriptions_only_status()


def _meter_icon(meter: dict) -> str:
    """Timer, calendar, infinity, or the meter's own icon — by window length."""
    label, duration = meter.get("label"), meter.get("window_mins")
    if duration == 300 or label == "5h":
        return "⏱️"
    if duration == 10080 or label == "7d":
        return "📅"
    if meter.get("unlimited"):
        return "♾️"
    return meter["icon"] if "icon" in meter else "📊"


def _codex_badge(row: dict, sid: str) -> None:
    """Restore the account letter badge and its canonical icon."""
    if sid not in {"codex:a", "codex:b", "codex:c"}:
        return
    row["account_badge"] = sid[-1].upper()
    if row.get("icon") in {"🅐", "🅑", "🅒", "🅰️", "🅱️"}:
        row["icon"] = "🌀"


def _codex_pool_note(row: dict, sid: str, meters: list[dict]) -> None:
    """A codex row with several pools: say applicability is unknown, and never
    let one spent pool read as "every model unavailable"."""
    pools = {m.get("label") for m in meters} - {None, "", "5h", "7d"}
    if not (sid.startswith("codex:") and len(pools) > 1):
        return
    detail = "Multiple reported pools; model-to-pool applicability unknown"
    row["detail"] = " · ".join(filter(None, [row.get("detail"), detail]))
    readings = [m["pct"] for m in meters if isinstance(m.get("pct"), int | float)]
    if row.get("status") == "exhausted" and readings and min(readings) < 100:
        row["status"], row["status_icon"] = "pool limited", "⚠️"
        row["detail"] += "; a spent pool is not proof that every model is unavailable"


def _decorate_row(row: dict) -> None:
    """Account badge, per-meter icon and reset stamp, then the pool note."""
    sid = row.get("id", "").lower()
    _codex_badge(row, sid)
    meters = row.get("meters") or []
    for meter in meters:
        meter["icon"] = _meter_icon(meter)
        meter["reset_at"] = _reset_at(meter.get("resets_at"))
    _codex_pool_note(row, sid, meters)


def apply_display(view: dict, config: dict) -> dict:
    """Hide configured display rows and decorate copied meters, leaving source evidence intact.

    Row hiding is ``_visible_subscriptions`` / ``_visible_lane_registry``; the
    primary-off board is ``_hide_primary``; per-row decoration is
    ``_decorate_row``.
    """
    result = deepcopy(view)
    defaults = config.get("defaults") or {}
    hidden = _hidden_families(defaults)
    result["subscriptions"] = _visible_subscriptions(result.get("subscriptions", []), hidden)
    lanes = result.get("lanes") or {}
    lanes["registry"] = _visible_lane_registry(lanes, hidden)
    result["lanes"] = lanes
    result["show_local"] = defaults.get("show_primary", True)
    if not result["show_local"]:
        _hide_primary(result)
    for row in result["subscriptions"]:
        _decorate_row(row)
    return result
