"""HTMX dashboard endpoints.

Routes:
    GET  /ui              — full page (Catppuccin Mocha, one HTMX script).
    GET  /ui/stats        — `<table>` partial; hx-trigger fires every 2 s.
    GET  /ui/config       — config-editor form partial (one row per editable knob).
    POST /ui/config       — set one knob's runtime override (validates + persists).
    POST /ui/config/reset — drop one knob's runtime override, restore env default.
    GET  /ui/static/...   — CSS + favicon.
    POST /ui/advisor      — optional GROQ call (gated by ADVISOR_ENABLED).

The hot path proxy is NOT routed through this module. Failure to render the
UI must not break /v1/messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import aiohttp_jinja2
import jinja2
from aiohttp import web

from .. import accounts as _accounts
from .. import config as _config
from .. import copilot as _copilot
from .. import fleet as _fleet
from .. import history as _history
from .. import lanes as _lanes
from .. import metrics as _metrics

# Lazy import: keep the proxy hot path free of UI deps.
from .. import proxy as _proxy
from . import signals as _signals

_HERE = Path(__file__).resolve().parent
_TEMPLATES = _HERE / "templates"
_STATIC = _HERE / "static"


def _asset_version(static: Path = _STATIC) -> str:
    """Content hash of the static bundle, used as a cache-busting URL suffix.

    Nix normalises every store file's mtime to epoch 1, so aiohttp serves the
    stylesheet with ``Last-Modified: 1970``. A browser with no explicit
    ``Cache-Control`` then applies heuristic freshness (10% of the resource's
    apparent age = decades) and never revalidates: after a rebuild Firefox
    rendered the NEW markup against the OLD CSS, which reads as a totally
    unstyled dashboard (03/08/2026). Hashing content into the URL gives each
    build its own cache entry.
    """
    h = hashlib.sha256()
    with contextlib.suppress(OSError):
        for path in sorted(p for p in static.iterdir() if p.is_file()):
            h.update(path.read_bytes())
    return h.hexdigest()[:12]


_ASSET_V = _asset_version()

# Utilization at/above this fraction of a unified window counts as "pacing"
# even before Anthropic flips the window to ``rejected``.
_PACING_UTIL = 0.80

# Partial-template paths, named once so SonarQube python:S1192 (duplicated
# literal) stays clean. f-strings keep the full literal out of the source.
_PARTIALS = "partials"
_TPL_ADVISOR = f"{_PARTIALS}/advisor.html"
_TPL_CONFIG = f"{_PARTIALS}/config.html"


def _bearer_pacing_state(b: dict) -> tuple[str | None, float | None, object]:
    """Classify one bearer for the status strip.

    Returns ``(state, util_5h, retry_after)`` where state is ``"throttled"`` /
    ``"pacing"`` / ``None`` (clear). Worst-wins aggregation is the caller's job.
    """
    unified = b.get("unified") or {}
    util_5h = unified.get("util_5h")
    util_7d = unified.get("util_7d")
    status_5h = unified.get("status") or unified.get("status_5h")
    # The 7d window was not read here at all, so a bearer whose WEEKLY budget is
    # spent — the state two of three fleet accounts sit in mid-week — reported
    # "clear" while the binding line right beside it said 100% (05/08/2026). A
    # rejected window is a rejected window whichever cycle it belongs to.
    status_7d = unified.get("status_7d")
    retry_after = (b.get("last_ratelimit") or {}).get("retry-after")
    lim = b.get("limiter") or {}
    live, hard = lim.get("max_concurrent"), lim.get("hard_max")
    shrunk = live is not None and hard is not None and live < hard
    over_pacing = any(u is not None and u >= _PACING_UTIL for u in (util_5h, util_7d))
    if status_5h == "rejected" or status_7d == "rejected" or retry_after:
        return "throttled", util_5h, retry_after
    if shrunk or over_pacing or b.get("queued", 0) > 0:
        return "pacing", util_5h, retry_after
    return None, util_5h, retry_after


def _window_stale(unified: dict | None, rkey: str, now: float) -> bool:
    """True when a unified window's last reading predates its own reset epoch.

    A stale reading is effectively 0% — the window rolled over since the header
    was captured, so surfacing the frozen utilisation would misreport a cleared
    window as still-loaded (the account panel already treats this as "0% · reset").
    """
    reset = (unified or {}).get(rkey)
    return reset is not None and now >= reset


# (util, reset) key pairs per unified window, for the stale-strip pass.
_UNIFIED_WINDOWS = (("util_5h", "reset_5h"), ("util_7d", "reset_7d"))


def _live_unified(unified: dict | None, now: float) -> dict:
    """Copy of ``unified`` with any stale window's utilisation dropped.

    Feeds the canonical ``_binding_window`` / ``_binding_utilization`` selectors so
    the status strip names the window actually holding the fleet back — a window
    whose reset already elapsed carries a frozen reading and must not win binding
    (it drops to the other live window, or None), instead of the old hardcoded
    "5h" label reporting a reset window's stale value.
    """
    if not unified:
        return {}
    live = dict(unified)
    for ukey, rkey in _UNIFIED_WINDOWS:
        if _window_stale(unified, rkey, now):
            live.pop(ukey, None)
    return live


def _fleet_verdict(n: int, throttled: int, pacing: int) -> tuple[str, str, str]:
    """Worst-wins ``(level, verdict, detail-prefix)`` for ``n`` bearers."""
    plural = "s" if n != 1 else ""
    if throttled:
        return "throttled", "THROTTLED", f"{throttled} of {n} bearer{plural} throttled"
    if pacing:
        return "pacing", "PACING", f"{pacing} of {n} bearer{plural} pacing"
    return "healthy", "HEALTHY", f"all {n} bearer{plural} clear"


def _fmt_since(seconds: float) -> str:
    """Compact duration for the status strip: ``12m``, ``2h 05m``, ``just now``."""
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _compute_status(
    bearers: list[dict], queue_mode: str, now: float | None = None
) -> dict[str, object]:
    """Derive one fleet-wide verdict from the live snapshot (drives the status strip).

    Worst-wins across bearers: ``throttled`` > ``pacing`` > ``healthy``. The
    ``binding`` line names the most-constrained bearer so the operator sees the
    single thing holding the fleet back without scanning the table. ``now`` (unix
    seconds) gates stale-window dropping; defaults to the current time.
    """
    if now is None:
        now = time.time()
    if not bearers:
        return {
            "level": "idle",
            "verdict": "IDLE",
            "detail": "no bearers yet — point a client at this proxy to start.",
            "since": _fmt_since(_history.level_since("idle", now)),
        }

    throttled: list[str] = []
    pacing: list[str] = []
    binding: tuple[float, str, str, object] | None = None  # (util, label, bearer_id, retry_after)
    for b in bearers:
        state, _util_5h, retry_after = _bearer_pacing_state(b)
        if state == "throttled":
            throttled.append(b["bearer_id"])
        elif state == "pacing":
            pacing.append(b["bearer_id"])
        live = _live_unified(b.get("unified"), now)
        util = _proxy._binding_utilization(live)
        label = _proxy._binding_window(live)
        if util is not None and label is not None and (binding is None or util > binding[0]):
            binding = (util, label, b["bearer_id"], retry_after)

    level, verdict, detail = _fleet_verdict(len(bearers), len(throttled), len(pacing))
    bound: dict[str, object] | None = None
    if binding is not None:
        # The binding block below the strip renders the same fact with a name,
        # a countdown and a way out. Repeating it here as prose gave the
        # operator two renderings of one condition that can drift apart
        # (cross-family review, round 2). The object is the single source.
        # A prose fragment cannot be ranked, linked to its row, or read first,
        # and "which subscription is blocked, until when, and what takes traffic
        # next" is the question this page exists to answer mid-incident.
        bound = {
            "bearer_id": binding[2],
            "window": binding[1],
            "pct": round(binding[0] * 100),
            "retry_after": binding[3],
        }
    if queue_mode == "off":
        detail += " · queue off (passthrough)"
    # A verdict with no duration cannot separate a transient from an outage
    # (`docs/DASHBOARD-DESIGN.md` S4.4). The ring knows when the level last
    # changed; asking it once per render is idempotent for an unchanged level.
    return {
        "level": level,
        "verdict": verdict,
        "detail": detail,
        "binding": bound,
        "since": _fmt_since(_history.level_since(level, now)),
    }


# account label -> last-published scoped model, so a Fable→Sonnet flip drops the
# stale per-model series instead of freezing it forever (Codex MEDIUM). Both this
# and the registry are process-local, so they stay in sync across a restart.
_scoped_model_seen: dict[str, str] = {}


def _publish_lane_gauges(lanes_view: dict[str, Any]) -> None:
    """Mirror the lane report into /metrics — including lanes this proxy never routes.

    A missing reading stays UNPUBLISHED rather than set to 0: an unprobed
    ChatGPT account reading as 0% is indistinguishable from a genuinely idle
    one, and that ambiguity is exactly what hid account B's spare capacity on
    03/08/2026.
    """
    age = lanes_view.get("age_s")
    if isinstance(age, (int, float)):
        _metrics.M_LANE_REPORT_AGE.set(float(age))
    for lane in lanes_view.get("lanes") or []:
        for meter in lane.get("meters") or []:
            if meter.get("used_pct") is None:
                continue
            _metrics.M_LANE_USED.labels(lane["id"], lane["family"], meter["label"]).set(
                meter["used_pct"]
            )


def _publish_account_gauges(
    endpoint: dict[str, dict[str, object]], identity: dict[str, object]
) -> None:
    """Mirror endpoint truth into /metrics so Grafana sees what /ui sees."""
    for label, path in _accounts.parse_spec(_config.ACCOUNT_CRED_PATHS):
        usage = (endpoint.get(path) or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        for window, ukey, rkey in (("5h", "util_5h", "reset_5h"), ("7d", "util_7d", "reset_7d")):
            util, reset = usage.get(ukey), usage.get(rkey)
            if util is not None:
                _metrics.M_ACCOUNT_USAGE.labels(label, window).set(util)
            if reset is not None:
                _metrics.M_ACCOUNT_RESET.labels(label, window).set(reset)
        # Spec 2: weekly per-model (scoped) meter — labeled by the model it
        # currently tracks so a Fable→Sonnet flip is visible per account.
        scoped = usage.get("scoped")
        if isinstance(scoped, dict) and scoped.get("util") is not None and scoped.get("model"):
            model = str(scoped["model"])
            prev = _scoped_model_seen.get(label)
            if prev is not None and prev != model:
                # Model flipped — drop the stale series (prev was published, so
                # the labelset exists; safe to remove without a guard).
                _metrics.M_ACCOUNT_SCOPED.remove(label, prev)
            _scoped_model_seen[label] = model
            _metrics.M_ACCOUNT_SCOPED.labels(label, model).set(scoped["util"])
    suspected = identity.get("suspected") or {}
    if identity["collapsed"]:
        _metrics.M_ACCOUNTS_DISTINCT.set(0)
    elif suspected:
        # Shared email pending live-token verification — unknown, not "distinct".
        _metrics.M_ACCOUNTS_DISTINCT.set(-1)
    elif int(identity["known"]) >= 2:  # type: ignore[call-overload]
        _metrics.M_ACCOUNTS_DISTINCT.set(1)
    else:
        _metrics.M_ACCOUNTS_DISTINCT.set(-1)
    # FR-005: partial collisions (some-but-not-all stores share an account) that
    # the binary distinct gauge above reads as "distinct". duplicates from the
    # richer identity verdict → count of stores tied to a VERIFIED non-unique
    # account; suspected → stores pending live-probe verification.
    duplicates = identity.get("duplicates") or {}
    _metrics.M_ACCOUNT_COLLISIONS.set(sum(len(labels) for labels in duplicates.values()))
    _metrics.M_ACCOUNT_SUSPECTED.set(sum(len(labels) for labels in suspected.values()))


def _provider_label(upstream: str) -> str:
    """Friendly provider name from an upstream URL — the host's root label.

    ``https://api.anthropic.com`` → ``anthropic``; ``https://api.moonshot.ai`` →
    ``moonshot``. Defensive: an unparseable / hostless URL falls back to the
    raw string so the row still renders (never raises into the render path).
    """
    host = urlparse(upstream).hostname or upstream.strip()
    host = host.removeprefix("www.").removeprefix("api.")
    root = host.split(".", 1)[0] if host else ""
    return root or "upstream"


def _build_providers(
    *,
    upstream: str,
    central_url: str,
    central_status: str,
    level: str,
    inflight: int,
    queued: int,
    served: int,
    max_concurrent: int,
    fleet: list[dict],
) -> list[dict]:
    """Unified provider rows: the primary upstream (always) + fleet siblings.

    "Integrate all providers by default" — the primary lane always renders as a
    provider row, synthesized from live proxy state, so the dashboard shows the
    routing destination with no env gate. Each configured sibling proxy
    (``THROTTLE_FLEET_HEALTH``) appends as another row, so every provider the
    proxy can reach lives in ONE table instead of a separate optional card strip.
    """
    is_central = central_url != "(direct)"
    providers: list[dict] = [
        {
            "name": "central" if is_central else _provider_label(upstream),
            "kind": "primary",
            "upstream": central_url if is_central else upstream,
            # The proxy itself is up (it is rendering this page); egress is only
            # impaired when a configured central tier is reporting down.
            "ok": True,
            "egress_ok": not (is_central and central_status == "down"),
            "inflight": inflight,
            "queued": queued,
            "served": served,
            "max_concurrent": max_concurrent,
            "level": level,
            "err": "",
        }
    ]
    for f in fleet:
        ok = bool(f.get("ok"))
        # A sibling can be reachable, resolve DNS, and still be unable to serve
        # one request because its own key is dead — the Kimi lane rendered
        # "HEALTHY egress ok" for weeks that way (04/08/2026). Auth is the
        # verdict that decides whether traffic can land, so it wins.
        auth_dead = f.get("upstream_auth_ok") is False
        providers.append(
            {
                "name": str(f.get("name") or "?"),
                "kind": "sibling",
                "upstream": str(f.get("upstream") or ""),
                "ok": ok,
                "egress_ok": bool(f.get("upstream_egress_ok")),
                "inflight": int(f.get("inflight") or 0),
                "queued": int(f.get("queued") or 0),
                "served": int(f.get("served") or 0),
                "max_concurrent": int(f.get("max_concurrent") or 0),
                # A sibling probe is BINARY reachability, not the primary's
                # 4-state pacing. Map a failed probe to the neutral "idle"
                # (grey dot) so a dead lane is never pixel-identical to a
                # rate-limited-but-serving primary ("throttled", red dot).
                "level": "crit" if (ok and auth_dead) else ("healthy" if ok else "idle"),
                "auth_dead": auth_dead,
                "err": str(f.get("err") or "")
                or (str(f.get("upstream_auth_error") or "") if auth_dead else ""),
            }
        )
    return providers


def _window_meter(label: str, window: dict | None) -> dict | None:
    """One unified window as a meter row, or None when never observed."""
    if not window:
        return None
    if window.get("stale"):
        # The reading predates its own reset: 0%, not the previous cycle's peak.
        return {"label": label, "pct": 0.0, "reset_in": "", "note": "reset"}
    return {
        "label": label,
        "pct": window.get("pct"),
        "reset_in": window.get("reset_in") or "",
        "rejected": bool(window.get("rejected")),
    }


def _account_status(account: dict) -> tuple[str, str]:
    """Worst-first verdict for an Anthropic account row: (status, detail)."""
    token = account.get("token") or {}
    # Detail accumulates: a rejected window and a usage lock are both true at
    # once, and dropping the lock hid WHEN the account comes back.
    notes = []
    if account.get("locked_in"):
        # Naming what it is NOT matters: a usage lock is a capacity cooldown,
        # not an authentication failure, and the two were confused before.
        notes.append(
            f"usage locked (capacity cooldown, not authentication) · resets {account['locked_in']}"
        )
    if account.get("endpoint_err"):
        notes.append(str(account["endpoint_err"]))
    detail = " · ".join(notes)
    credential = account.get("credential")
    if isinstance(credential, dict) and credential.get("ok") is False:
        reason = str(credential.get("detail") or credential.get("reason") or "refused upstream")
        return "refused", " · ".join([reason, *notes])
    if account.get("error"):
        return "error", " · ".join([str(account["error"]), *notes])
    for window, name in ((account.get("win5"), "5h"), (account.get("win7"), "7d")):
        if window and window.get("rejected"):
            return "rejected", " · ".join([f"{name} window rejected", *notes])
    if account.get("locked_in"):
        return "locked", detail
    if token.get("state") == "expired":
        return "token expired", " · ".join(filter(None, [str(token.get("detail") or ""), detail]))
    if not account.get("seen"):
        return "unseen", " · ".join(
            filter(None, ["no request served with this account's current token yet", detail])
        )
    return "ok", detail


def _lane_pace_eta(lane: dict[str, Any], now: float) -> tuple[float | None, str | None]:
    """Burn pace + projected exhaustion for a report lane's binding meter.

    The Anthropic rows have carried these two columns since the account panel
    existed; the report lanes rendered them as em-dashes, so the table answered
    "how full is it" for a Codex subscription but never "will it last the
    week". `codex-usage` prints the same figure at the shell, so the gap was
    only that nothing carried it into the view: the meters already publish
    utilisation, reset instant, and window length.

    Measured worth: ChatGPT B went 0%→100% in a single day on 07/08 at pace
    1.67×. A blank column cannot say that; `1.67× · in 14h` can.

    Paced on the binding (fullest) meter, matching the status rules, and only
    when that meter carries a real reading — a balance has no window, so a
    pay-go lane keeps its em-dash rather than gaining an invented one.
    """
    for meter in lane.get("meters") or []:
        used_pct, resets_at = meter.get("used_pct"), meter.get("resets_at")
        window_mins = meter.get("window_mins")
        if used_pct is None or resets_at is None or not window_mins:
            continue
        return _accounts._pace_eta(
            used_pct / 100.0, int(resets_at), now, window_s=float(window_mins) * 60.0
        )
    return None, None


def _build_subscriptions(
    accounts: list[dict], lanes_view: dict[str, Any], now: float
) -> list[dict]:
    """One row per subscription, whatever measures it.

    The Accounts and Subscriptions tables answered the SAME question — how much
    budget is left on each subscription — and were split only by where the
    number came from: Anthropic through this proxy's own bearers, everything
    else through the out-of-process lane report. That split also forced a
    content-free `anthropic:proxy · delegated · "live at the throttle proxy"`
    row whose only job was to point at the other table (Pedro, 04/08/2026:
    "the accounts and subscriptions sections are redundant").
    """
    rows: list[dict] = []
    for account in accounts:
        meters = [
            m
            for m in (
                _window_meter("5h", account.get("win5")),
                _window_meter("7d", account.get("win7")),
                _window_meter("7d sonnet", account.get("sonnet")),
                _window_meter("7d opus", account.get("opus")),
            )
            if m is not None
        ]
        extra = account.get("extra") or {}
        if extra.get("used") is not None:
            meters.append(
                {
                    "label": "credits",
                    "pct": None,
                    "reset_in": "",
                    "note": f"{extra['used']:.2f} {extra.get('currency') or ''}".strip(),
                }
            )
        status, detail = _account_status(account)
        rows.append(
            {
                "id": account.get("label") or "?",
                # One identity scheme across every table. The file-label is a
                # letter (A/B/C) that collides across families — anthropic A is
                # pedrobalbino@proton.me while codex:a is phsb5321@gmail.com —
                # so the EMAIL is the identity and the letter is the tag.
                "identity": account.get("email") or account.get("label") or "?",
                "sub": account.get("email") or "",
                # Carried so the status strip can point at THIS row as the
                # binding constraint instead of naming a bare hash.
                "bearer_id": account.get("bearer_id") or "",
                "family": "anthropic",
                "plan": "",
                "src": account.get("src") or "",
                "meters": meters,
                "pace": account.get("pace"),
                "pace_warn": bool(account.get("pace_warn")),
                "eta": account.get("eta") or "",
                "status": status,
                "detail": detail,
            }
        )
    for lane in lanes_view.get("lanes") or []:
        # Anthropic is measured above, per account, from live bearer state. The
        # report's placeholder row for it carries no meter by construction.
        if lane.get("kind") == "anthropic":
            continue
        meters = [
            {
                "label": m.get("label") or "?",
                "pct": m.get("used_pct"),
                "reset_in": m.get("reset_in") or "",
                # A meter may carry its own note (a pay-go lane's remaining
                # balance); `unlimited` is just the oldest one.
                "note": m.get("note") or ("unlimited" if m.get("unlimited") else ""),
                "exhausted_ok": bool(m.get("exhausted_ok")),
            }
            for m in lane.get("meters") or []
        ]
        pace, eta = _lane_pace_eta(lane, now)
        rows.append(
            {
                "id": lane.get("id") or "?",
                "identity": lane.get("id") or "?",
                "sub": "",
                "family": lane.get("family") or "",
                "plan": lane.get("plan") or "",
                "src": "report",
                "meters": meters,
                "pace": pace,
                "pace_warn": pace is not None and pace >= _accounts.PACE_WARN,
                "eta": eta or "",
                "status": lane.get("status") or "unknown",
                "detail": lane.get("reason") or "",
            }
        )

    # Pace answers "will this last the window". Once the window has already
    # refused, it did not, and the columns become noise: account B rendered
    # `1.08× · exhausts in <1m` next to a REJECTED badge, which reads like a
    # prediction about a thing that has already happened. The reopen time is
    # the live fact for those rows, and the binding strip already carries it.
    for row in rows:
        if row.get("status") in _CLOSED_STATUSES:
            row["pace"], row["pace_warn"], row["eta"] = None, False, ""

    # Grouped by family, fullest first WITHIN each group. A single global
    # fullest-first sort interleaved the providers (`B · copilot · A · codex:b ·
    # C · codex:a`), so the eye could not scan "how is Anthropic doing" without
    # reading every row. Which family is most pressed still leads, because a
    # group sorts by its own fullest member.
    def _binding(row: dict) -> float:
        readings = [m["pct"] for m in row["meters"] if m.get("pct") is not None]
        return max(readings) if readings else -1.0

    worst_in_family: dict[str, float] = {}
    for row in rows:
        family = row.get("family") or ""
        worst_in_family[family] = max(worst_in_family.get(family, -1.0), _binding(row))
    rows.sort(
        key=lambda r: (
            -worst_in_family.get(r.get("family") or "", -1.0),
            r.get("family") or "",
            -_binding(r),
            r.get("id") or "",
        )
    )
    return rows


# Statuses where the subscription is already refusing, so a burn projection is
# a prediction about the past. `locked` is deliberately absent: a usage cooldown
# is temporary and the window itself still has budget.
_CLOSED_STATUSES = frozenset({"rejected", "exhausted", "refused", "error"})


def _retry_after_text(last_ratelimit: dict | None) -> str:
    """Humanize the honoured Retry-After; empty when there is none to show."""
    raw = (last_ratelimit or {}).get("retry-after")
    if raw in (None, ""):
        return ""
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    return _accounts._fmt_duration(seconds) if seconds > 0 else "0s"


def _attach_binding(status: dict, subscriptions: list[dict]) -> None:
    """Turn the binding bearer hash into a named row plus a way out.

    "binding: 7d window 100% on b144f62f" tells the operator a hash. What they
    need mid-incident is which paid subscription is blocked, when its window
    reopens, and which subscription should take traffic meanwhile — so the
    binding row is resolved against the subscription table, and the freest
    serving sibling is named next to it.
    """
    bound = status.get("binding")
    if not isinstance(bound, dict):
        return
    for row in subscriptions:
        if row.get("bearer_id") and row["bearer_id"] == bound["bearer_id"]:
            row["is_binding"] = True
            bound["subscription"] = row["id"]
            bound["sub"] = row.get("sub") or ""
            for meter in row.get("meters") or []:
                if meter.get("label") == bound["window"]:
                    bound["resets_in"] = meter.get("reset_in") or ""
            break
    # Next usable: an Anthropic sibling that is serving, ranked by how much
    # room it has left. Nothing to suggest is itself the answer — say so rather
    # than leaving the operator to scan for a row that does not exist.
    candidates = [
        row
        for row in subscriptions
        if row.get("family") == "anthropic"
        and not row.get("is_binding")
        # "unseen" is fine — no traffic yet is not a fault. "refused" is not:
        # naming a quarantined credential as what takes traffic next sends the
        # operator at an account that answers 403 on every request (measured
        # live 05/08/2026: account A, org-policy refusal, offered as next).
        and row.get("status") in {"ok", "unseen"}
    ]

    def _fill(row: dict) -> float:
        readings = [m["pct"] for m in row.get("meters") or [] if m.get("pct") is not None]
        return max(readings) if readings else 0.0

    candidates.sort(key=_fill)
    if candidates:
        bound["next_usable"] = candidates[0]["id"]
        bound["next_usable_pct"] = round(_fill(candidates[0]))


async def _collect_view() -> dict[str, object]:
    """Snapshot the proxy's globals into a JSON-safe view for the template."""
    cs = _proxy.state["central_status"]
    labels = _accounts.bearer_labels()
    now = time.time()
    bearers = []
    # _anon is the unauthenticated bypass slot (health checks, /metrics). It
    # has no account, no budget window and no retry state — a row of dashes
    # that reads as plumbing. Keep it out of the operator's bearer table.
    anon_bid = "_anon"  # ratelimit._bearer_id's unauthenticated bypass slot
    for bid, bstate in _proxy.bearer_state.items():
        if bid == anon_bid or bid == "_anon":
            continue
        lim = _proxy.bearer_limiters.get(bid)
        unified = bstate.get("unified")
        bearers.append(
            {
                "bearer_id": bid,
                "account": labels.get(bid),
                "inflight": bstate.get("inflight", 0),
                "queued": bstate.get("queued", 0),
                "served": bstate.get("served", 0),
                "last_ratelimit": bstate.get("last_ratelimit"),
                # `148806` in a column headed "retry-after" is a number the
                # operator has to divide by 3600 mid-incident. It is 41h 20m.
                "retry_after_in": _retry_after_text(bstate.get("last_ratelimit")),
                "unified": unified,
                # 5h reading frozen from before its own reset — render as
                # "0% · reset" so the bearer column matches the accounts panel.
                "unified_5h_stale": _window_stale(unified, "reset_5h", now),
                # A credential the upstream REFUSES (403 org-policy, revoked
                # token) is not a budget state: it has no window and no
                # Retry-After, so every "idlest account" ranking reads it as
                # the freest thing on the fleet. The proxy already quarantines
                # it (#168); the page has to say so.
                # `_bearer_credential`, not `bstate["credential"]`: a restart
                # restores the quarantine into `_restored_credentials` and
                # leaves bearer_state clean, so reading bstate alone showed a
                # 403-refused account as healthy until it happened to be
                # re-probed. /__throttle/health already reads it this way —
                # measured live 05/08/2026, the page offered the org-dead
                # account as "takes traffic next" right after a restart.
                "credential": _proxy._bearer_credential(bid) or None,
                "limiter": lim.snapshot() if lim is not None else None,
            }
        )
    endpoint = await _accounts.refresh_endpoint(now)
    accounts_view = _accounts.account_view(bearers, now, endpoint)
    # Same identity scheme as the subscriptions table: email first.
    email_by_bid = {
        a["bearer_id"]: a["email"] for a in accounts_view if a.get("bearer_id") and a.get("email")
    }
    for b in bearers:
        b["identity"] = email_by_bid.get(b["bearer_id"]) or b.get("account")
    identity = _accounts.identity_state(accounts_view)
    _publish_account_gauges(endpoint, identity)
    # Fleet + Copilot are concurrent with the account refresh — both are
    # failure-tolerant (a down sibling / 403 org renders as such, never raises).
    # return_exceptions: a future regression in one panel must never blank the
    # other panels or the bearer table — coerce any exception to an empty list
    # (panel hides) rather than a 500.
    fleet_raw, copilot_raw = await asyncio.gather(
        _fleet.refresh(now), _copilot.refresh(now), return_exceptions=True
    )
    fleet_view = fleet_raw if isinstance(fleet_raw, list) else []
    copilot_view = copilot_raw if isinstance(copilot_raw, list) else []
    status = _compute_status(bearers, _proxy.QUEUE_MODE, now)
    central_url = _proxy.CENTRAL_URL or "(direct)"
    providers = _build_providers(
        upstream=_proxy.UPSTREAM,
        central_url=central_url,
        central_status=cs,
        level=str(status.get("level", "idle")),
        inflight=_proxy.state["inflight"],
        queued=_proxy.state["queued"],
        served=_proxy.state["served"],
        max_concurrent=_proxy.MAX_CONCURRENT,
        fleet=fleet_view,
    )
    lanes_view = _lanes.view(now)
    _publish_lane_gauges(lanes_view)
    subscriptions = _build_subscriptions(accounts_view, lanes_view, now)
    _attach_binding(status, subscriptions)
    return {
        "signals": _signals.collect(),
        "subscriptions": subscriptions,
        "identity": identity,
        "providers": providers,
        "lanes": lanes_view,
        "copilot": copilot_view,
        "status": status,
        "inflight": _proxy.state["inflight"],
        "queued": _proxy.state["queued"],
        # Streams parked in an SSE keepalive-hold. Peer of inflight/queued, not
        # a total: a held request is already answered 200 and is counted by
        # neither, so without this row the operator's screen shows an idle proxy
        # while it is holding streams open (spec 092 T003).
        "holds": _proxy.state["keepalive_holds_active"],
        "served": _proxy.state["served"],
        "disconnects": _proxy.state["client_disconnects"],
        "retries": _proxy.state["upstream_retries"],
        "max_concurrent": _proxy.MAX_CONCURRENT,
        "queue_mode": _proxy.QUEUE_MODE,
        "min_dispatch_gap_ms": int(_proxy.MIN_DISPATCH_GAP_S * 1000),
        "upstream": _proxy.UPSTREAM,
        "central_url": central_url,
        "central_status": cs,
        "bearers": bearers,
        "advisor_enabled": os.environ.get("ADVISOR_ENABLED", "false").lower() == "true",
        "last_advisor": _proxy.state.get("last_advisor"),
    }


