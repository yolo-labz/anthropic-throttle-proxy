"""Subscription-lane report reader — every meter this host pays for, in one pane.

The proxy renders Anthropic live because it ROUTES Anthropic. Every other
subscription the fleet spends (ChatGPT/Codex accounts, GitHub Copilot, and any
future lane) is invisible to it, which on 03/08/2026 meant two Anthropic
bearers sat 7d-REJECTED while a ChatGPT account sat at 0% and nothing on the
dashboard could say so.

NixOS ``modules.home.throttleLanes`` (PR #1590) already probes those meters out
of process on a user timer and publishes ``$XDG_RUNTIME_DIR/throttle-lanes.json``
(0600). This module only READS that file: no credential, no outbound call, no
vendor SDK — the same reasons the probes were kept out of the proxy in the
first place.

UNKNOWN IS NOT HEALTHY. A lane whose probe failed reports ``unknown`` with a
reason, never an absent key a consumer could coerce to "fine". A stale file is
the same class of lie, so a report older than ``2x intervalSeconds`` degrades
every lane to ``stale`` instead of rendering decades-old percentages as truth.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import accounts as _accounts

# Re-read no more than this often. The writer's timer is 900 s by default, so
# even 5 s is wildly over-eager; it exists only so a 2 s dashboard poll does
# not stat+parse the file 30 times a minute.
TTL_S = 5.0

# A report older than this multiple of its own declared interval is stale.
# 2x tolerates one missed timer tick (a laptop suspend, a slow probe) before
# it starts lying.
_STALE_INTERVALS = 2.0

# Lane kind -> model family. The family-diversity invariant (a generator is
# never reviewed by its own family) is only enforceable if the dashboard and
# the orchestrator agree on who is whose sibling.
_FAMILY = {
    "anthropic": "anthropic",
    "codex": "openai",
    "copilot": "github",
    "groq": "openai",
    "deepinfra": "chinese-frontier",
    "kimi": "chinese-frontier",
    # Z.AI/GLM and DeepSeek are one review family with Kimi/Qwen: a
    # Chinese-frontier generator may not be reviewed by another one.
    "zai": "chinese-frontier",
    "deepseek": "chinese-frontier",
}

_PROVIDER = {
    "anthropic": ("✳️", "Anthropic"),
    "claude": ("✳️", "Claude"),
    "codex": ("🌀", "Codex"),
    "zai": ("✨", "Z.AI"),
    "deepseek": ("🐋", "DeepSeek"),
    "copilot": ("🐙", "Copilot"),
    "groq": ("🚀", "Groq"),
    "deepinfra": ("🌙", "DeepInfra"),
    "kimi": ("🌙", "Kimi"),
}

_cache: tuple[float, dict[str, Any]] | None = None


def report_path() -> str:
    """Path of the lane report. ``THROTTLE_LANES_FILE`` overrides the default."""
    override = os.environ.get("THROTTLE_LANES_FILE", "").strip()
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    return f"{runtime}/throttle-lanes.json" if runtime else ""


def _pct(value: Any) -> float | None:
    """Coerce a percentage to float; anything non-numeric → None (unknown)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _epoch(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _window_meters(lane: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize provider windows without inventing a common quota unit.

    Codex reports request percentages; Z.AI reports Coding Plan credits. Both
    publish the same decision fields (used %, reset epoch, window duration),
    while allowance/remaining stay additive details for the Z.AI row.
    """
    out = []
    for meter in lane.get("meters") or []:
        if not isinstance(meter, dict):
            continue
        out.append(
            {
                "label": str(meter.get("limitId") or "?"),
                "used_pct": _pct(meter.get("usedPercent")),
                "resets_at": _epoch(meter.get("resetsAt")),
                "window_mins": meter.get("windowMins"),
                "allowance": meter.get("allowance"),
                "current": meter.get("current"),
                "remaining": meter.get("remaining"),
            }
        )
    return out


def _billing(lane: dict[str, Any], *, stale: bool) -> dict[str, Any] | None:
    """Allowlist non-secret billing facts from the report.

    In particular, ``paymentType=WAIT_PAY`` is preserved but never interpreted
    as success. The Nix writer's measured current-plan predicate is the verdict.
    """
    raw = lane.get("billing")
    if not isinstance(raw, dict):
        return None
    amount = raw.get("renewalAmount")
    amount = (
        float(amount) if isinstance(amount, int | float) and not isinstance(amount, bool) else None
    )
    currency = str(raw.get("currency") or "USD").upper()
    cycle = str(raw.get("cycle") or "")
    date = str(raw.get("nextRenewDate") or "")
    try:
        date_display = datetime.strptime(date, "%Y-%m-%d").strftime("%d/%m")
    except ValueError:
        date_display = date
    symbol = "$" if currency == "USD" else f"{currency} "
    suffix = "/mo" if cycle == "monthly" else ("/yr" if cycle == "annual" else "")
    return {
        # Staleness makes current-plan state unknown, not delinquent and not
        # paid. Keep the last raw facts for diagnosis but remove the verdict.
        "current": None if stale else raw.get("current") is True,
        "stale": stale,
        "plan_status": str(raw.get("planStatus") or "unknown"),
        "auto_renew": raw.get("autoRenew") is True,
        "cycle": cycle,
        "renewal_amount": amount,
        "renewal_label": f"{symbol}{amount:.0f}{suffix}" if amount is not None else "",
        "currency": currency,
        "next_renew_date": date,
        "next_renew_display": date_display,
        "payment_type": str(raw.get("paymentType") or "unknown"),
    }


def _copilot_meters(lane: dict[str, Any]) -> list[dict[str, Any]]:
    """Copilot reports REMAINING percent per quota; invert to used for parity.

    An ``unlimited`` quota has no meaningful fill level — it renders as a named
    row with ``used_pct=None`` rather than a fake 0%, so "unlimited" and "no
    reading" stay distinguishable.
    """
    out = []
    quotas = lane.get("quotas")
    if not isinstance(quotas, dict):
        return out
    for name, quota in quotas.items():
        if not isinstance(quota, dict):
            continue
        remaining = _pct(quota.get("percentRemaining"))
        unlimited = bool(quota.get("unlimited"))
        spent = remaining is not None and not unlimited and remaining <= 0
        out.append(
            {
                "label": str(name),
                "used_pct": None if unlimited or remaining is None else 100.0 - remaining,
                "resets_at": None,
                "unlimited": unlimited,
                # A finite quota that is fully spent does not kill a lane that
                # also carries unlimited windows (Copilot premium_interactions
                # vs chat/completions). The meter row says "spent"; the lane
                # badge says "ok" — two different subjects, no contradiction.
                "exhausted_ok": spent,
                "entitlement": quota.get("entitlement"),
            }
        )
    return out


def _balance_meters(lane: dict[str, Any]) -> list[dict[str, Any]]:
    """Pay-go lanes meter in money, and money has no ceiling to be a % of.

    DeepSeek/DeepInfra/Groq publish a remaining balance, not a utilisation. A
    percentage would have to invent the denominator, so the amount renders as a
    note with ``used_pct=None`` — the same shape Copilot's ``unlimited`` rows
    already use, which is why the template needs no new branch.

    This matters as much as any window: on 07/08/2026 this lane fell to $0.15
    with two Anthropic accounts 7d-rejected, and the panel that is supposed to
    show every subscription's remaining budget had no row for it at all.
    """
    balance = lane.get("balance")
    if not isinstance(balance, dict):
        return []
    total = balance.get("total")
    if isinstance(total, str):
        try:
            total = float(total)
        except ValueError:
            total = None
    if not isinstance(total, int | float):
        return []
    currency = str(balance.get("currency") or "USD").upper()
    symbol = "$" if currency == "USD" else f"{currency} "
    return [
        {
            "label": "balance",
            "used_pct": None,
            "resets_at": None,
            "note": f"{symbol}{total:.2f}",
            "balance_total": float(total),
        }
    ]


def _reset_in(resets_at: float | None, now: float) -> str:
    """Humanized countdown to a meter's reset; empty when unknown or elapsed."""
    if resets_at is None or resets_at <= now:
        return ""
    return _accounts._fmt_duration(resets_at - now)


def _exhausted_reason(meters: list[dict[str, Any]]) -> str:
    """Name the full meter and when it reopens — measured numbers, no paraphrase.

    Not simply ``meters[0]``: the sort is fullest-first but stable, so when two
    meters are BOTH full it keeps whatever order the probe wrote, and naming the
    one that happens to reopen first would promise the lane back while the other
    is still walled. Pick the full meter that reopens LAST — the pessimistic one
    is the only honest one. A meter with no reset reading sorts last of all,
    since "unknown" cannot be claimed to reopen at any time.
    """
    full = [m for m in meters if (m.get("used_pct") or 0.0) >= 100.0]
    if not full:
        return ""
    binding = max(full, key=lambda m: (m.get("resets_at") is None, m.get("resets_at") or 0.0))
    label = str(binding.get("label") or "?")
    # Report the measured figure, not the threshold: a provider can publish
    # over-quota usage (130%), and rounding that down to "100%" would understate
    # the breach on the one screen meant to prevent exactly that.
    pct = binding.get("used_pct")
    pct_text = f"{pct:.0f}%" if isinstance(pct, int | float) else "100%"
    reset_in = str(binding.get("reset_in") or "")
    if reset_in:
        return f"{label} meter at {pct_text} — reopens in {reset_in}"
    return f"{label} meter at {pct_text}"


def _normalize(lane: dict[str, Any], stale: bool, now: float) -> dict[str, Any]:
    kind = str(lane.get("kind") or "?")
    lane_id = str(lane.get("id") or "?")
    status = str(lane.get("status") or "unknown")
    if stale and status == "ok":
        status = "stale"
    meters = _window_meters(lane) if kind in {"codex", "zai"} else []
    if kind == "copilot":
        meters = _copilot_meters(lane)
    if not meters:
        # Any lane may carry a balance; kind decides only which OTHER meters it
        # has. Checked last so a window-metered lane keeps its windows.
        meters = _balance_meters(lane)
    for meter in meters:
        meter["reset_in"] = _reset_in(meter.get("resets_at"), now)
    # Fullest first: the meter that decides whether this lane can take the next
    # request must be the one the eye lands on. Unreadable meters sort last.
    meters.sort(key=lambda m: (m["used_pct"] is None, -(m["used_pct"] or 0.0)))
    plan = ""
    for meter in lane.get("meters") or []:
        if isinstance(meter, dict) and meter.get("planType"):
            plan = str(meter["planType"])
            break
    if not plan and isinstance(lane.get("plan"), str):
        plan = lane["plan"]
    # The binding meter is the fullest one — that is the number that decides
    # whether this lane can take the next request.
    filled = [m["used_pct"] for m in meters if m["used_pct"] is not None]
    binding_pct = max(filled) if filled else None
    # A full meter REFUSES. Measured 07/08/2026: with the shared `codex` meter
    # at 100%, `codex exec` answers "You've hit your usage limit ... try again
    # at Aug 8th, 2026 12:48 PM" — yet the row still read `ok`, because the
    # status came verbatim from the probe report and never looked at the
    # meters. That is the one thing this table must never do: render a lane
    # with no capacity as healthy. Guarded on `ok` so a worse verdict
    # (refused/error/stale) still wins — a stale 100% is untrusted, not proven
    # exhausted, and could already have reset.
    # An `unlimited` meter is live capacity that no percentage can express, so a
    # lane holding one is never exhausted — Copilot's premium bucket runs to 0
    # while chat and completions keep serving, and calling that lane dead would
    # be the opposite lie.
    has_unlimited = any(m.get("unlimited") for m in meters)
    # .strip() before the truthiness test below: a probe that writes "   "
    # would otherwise win the `or` and render a blank tooltip — the exact bug
    # this reason exists to close.
    reason = str(lane.get("reason") or "").strip()
    if status == "ok" and not has_unlimited and binding_pct is not None and binding_pct >= 100.0:
        status = "exhausted"
        # #189 derives this verdict from the meter, so the row arrived with an
        # empty tooltip: EXHAUSTED and nothing to say why or until when. State
        # what was measured — which meter is full and when it reopens — and
        # never paraphrase the provider; if the probe DID carry the upstream's
        # own words, those win, because they are first-hand.
        reason = reason or _exhausted_reason(meters)
    # A drained wallet refuses exactly like a full window: DeepSeek answers 402
    # at zero. It cannot reach the branch above because money has no
    # percentage, so it needs its own — otherwise the lane that actually died
    # is the one row still reading `ok`.
    drained = next(
        (
            m
            for m in meters
            if isinstance(m.get("balance_total"), float) and m["balance_total"] <= 0
        ),
        None,
    )
    if status == "ok" and drained is not None:
        status = "exhausted"
        reason = reason or f"balance {drained['note']} — the lane refuses at zero"
    icon, provider = _PROVIDER.get(kind, ("🤖", kind or "provider"))
    identity = provider
    if kind == "codex" and ":" in lane_id:
        identity = f"{provider} {lane_id.rsplit(':', 1)[1].upper()}"
    elif kind == "copilot" and ":" in lane_id:
        identity = f"{provider} {lane_id.rsplit(':', 1)[1]}"
    return {
        "id": lane_id,
        "kind": kind,
        "provider": provider,
        "identity": identity,
        "icon": icon,
        "family": _FAMILY.get(kind, kind),
        "status": status,
        "plan": plan,
        "billing": _billing(lane, stale=stale),
        "meters": meters,
        "binding_pct": binding_pct,
        "reason": reason,
    }


def _age_s(generated: Any, now: float) -> float | None:
    """Seconds since the report was written; None when unparseable."""
    if not isinstance(generated, str):
        return None
    try:
        written = datetime.strptime(generated, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return now - written.timestamp()


EMPTY: dict[str, Any] = {"lanes": [], "registry": [], "age_s": None, "stale": False}


def _read(now: float) -> dict[str, Any]:
    path = report_path()
    if not path:
        return EMPTY
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # No report (timer not enabled on this host) or a half-written file the
        # writer's atomic mv should have prevented: hide the panel, never raise.
        return EMPTY
    if not isinstance(raw, dict) or not isinstance(raw.get("lanes"), list):
        return EMPTY
    interval = raw.get("intervalSeconds")
    interval = float(interval) if isinstance(interval, (int, float)) else 900.0
    age = _age_s(raw.get("generatedAt"), now)
    stale = age is not None and age > interval * _STALE_INTERVALS
    lanes = [_normalize(lane, stale, now) for lane in raw["lanes"] if isinstance(lane, dict)]
    lanes.sort(key=lambda lane: (lane["family"], lane["id"]))
    registry = []
    for provider_id in raw.get("registryProviders") or []:
        if not isinstance(provider_id, str):
            continue
        icon, provider = _PROVIDER.get(provider_id, ("🤖", provider_id))
        registry.append({"id": provider_id, "icon": icon, "provider": provider})
    return {"lanes": lanes, "registry": registry, "age_s": age, "stale": stale}


def view(now: float) -> dict[str, Any]:
    """Lane view for the dashboard. TTL-cached; empty when no report exists."""
    global _cache
    if _cache is not None and now - _cache[0] < TTL_S:
        return _cache[1]
    snapshot = _read(now)
    _cache = (now, snapshot)
    return snapshot
