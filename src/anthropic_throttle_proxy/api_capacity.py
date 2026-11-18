"""API lane capacity: one read-only source of truth, ``api-capacity.v1``.

``GET /__throttle/api-capacity`` (JSON) and ``GET /ui/api-capacity`` (HTMX panel)
PROJECT the existing ``apiCredits`` schema-1 receipt (``THROTTLE_API_CREDITS_FILE``,
normalized read-only by ``api_credits.normalize``, the same call the router uses).
No parallel schema: per account it keeps every field ``normalize`` yields and labels
each one individually — credit (status, remaining + currency only when normalize
proves them, expiresAt), rateLimits[] (rpm/tpm per model/scope), spendLimit
(remaining/enforced), observedAt/validUntil and source. Beside it:

* ``subscription`` — a link to the existing window source (``/__throttle/admission``
  ``/weekly_pacing``). Quota percent is never recomputed, summed or co-plotted with USD.
* Remote access: both routes use ``ui.auth.guard`` (PR #28) when that gate exists.
  Until it lands, this module fails CLOSED instead: a remote or relayed peer is
  refused outright (`remote_access_gate: "loopback-only"`), because a credits
  projection on the Dokku instance must never be readable without a token.
* ``header_lanes`` — the ``anthropic-ratelimit-*`` headers the gateway already keeps in
  ``bearer_state[bid]["last_ratelimit"]`` for API-key bearers.

Freshness is per meter and comes only from when the value was observed, never the
write or render time. A missing, invalid or future observation is never fresh, so a
frozen receipt ages into ``stale`` and shows no amount as current. A refused or
insufficient-credit receipt carries ``remaining: null`` ("balance unknown"), never 0.

Nothing here calls a provider, reads a key or logs. Raw accountId / workspaceId and
bearer ids never leave the module: accounts appear by their alias plus a short hash.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

import aiohttp_jinja2
from aiohttp import web

from . import api_credits

try:  # PR #28's token gate owns remote access once it lands; until then this module
    from .ui import auth as _auth  # refuses remote peers itself, fail closed.
except ImportError:  # pragma: no cover - exercised through `_guarded`
    _auth = None

CAPACITY_SCHEMA: Final[str] = "api-capacity.v1"
ENV_RATE_MAX_AGE: Final[str] = "THROTTLE_API_RATELIMIT_MAX_AGE_S"
DEFAULT_RATE_MAX_AGE_S: Final[int] = 300
MAX_CREDITS_BYTES: Final[int] = 256 * 1024  # same bound as the router's read
SUBSCRIPTION_HREF: Final[str] = "/__throttle/admission"
SUBSCRIPTION_POINTER: Final[str] = "/weekly_pacing"
_RATE_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("requests_limit", "anthropic-ratelimit-requests-limit"),
    ("requests_remaining", "anthropic-ratelimit-requests-remaining"),
    ("tokens_limit", "anthropic-ratelimit-tokens-limit"),
    ("tokens_remaining", "anthropic-ratelimit-tokens-remaining"),
    ("input_tokens_remaining", "anthropic-ratelimit-input-tokens-remaining"),
    ("output_tokens_remaining", "anthropic-ratelimit-output-tokens-remaining"),
    ("retry_after_s", "retry-after"),
)
_RESET_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("requests_reset", "anthropic-ratelimit-requests-reset"),
    ("tokens_reset", "anthropic-ratelimit-tokens-reset"),
    ("input_tokens_reset", "anthropic-ratelimit-input-tokens-reset"),
    ("output_tokens_reset", "anthropic-ratelimit-output-tokens-reset"),
)
_API_KEY_FAMILY: Final[frozenset[str]] = frozenset(h for _, h in _RATE_FIELDS[:4])


def _env_seconds(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def parse_ts(value: Any) -> float | None:
    """Epoch seconds from an ISO 8601 string WITH an offset; anything else is None."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def freshness(observed: float | None, now: float, max_age_s: int) -> dict[str, Any]:
    """Per-meter staleness from observed_at alone. Future or absent is never fresh."""
    if observed is None:
        return {"observed_at": None, "age_s": None, "fresh": False, "freshness": "unobserved"}
    age = now - observed
    if age < 0:
        verdict = "future"
    else:
        verdict = "fresh" if age <= max_age_s else "stale"
    return {
        "observed_at": _iso(observed),
        "age_s": int(age),
        "fresh": verdict == "fresh",
        "freshness": verdict,
    }


