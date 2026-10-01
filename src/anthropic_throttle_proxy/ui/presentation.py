"""Display-only projection; never an admission or credential policy."""

import re
from copy import deepcopy
from datetime import UTC, datetime
from math import isfinite

from ..lanes import _pct


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
    result["identity"] = {}
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


# ── capacity truth (spec 285) ───────────────────────────────────────────────
# Quota evidence is neither transport health nor permission to route a model.
# Independent seats may disagree: one exhausted allowance does not erase a
# usable sibling, while stale/unknown readings never imply usable capacity.
_CAPACITY_CLASSES = ("exhausted", "stale", "unknown", "limited", "usable")

# Statuses where the row itself says the seat is refusing RIGHT NOW, with the
# evidence behind the verdict. `locked` is a usage cooldown and `token expired`
# an auth state: both are measured refusals, not quota exhaustion — but the
# summary bucket is "cannot serve now, measured", and the row keeps its own
# precise status one line below.
_REFUSING_STATUSES = frozenset({"exhausted", "rejected", "refused", "locked", "token expired"})

_CAPACITY_LABELS = {
    "usable": "capacity usable",
    "limited": "capacity limited",
    "exhausted": "capacity exhausted",
    "stale": "capacity stale",
    "unknown": "capacity unknown",
    "unmeasured": "capacity unmeasured",
}