async def index(
    request: web.Request,
) -> web.Response:
    """GET /ui — render the full HTMX dashboard page."""
    return aiohttp_jinja2.render_template(
        "dashboard.html", request, {**await _collect_view(), "asset_v": _ASSET_V}
    )


async def stats_partial(
    request: web.Request,
) -> web.Response:
    """GET /ui/stats — render the live stats ``<table>`` partial (hx-polled)."""
    return aiohttp_jinja2.render_template("partials/stats.html", request, await _collect_view())


async def advisor(request: web.Request) -> web.Response:
    """POST /ui/advisor — ask GROQ to recommend knob tweaks.

    Always returns 200 with a rendered HTML partial so HTMX swaps the
    response into ``#advisor-out`` regardless of error state. Returning
    non-2xx would leave the dashboard's response area silently empty,
    which Pedro reported on 27/05/2026 ("groq integration that does not
    work" — the integration *did* work, but errors landed off-screen).
    """
    if os.environ.get("ADVISOR_ENABLED", "false").lower() != "true":
        return aiohttp_jinja2.render_template(
            _TPL_ADVISOR,
            request,
            {
                "recommendation": None,
                "snapshot": None,
                "error": (
                    "Advisor is disabled. Set `ADVISOR_ENABLED=true` and "
                    "`GROQ_API_KEY` (proxy reads them from the EnvironmentFile "
                    "at ~/.local/state/anthropic-throttle-proxy/groq.env), "
                    "then restart the service."
                ),
            },
        )
    # Lazy import — keeps the advisor (and its HTTP client) off the hot path.
    from .advisor_impl import recommend

    snapshot = await _collect_view()
    try:
        recommendation = await recommend(snapshot)
    except Exception as exc:
        return aiohttp_jinja2.render_template(
            _TPL_ADVISOR,
            request,
            {
                "recommendation": None,
                "snapshot": snapshot,
                "error": f"Advisor call failed: {exc!s}",
            },
        )
    return aiohttp_jinja2.render_template(
        _TPL_ADVISOR,
        request,
        {"recommendation": recommendation, "snapshot": snapshot, "error": None},
    )