def _int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _is_api_key_meta(meta: Mapping[str, Any]) -> bool:
    keys = set(meta)
    return bool(keys & _API_KEY_FAMILY) and not any(
        k.startswith("anthropic-ratelimit-unified") for k in keys
    )


def _rate_meter(bstate: Mapping[str, Any], now: float, max_age_s: int) -> dict[str, Any]:
    meta = bstate.get("last_ratelimit")
    meta = meta if isinstance(meta, Mapping) else {}
    stamp = bstate.get("last_ratelimit_at")
    observed = float(stamp) if isinstance(stamp, int | float) and stamp > 0 else None
    meter = freshness(observed, now, max_age_s)
    values: dict[str, Any] = {name: _int(meta.get(header)) for name, header in _RATE_FIELDS}
    resets = {name: parse_ts(meta.get(header)) for name, header in _RESET_FIELDS}
    values.update({name: _iso(ts) if ts is not None else None for name, ts in resets.items()})
    known = [ts for ts in resets.values() if ts is not None]
    reset_passed = bool(known) and max(known) <= now
    if reset_passed:
        # The window these numbers describe has already rolled over.
        meter["fresh"] = False
    return {
        "state": "observed" if any(v is not None for v in values.values()) else "absent",
        "header_family": "anthropic-ratelimit",
        "values": values,
        "reset_passed": reset_passed,
        **meter,
    }


def _slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]
    return slug or "api"


def _rate_lanes(
    bearer_state: Mapping[str, Any], candidate_bid: str | None, api_label: str
) -> list[tuple[str, Mapping[str, Any]]]:
    """API-key bearers → (lane id, state). Bearer ids never leave this function."""
    named: list[tuple[str, Mapping[str, Any]]] = []
    observed: list[tuple[str, Mapping[str, Any]]] = []
    for bid, bstate in sorted(list(bearer_state.items())):
        if not isinstance(bstate, Mapping):
            continue
        meta = bstate.get("last_ratelimit")
        meta = meta if isinstance(meta, Mapping) else {}
        if bid == "api-key":
            named.append(("api:caller", bstate))
        elif candidate_bid is not None and bid == candidate_bid:
            named.append((f"api:{_slug(api_label)}", bstate))
        elif _is_api_key_meta(meta):
            observed.append((f"api:observed-{len(observed) + 1}", bstate))
    return named + observed


def _link_meter() -> dict[str, Any]:
    return {
        "state": "linked",
        "href": SUBSCRIPTION_HREF,
        "pointer": SUBSCRIPTION_POINTER,
        "observed_at": None,
        "age_s": None,
        "fresh": False,
        "freshness": "linked",
    }


_KEY_SHAPED = re.compile(r"(sk-|bearer)", re.IGNORECASE)


def _safe_alias(value: str) -> str | None:
    """The collector's alias, unless it is shaped like a key (same rule as the footer)."""
    return None if not value or _KEY_SHAPED.match(value) else value


def _ref(value: str) -> str:
    """Short, stable reference for a raw account/workspace id; the raw id stays here."""
    return "h:" + hashlib.sha256(value.encode()).hexdigest()[:8] if value else ""


_CREDIT_LABEL: Final[dict[str, str]] = {
    "available": "fresh",
    "pending": "pending",
    "expired": "expired",
    "stale": "stale",
}


def _value_label(value: Any, fresh: bool) -> str:
    if not fresh:
        return "stale"
    return "fresh" if value is not None else "unknown"


def _observation(account: Mapping[str, Any], now: float) -> dict[str, Any]:
    """When THIS receipt was observed. ``normalize`` marks it stale unless
    observedAt <= now < validUntil, so a future or frozen receipt is never fresh."""
    observed = parse_ts(account["observedAt"])
    fresh = account["credit"]["status"] != "stale"
    if observed is None:
        verdict = "unobserved"
    elif observed > now:
        verdict = "future"
    else:
        verdict = "fresh" if fresh else "stale"
    return {
        "observed_at": account["observedAt"],
        "valid_until": account["validUntil"],
        "age_s": int(now - observed) if observed is not None else None,
        "fresh": fresh and verdict == "fresh",
        "freshness": verdict,
    }