_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _positive(value: object) -> bool:
    """True only for a genuinely numeric value above zero (bools excluded)."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return False
    try:
        return isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _meter_evidence(meters: list) -> str:
    """``usable`` / ``exhausted`` / ``unknown`` from MEASURED meter fields only.

    Usable needs positive evidence: a reading below 100 %, an unlimited
    product (Copilot chat/completions keep serving when premium is spent), or
    a positive non-percentage balance/remaining (money and credits have no
    ceiling to be a percentage of). Full readings with nothing live beside
    them prove exhaustion; anything unmeasured stays unknown — never usable.
    """
    readings: list[float] = []
    live = False
    for meter in meters:
        if not isinstance(meter, dict):
            continue
        pct = _pct(meter.get("pct"))
        if meter.get("pct") is not None and (pct is None or pct < 0):
            return "unknown"
        if meter.get("rejected") or (
            pct is not None and pct >= 100 and not meter.get("exhausted_ok")
        ):
            return "exhausted"
        if pct is not None and not meter.get("exhausted_ok"):
            readings.append(pct)
        if meter.get("unlimited") is True:
            live = True
        if _positive(meter.get("balance_total")) or _positive(meter.get("remaining")):
            live = True
    if live or any(r < 100.0 for r in readings):
        return "usable"
    return "unknown"


def row_capacity_class(row: dict) -> str:
    """Capacity class for one seat row: usable / limited / exhausted / stale / unknown.

    The row's own verdict leads (lanes normalisation already folded meter
    truth into it); meters only VERIFY a usable claim. A configured row with
    no reading — an unassigned seat — lands in unknown and is never counted
    usable, and stale evidence outranks every other reading.
    """
    status = str(row.get("status") or "unknown").strip().lower()
    if status == "stale":
        return "stale"
    if status in _REFUSING_STATUSES:
        return "exhausted"
    if status == "pool limited":
        return "limited"
    if status == "ok":
        return _meter_evidence(row.get("meters") or [])
    return "unknown"


def capacity_summary(view: dict) -> dict:
    """At-a-glance board truth (FR-1/FR-2/FR-4), from the projected rows.

    Counts are SEATS, never a summed budget: unlike percentages, units and
    windows are listed per binding window and never added into a fictitious
    total. Binding windows are named only where the evidence is current — a
    stale 100 % is untrusted, not proven binding. The live line and the
    throughput belong to this local proxy, so they ride the ``show_local``
    scope; an unmeasured throughput is absent, not zero.
    """
    rows = [r for r in (view.get("subscriptions") or []) if isinstance(r, dict)]
    counts = dict.fromkeys(_CAPACITY_CLASSES, 0)
    windows: list[dict] = []
    for row in rows:
        cls = row_capacity_class(row)
        counts[cls] = counts.get(cls, 0) + 1
        if cls in {"stale", "unknown"}:
            continue
        for meter in row.get("meters") or []:
            if not isinstance(meter, dict):
                continue
            pct = _pct(meter.get("pct"))
            rejected = bool(meter.get("rejected"))
            full = pct is not None and pct >= 100.0 and not meter.get("exhausted_ok")
            if not (rejected or full):
                continue
            windows.append(
                {
                    "row": row.get("id") or "",
                    "label": row.get("label") or row.get("identity") or row.get("id") or "?",
                    "window": meter.get("label") or "?",
                    "pct": pct,
                    "rejected": rejected,
                    "reset_in": meter.get("reset_in") or "",
                    "reset_at": meter.get("reset_at") or "",
                }
            )
    summary: dict = {
        "counts": counts,
        "total": len(rows),
        "windows": windows,
        "live": None,
        # Stable shape: None when unmeasured — the template must never read a
        # key the summary may forget to carry.
        "throughput": None,
    }
    if not view.get("show_local", True):
        return summary
    summary["live"] = {
        "inflight": int(view.get("inflight") or 0),
        "queued": int(view.get("queued") or 0),
        "capacity": int(view.get("max_concurrent") or 0),
    }
    tps = view.get("tps")
    if tps is not None:
        seen = tps.get("seen") if isinstance(tps, dict) else getattr(tps, "seen", False)
        value = _pct(tps.get("value") if isinstance(tps, dict) else getattr(tps, "value", None))
        if seen and value is not None and value >= 0:
            summary["throughput"] = {"value": round(value, 1), "unit": "tokens/s"}
    return summary


def _norm_token(value: object) -> str:
    return _TOKEN_RE.sub("", str(value or "").lower())


def _row_tokens(row: dict) -> set[str]:
    """Identifiers a seat row answers to: its lane-id prefix and its provider.

    Family is deliberately NOT a key: ``openai`` the upstream and ``codex:*``
    the subscriptions share a family and nothing else, and a family join would
    paint ChatGPT exhaustion onto an unrelated routing row.
    """
    rid = str(row.get("id") or "")
    head = rid.split(":", 1)[0] if ":" in rid else rid
    return {t for t in {_norm_token(head), _norm_token(row.get("provider"))} if len(t) >= 2}


def attach_provider_capacity(providers: list, rows: list) -> None:
    """Join seat capacity onto routing rows (FR-5/FR-6), in place, display-only.

    A mixed set with a usable seat is limited, not wholly exhausted. Otherwise
    disagreement is unknown; only unanimous evidence grants a uniform verdict.
    ``mimo`` joins both individual and Team rows without collapsing them. A
    join that finds no seat is unmeasured; connectivity never implies quota.
    """
    seats = [r for r in (rows or []) if isinstance(r, dict)]
    for provider in providers or []:
        if not isinstance(provider, dict):
            continue
        tokens = {_norm_token(provider.get("name"))}
        tokens = {t for t in tokens if len(t) >= 2}
        matched = [r for r in seats if tokens & _row_tokens(r)] if tokens else []
        classes = [row_capacity_class(r) for r in matched]
        state = "unmeasured"
        if classes:
            state = classes[0] if len(set(classes)) == 1 else "unknown"
            if len(set(classes)) > 1 and any(c in {"usable", "limited"} for c in classes):
                state = "limited"
        provider["capacity"] = {
            "state": state,
            "label": _CAPACITY_LABELS[state],
            "matched": [str(r.get("id") or "") for r in matched],
            "detail": " · ".join(
                f"{r.get('label') or r.get('id') or '?'} {cls}"
                for r, cls in zip(matched, classes, strict=True)
            )
            or "no seat row joined to this routing destination",
        }