async def config_form(
    request: web.Request,
) -> web.Response:
    """GET /ui/config — render the editable-knobs form partial."""
    return aiohttp_jinja2.render_template(
        _TPL_CONFIG,
        request,
        {"knobs": _config.knob_snapshot(), "message": None, "error": None},
    )


async def config_set(request: web.Request) -> web.Response:
    """POST /ui/config — apply one knob's runtime override (form data: ``key``, ``value``).

    Returns the re-rendered config partial with a status message. HTMX swaps
    the section in place; no page reload.
    """
    form = await request.post()
    key = str(form.get("key", "")).strip()
    raw_value = form.get("value", "")
    message: str | None = None
    error: str | None = None
    if not key:
        error = "missing 'key' field"
    else:
        try:
            value = _config.set_override(key, raw_value)
            message = f"{key} → {value}"
        except KeyError as exc:
            error = f"unknown knob: {exc!s}"
        except (ValueError, TypeError) as exc:
            error = f"invalid value: {exc!s}"
    return aiohttp_jinja2.render_template(
        _TPL_CONFIG,
        request,
        {"knobs": _config.knob_snapshot(), "message": message, "error": error},
    )


async def config_reset(request: web.Request) -> web.Response:
    """POST /ui/config/reset — drop one knob's runtime override.

    Restores the env-default value for the named knob and removes the entry
    from the persisted overrides file. Returns the re-rendered partial.
    """
    form = await request.post()
    key = str(form.get("key", "")).strip()
    message: str | None = None
    error: str | None = None
    try:
        restored = _config.reset_override(key)
        message = f"{key} reset → {restored}"
    except KeyError as exc:
        error = f"unknown knob: {exc!s}"
    return aiohttp_jinja2.render_template(
        _TPL_CONFIG,
        request,
        {"knobs": _config.knob_snapshot(), "message": message, "error": error},
    )