def _credit_meter(account: Mapping[str, Any], obs: Mapping[str, Any]) -> dict[str, Any]:
    credit = account["credit"]
    remaining = credit["remaining"]  # normalize proves it (fresh + available) or nulls it
    currency = account["currency"] if remaining is not None else None
    display = f"{currency} {remaining:.2f}" if remaining is not None else "balance unknown"
    label = _CREDIT_LABEL.get(credit["status"], "unknown")
    return {
        "status": credit["status"],
        "label": label,
        "remaining": remaining,
        "currency": currency,
        "display": display if label in ("fresh", "unknown") else f"{display} · {label}",
        "expires_at": credit["expiresAt"],
        **obs,
    }


def _rate_limit_meters(account: Mapping[str, Any], obs: Mapping[str, Any]) -> list[dict]:
    return [
        {
            "model": rate["model"] or None,
            "scope": rate["scope"] or None,
            "rpm_remaining": rate["rpmRemaining"],
            "rpm_label": _value_label(rate["rpmRemaining"], obs["fresh"]),
            "tpm_remaining": rate["tpmRemaining"],
            "tpm_label": _value_label(rate["tpmRemaining"], obs["fresh"]),
            **obs,
        }
        for rate in account["rateLimits"]
    ]


def _spend_limit_meter(account: Mapping[str, Any], obs: Mapping[str, Any]) -> dict[str, Any]:
    spend = account["spendLimit"]
    remaining = spend["remaining"]
    return {
        "remaining": remaining,
        "remaining_label": _value_label(remaining, obs["fresh"]),
        "currency": account["currency"] if remaining is not None else None,
        "enforced": spend["enforced"],
        "enforced_label": _value_label(spend["enforced"], obs["fresh"]),
        **obs,
    }


def project_account(account: Mapping[str, Any], now: float) -> dict[str, Any]:
    """One normalized ``apiCredits`` account → per-field-labelled meters."""
    obs = _observation(account, now)
    return {
        "id": _safe_alias(account["id"]),
        "provider": account["provider"],
        "source": account["source"] or None,
        "account_ref": _ref(account["accountId"]),
        "workspace_ref": _ref(account["workspaceId"]),
        "authorized": account["authorized"],
        **obs,
        "meters": {
            "credit": _credit_meter(account, obs),
            "rate_limits": _rate_limit_meters(account, obs),
            "spend_limit": _spend_limit_meter(account, obs),
            "subscription": _link_meter(),
        },
    }


def read_credits(path: str, now: float) -> tuple[str, dict[str, Any]]:
    """Bounded read of the existing receipt, normalized read-only (router's call)."""
    empty = api_credits.normalize(None, now)
    if not path:
        return "unconfigured", empty
    try:
        with open(path, "rb") as fh:
            encoded = fh.read(MAX_CREDITS_BYTES + 1)
    except FileNotFoundError:
        return "absent", empty
    except OSError:
        return "unreadable", empty
    if len(encoded) > MAX_CREDITS_BYTES:
        return "too_large", empty
    try:
        raw = json.loads(encoded)
    except ValueError:
        return "invalid", empty
    return "ok", api_credits.normalize(
        raw.get("apiCredits", raw) if isinstance(raw, dict) else raw, now
    )


def _hashed(normalized: Mapping[str, Any]) -> dict[str, Any]:
    """The normalized shape (consumable by the Pi footer) with raw ids replaced."""
    return {
        "schema": normalized["schema"],
        "accounts": [
            {
                **a,
                "id": _safe_alias(a["id"]) or "redacted",
                "accountId": _ref(a["accountId"]),
                "workspaceId": _ref(a["workspaceId"]),
            }
            for a in normalized["accounts"]
        ],
    }


def snapshot(
    *,
    credits: tuple[str, dict[str, Any]],
    bearer_state: Mapping[str, Any],
    candidate_bid: str | None,
    api_label: str,
    now: float,
    rate_max_age_s: int = DEFAULT_RATE_MAX_AGE_S,
    gate: str = "none",
) -> dict[str, Any]:
    """Pure: build the ``api-capacity.v1`` document from already-read inputs."""
    state, normalized = credits
    return {
        "schema": CAPACITY_SCHEMA,
        "generated_at": _iso(now),
        "credits_file": {"state": state, "schema": "apiCredits/1"},
        "accounts": [project_account(a, now) for a in normalized["accounts"]],
        "header_lanes": [
            {"lane": lane, "rate_limit": _rate_meter(bstate, now, rate_max_age_s)}
            for lane, bstate in _rate_lanes(bearer_state, candidate_bid, api_label)
        ],
        "subscription": _link_meter(),
        "ratelimit_max_age_s": rate_max_age_s,
        "remote_access_gate": gate,
        "apiCredits": _hashed(normalized),
    }


