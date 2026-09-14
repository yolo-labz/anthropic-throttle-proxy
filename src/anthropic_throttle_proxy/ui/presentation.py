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


def apply_display(view: dict, config: dict) -> dict:
    """Hide configured display rows and decorate copied meters, leaving source evidence intact."""
    result = deepcopy(view)
    defaults = config.get("defaults") or {}
    hidden = {_family(name) for name in defaults.get("hidden_families", [])}
    result["subscriptions"] = [
        row
        for row in result.get("subscriptions", [])
        if _family(row.get("family") or "") not in hidden
    ]
    lanes = result.get("lanes") or {}
    lanes["registry"] = [
        row
        for row in lanes.get("registry", [])
        if not any(_family(row.get(key) or "") in hidden for key in ("family", "provider", "id"))
    ]
    result["lanes"] = lanes
    result["show_local"] = defaults.get("show_primary", True)
    if not result["show_local"]:
        result["providers"] = [p for p in result.get("providers", []) if p.get("kind") == "sibling"]
        result["bearers"], result["signals"] = [], []
        result["identity"], result["last_advisor"] = {}, None
        result["status"] = {
            "level": "idle",
            "verdict": "SUBSCRIPTIONS",
            "binding": None,
            "since": "",
            "detail": "Read-only subscription meters; model eligibility is not verified",
        }
    for row in result["subscriptions"]:
        sid = row.get("id", "").lower()
        if sid in {"codex:a", "codex:b", "codex:c"}:
            row["account_badge"] = sid[-1].upper()
            if row.get("icon") in {"🅐", "🅑", "🅒", "🅰️", "🅱️"}:
                row["icon"] = "🌀"
        meters = row.get("meters") or []
        for meter in meters:
            label, duration = meter.get("label"), meter.get("window_mins")
            if duration == 300 or label == "5h":
                meter["icon"] = "⏱️"
            elif duration == 10080 or label == "7d":
                meter["icon"] = "📅"
            elif meter.get("unlimited"):
                meter["icon"] = "♾️"
            else:
                meter.setdefault("icon", "📊")
            meter["reset_at"] = _reset_at(meter.get("resets_at"))
        pools = {m.get("label") for m in meters} - {None, "", "5h", "7d"}
        if sid.startswith("codex:") and len(pools) > 1:
            detail = "Multiple reported pools; model-to-pool applicability unknown"
            row["detail"] = " · ".join(filter(None, [row.get("detail"), detail]))
            readings = [m["pct"] for m in meters if isinstance(m.get("pct"), int | float)]
            if row.get("status") == "exhausted" and readings and min(readings) < 100:
                row["status"], row["status_icon"] = "pool limited", "⚠️"
                row["detail"] += "; a spent pool is not proof that every model is unavailable"
    return result