# Background cadence for the account-endpoint refresher: keeps /metrics
# gauges + the email cache warm with NO dashboard viewer. 300s matches the
# polling guidance the usage endpoint tolerates comfortably (its own TTL
# inside refresh_endpoint additionally dedupes against dashboard renders).
_REFRESH_INTERVAL_S = 300.0


async def _account_refresh_loop() -> None:
    """Slow loop publishing account endpoint truth to /metrics."""
    log = logging.getLogger("throttle.ui.accounts")
    while True:
        try:
            now = time.time()
            endpoint = await _accounts.refresh_endpoint(now)
            view = _accounts.account_view([], now, endpoint)
            _publish_account_gauges(endpoint, _accounts.identity_state(view))
        except Exception as exc:  # noqa: BLE001 — a UI nicety must never crash the app
            log.debug("account endpoint refresh failed: %s", exc)
        await asyncio.sleep(_REFRESH_INTERVAL_S)


async def _start_account_refresher(
    app: web.Application,
) -> None:
    if _accounts.parse_spec(_config.ACCOUNT_CRED_PATHS):
        app["_account_refresher"] = asyncio.create_task(_account_refresh_loop())


async def _stop_account_refresher(app: web.Application) -> None:
    await _cancel(app.get("_account_refresher"))


async def _cancel(task: asyncio.Task | None) -> None:
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _live_cap() -> int:
    """Sum of every bearer's CURRENT AIMD ceiling — what can dispatch right now.

    Read without the limiter lock on purpose: this is a 10-second sample of a
    number that changes on pushback, and blocking the event loop for it would
    break the <50 ms health budget the same loop serves.
    """
    return sum(lim.max_concurrent for lim in list(_proxy.bearer_limiters.values()))