def _live_inputs() -> dict[str, Any]:
    from . import config, proxy

    cache = getattr(proxy, "_api_key_cache", None)
    return {
        "credits_path": config.API_CREDITS_FILE,
        "bearer_state": config.bearer_state,
        "candidate_bid": cache[2] if isinstance(cache, tuple) and len(cache) > 2 else None,
        "api_label": config.API_KEY_LABEL,
    }


_RELAY_HEADERS: Final[tuple[str, ...]] = ("Forwarded", "X-Forwarded-For", "X-Real-IP")


def _is_remote(request: web.Request) -> bool:
    """Same reading as ``ui.auth``: a relayed peer counts as remote, not loopback."""
    if any(request.headers.get(name) for name in _RELAY_HEADERS):
        return True
    peer = request.remote or ""
    # An unknown peer is remote (fail closed), not loopback by accident.
    return not (peer == "::1" or peer.startswith("127."))


def _loopback_only(handler: Any) -> Any:
    """Fail-closed stand-in for ``ui.auth.guard`` while PR #28 is unmerged."""

    async def _refuse_remote(request: web.Request) -> web.StreamResponse:
        if _is_remote(request):
            # The reason is the policy, never the receipt: no id, amount or path.
            return web.Response(
                text="api-capacity: remote access needs the UI token gate\n",
                status=403,
                content_type="text/plain",
            )
        return await handler(request)

    return _refuse_remote


def _guarded(handler: Any) -> Any:
    return _auth.guard(handler) if _auth is not None else _loopback_only(handler)


def gate_state() -> str:
    """What happens to a remote peer right now (loopback is always open)."""
    if _auth is None:
        return "loopback-only"
    # An unreadable token file also reports "ui-auth": the gate then fails closed,
    # which is stricter than requiring the token, never weaker.
    return "ui-auth" if _auth.current_gate().enabled else "none"


def current() -> dict[str, Any]:
    now = time.time()
    live = _live_inputs()
    return snapshot(
        credits=read_credits(live.pop("credits_path"), now),
        now=now,
        rate_max_age_s=_env_seconds(ENV_RATE_MAX_AGE, DEFAULT_RATE_MAX_AGE_S),
        gate=gate_state(),
        **live,
    )


_TONE: Final[dict[str, str]] = {"fresh": "ok", "pending": "warn", "unknown": "warn"}


def _esc(value: Any) -> str:
    return "—" if value is None else html.escape(str(value))


def _tag(label: str) -> str:
    return "" if label == "fresh" else f' <span class="muted">{html.escape(label)}</span>'


def _rates_cell(rates: list[Mapping[str, Any]]) -> str:
    if not rates:
        return '<span class="muted">no limits recorded</span>'
    return "<br>".join(
        f"<code>{_esc(r['model'])}@{_esc(r['scope'])}</code> "
        f"RPM {_esc(r['rpm_remaining'])}{_tag(r['rpm_label'])} · "
        f"TPM {_esc(r['tpm_remaining'])}{_tag(r['tpm_label'])}"
        for r in rates
    )


def _spend_cell(spend: Mapping[str, Any]) -> str:
    amount = (
        f"{_esc(spend['currency'])} {spend['remaining']:.2f}"
        if spend["remaining"] is not None
        else "unknown"
    )
    enforced = {True: "enforced", False: "advisory", None: "unknown enforcement"}
    return (
        f"{amount}{_tag(spend['remaining_label'])} · "
        f"{enforced[spend['enforced']]}{_tag(spend['enforced_label'])}"
    )


def _account_row(acct: Mapping[str, Any]) -> str:
    m = acct["meters"]
    credit = m["credit"]
    tone = "crit" if credit["status"] == "refused" else _TONE.get(credit["label"], "warn")
    return (
        f'<tr><td><code>{_esc(acct["id"])}</code> <span class="muted">'
        f"{_esc(acct['account_ref'])}</span></td><td>{_esc(acct['provider'])}</td>"
        f'<td><span class="{tone}">{html.escape(credit["display"])}</span>'
        f'<br><span class="muted">status {_esc(credit["status"])} · expires '
        f"{_esc(credit['expires_at'])}</span></td>"
        f"<td>{_rates_cell(m['rate_limits'])}</td><td>{_spend_cell(m['spend_limit'])}</td>"
        f"<td>{_esc(acct['observed_at'])}{_tag(acct['freshness'])}"
        f'<br><span class="muted">valid until {_esc(acct["valid_until"])}</span></td>'
        f"<td>{_esc(acct['source'])}</td></tr>"
    )


def _header_row(lane: Mapping[str, Any]) -> str:
    rate = lane["rate_limit"]
    values = rate["values"]
    return (
        f"<tr><td><code>{_esc(lane['lane'])}</code></td>"
        f"<td>req {_esc(values['requests_remaining'])}/{_esc(values['requests_limit'])} · "
        f"tok {_esc(values['tokens_remaining'])}/{_esc(values['tokens_limit'])}</td>"
        f"<td>{_esc(rate['observed_at'])}{_tag(rate['freshness'])}</td></tr>"
    )


def _table(head: tuple[str, ...], rows: list[str], empty: str) -> str:
    th = "".join(f'<th scope="col">{h}</th>' for h in head)
    body = "".join(rows) or f'<tr><td colspan="{len(head)}" class="muted">{empty}</td></tr>'
    return (
        f'<table class="projects-table"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'
    )


def render_fragment(snap: Mapping[str, Any]) -> str:
    """Server-rendered panel; colours come from the dashboard's token classes.

    USD meters and the subscription window are separate units: the window is a
    link, never a number on this panel, and nothing here is ever totalled.
    """
    accounts = _table(
        ("account", "provider", "credit", "rate limits", "spend limit", "observed", "source"),
        [_account_row(a) for a in snap["accounts"]],
        "no apiCredits receipt account",
    )
    headers = _table(
        ("API-key lane", "rate-limit headers", "observed"),
        [_header_row(lane) for lane in snap["header_lanes"]],
        "no API-key rate-limit headers observed",
    )
    return (
        '<div class="projects-card">'
        '<div class="projects-head"><span><b>API credits &amp; limits</b> · read-only</span>'
        f"<span>receipt <b>{html.escape(snap['credits_file']['state'])}</b></span>"
        f'<span>subscription window: <a href="{SUBSCRIPTION_HREF}">separate meter</a></span>'
        f"<span>gate <b>{html.escape(snap['remote_access_gate'])}</b></span></div>"
        + accounts
        + headers
        + '<p class="muted">Each value carries its own freshness from when it was observed. '
        "A refused or insufficient-credit receipt is shown as balance unknown, never 0.</p>"
        "</div>"
    )


_PAGE: Final[str] = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>API capacity</title>'
    '<link rel="stylesheet" href="/ui/static/style.css">'
    '<script src="https://unpkg.com/htmx.org@1.9.12" '
    'integrity="sha384-ujb1lZYygJmzgSwoxRggbCHcjc0rB2XoQrxeTUQyRjrOnlCoYta87iKBWq3EsdM2" '
    'crossorigin="anonymous"></script>'
    '</head><body><main><section id="api-capacity" hx-get="/ui/api-capacity" '
    'hx-trigger="every 10s" hx-swap="innerHTML">{fragment}</section></main></body></html>'
)


def panel_enabled() -> bool:
    """The dashboard card renders when a receipt is configured at all.

    ``THROTTLE_API_CREDITS_FILE`` also drives the router's overflow gate, so the
    card is present exactly when the fleet has metered allowance to show.
    """
    return bool(os.environ.get("THROTTLE_API_CREDITS_FILE", "").strip())


def attach_api_capacity(app: web.Application) -> None:
    """Mount both routes and the dashboard's env-gated card (spec 380 pattern)."""

    async def capacity_json(_: web.Request) -> web.Response:
        return web.json_response(current())

    async def capacity_html(request: web.Request) -> web.Response:
        fragment = render_fragment(current())
        text = (
            fragment if request.headers.get("HX-Request") else _PAGE.replace("{fragment}", fragment)
        )
        return web.Response(text=text, content_type="text/html")

    app.router.add_get("/__throttle/api-capacity", _guarded(capacity_json))
    app.router.add_get("/ui/api-capacity", _guarded(capacity_html))


def attach_api_capacity_globals(app: web.Application) -> None:
    """Add the dashboard's env-gated card flag; call after ``attach_ui`` (jinja env)."""
    aiohttp_jinja2.get_env(app).globals["api_capacity_enabled"] = panel_enabled