def _counter(key: str) -> int:
    """Read one of ``proxy.state``'s integer counters. ``state`` is typed
    ``dict[str, object]`` because it also holds strings and the advisor dict."""
    return cast(int, _proxy.state[key])


async def _history_sample_loop() -> None:
    """Close one history bucket per ``RESOLUTION_S`` so /ui has a time axis."""
    log = logging.getLogger("throttle.ui.history")
    while True:
        await asyncio.sleep(_history.RESOLUTION_S)
        try:
            _history.record(
                queued=_counter("queued"),
                inflight=_counter("inflight"),
                cap=_live_cap(),
            )
        # A sparkline must never take the proxy down, but the catch stays
        # narrow: these are the failures a shifting `state`/limiter shape can
        # actually produce, and anything else deserves to surface.
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            log.debug("history sample failed: %s", exc)


async def _start_history_sampler(app: web.Application) -> None:
    app["_history_sampler"] = asyncio.create_task(_history_sample_loop())


async def _stop_history_sampler(app: web.Application) -> None:
    await _cancel(app.get("_history_sampler"))


def attach_ui(app: web.Application) -> None:
    """Wire jinja2 + the /ui routes onto an existing aiohttp app."""
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(str(_TEMPLATES)))
    app.router.add_get("/ui", index)
    app.router.add_get("/ui/stats", stats_partial)
    app.router.add_get("/ui/config", config_form)
    app.router.add_post("/ui/config", config_set)
    app.router.add_post("/ui/config/reset", config_reset)
    app.router.add_post("/ui/advisor", advisor)
    app.router.add_static("/ui/static/", _STATIC, follow_symlinks=False)
    app.on_startup.append(_start_account_refresher)
    app.on_cleanup.append(_stop_account_refresher)
    app.on_startup.append(_start_history_sampler)
    app.on_cleanup.append(_stop_history_sampler)
