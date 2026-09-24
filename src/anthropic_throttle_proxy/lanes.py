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
import math
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
    "mimo": "chinese-frontier",
}

_PROVIDER = {
    "anthropic": ("✳️", "Anthropic"),
    "claude": ("✳️", "Claude"),
    "codex": ("🌀", "Codex"),
    "zai": ("✨", "Z.AI"),
    "deepseek": ("🐋", "DeepSeek"),
    "mimo": ("Ⓜ️", "MiMo"),
    "copilot": ("🐙", "Copilot"),
    "groq": ("🚀", "Groq"),
    "deepinfra": ("🌙", "DeepInfra"),
    "kimi": ("🌙", "Kimi"),
}

# Price suffix per billing cycle. An unknown cycle renders no suffix rather
# than guessing a cadence (python:S3358 keeps the nested ternary out of here).
_CYCLE_SUFFIX = {"monthly": "/mo", "annual": "/yr"}

# The MiMo plan row's lane id: it is both the id of the synthetic row we mint
# when the probe reports nothing, and the filter that keeps a stale copy out.
_MIMO_PLAN_LANE_ID = "mimo:plan"

_cache: tuple[float, dict[str, Any]] | None = None


def report_path() -> str:
    """Path of the lane report. ``THROTTLE_LANES_FILE`` overrides the default."""
    override = os.environ.get("THROTTLE_LANES_FILE", "").strip()
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    return f"{runtime}/throttle-lanes.json" if runtime else ""


def _pct(value: Any) -> float | None:
    """Coerce a percentage to float; anything non-numeric → None (unknown).

    ``math.isfinite`` raises ``OverflowError`` on an integer too large to
    convert to float — and this reads ARBITRARY JSON, so ``intervalSeconds:
    10**400`` in the report file crashed ``_read``, which crashed
    ``_collect_view``, which is to say a malformed number in a file written by
    another process took the dashboard down (cross-family review, 18/09/2026).
    A number the platform cannot represent is not a reading.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
        return float(value)
    except (OverflowError, ValueError):
        return None


def _epoch(value: Any) -> float | None:
    return _pct(value)


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
                "window_mins": _pct(meter.get("windowMins")),
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
    suffix = _CYCLE_SUFFIX.get(str(cycle), "")
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


def _plan_text(lane: dict[str, Any]) -> str:
    """The plan sentence for a lane row: a meter's own ``planType`` wins."""
    for meter in lane.get("meters") or []:
        if isinstance(meter, dict) and meter.get("planType"):
            return str(meter["planType"])
    plan = lane.get("plan")
    return plan if isinstance(plan, str) else ""


def _binding_pct(meters: list[dict[str, Any]]) -> float | None:
    """The fullest READABLE meter — the number that decides this lane's next request."""
    filled = [m["used_pct"] for m in meters if m["used_pct"] is not None]
    return max(filled) if filled else None


def _capacity_verdict(
    status: str, meters: list[dict[str, Any]], binding_pct: float | None, reason: str
) -> tuple[str, str]:
    """Downgrade an ``ok`` verdict the METERS contradict. Returns (status, reason).

    Two independent walls, both invisible to a status that only repeats the
    probe's own words:

    A full meter REFUSES. Measured 07/08/2026: with the shared `codex` meter at
    100%, `codex exec` answers "You've hit your usage limit ... try again at Aug
    8th, 2026 12:48 PM" - yet the row still read `ok`, because the status came
    verbatim from the probe report and never looked at the meters. That is the
    one thing this table must never do: render a lane with no capacity as
    healthy. Guarded on `ok` so a worse verdict (refused/error/stale) still
    wins - a stale 100% is untrusted, not proven exhausted, and could already
    have reset. An `unlimited` meter is live capacity that no percentage can
    express, so a lane holding one is never exhausted: Copilot's premium bucket
    runs to 0 while chat and completions keep serving, and calling that lane
    dead would be the opposite lie.

    A drained wallet refuses exactly like a full window: DeepSeek answers 402 at
    zero. It cannot reach the branch above because money has no percentage, so
    it needs its own - otherwise the lane that actually died is the one row
    still reading `ok`.
    """
    if status != "ok":
        return status, reason
    has_unlimited = any(m.get("unlimited") for m in meters)
    if not has_unlimited and binding_pct is not None and binding_pct >= 100.0:
        # #189 derives this verdict from the meter, so the row arrived with an
        # empty tooltip: EXHAUSTED and nothing to say why or until when. State
        # what was measured - which meter is full and when it reopens - and
        # never paraphrase the provider; if the probe DID carry the upstream's
        # own words, those win, because they are first-hand.
        return "exhausted", (reason or _exhausted_reason(meters))
    drained = next(
        (
            m
            for m in meters
            if isinstance(m.get("balance_total"), float) and m["balance_total"] <= 0
        ),
        None,
    )
    if drained is not None:
        return "exhausted", (reason or f"balance {drained['note']} — the lane refuses at zero")
    return status, reason


def _lane_meters(lane: dict[str, Any], kind: str, now: float) -> list[dict[str, Any]]:
    """This lane's meters, fullest first, each carrying its own reset clock.

    Kind decides only which meters a lane *has*: window meters for the
    window-metered kinds, the copilot pair for copilot. Any lane may carry a
    balance, so that is the fallback and it is checked last — a window-metered
    lane keeps its windows.
    """
    meters = _window_meters(lane) if kind in {"codex", "zai", "mimo"} else []
    if kind == "copilot":
        meters = _copilot_meters(lane)
    if not meters:
        meters = _balance_meters(lane)
    for meter in meters:
        meter["reset_in"] = _reset_in(meter.get("resets_at"), now)
    # Fullest first: the meter that decides whether this lane can take the next
    # request must be the one the eye lands on. Unreadable meters sort last.
    meters.sort(key=lambda m: (m["used_pct"] is None, -(m["used_pct"] or 0.0)))
    return meters


def _lane_identity(kind: str, lane_id: str, provider: str) -> str:
    """Provider label, qualified by the lane's own suffix when it has one.

    codex lanes are labelled by account letter (upper-cased) and copilot lanes
    by their raw suffix; anything else, or no suffix at all, stays the plain
    provider label.
    """
    if ":" not in lane_id:
        return provider
    suffix = lane_id.rsplit(":", 1)[1]
    if kind == "codex":
        return f"{provider} {suffix.upper()}"
    if kind == "copilot":
        return f"{provider} {suffix}"
    return provider


def _normalize(lane: dict[str, Any], stale: bool, now: float) -> dict[str, Any]:
    kind = str(lane.get("kind") or "?")
    lane_id = str(lane.get("id") or "?")
    status = str(lane.get("status") or "unknown")
    if stale and status == "ok":
        status = "stale"
    meters = _lane_meters(lane, kind, now)
    plan = _plan_text(lane)
    binding_pct = _binding_pct(meters)
    # .strip() before the truthiness test below: a probe that writes "   "
    # would otherwise win the `or` and render a blank tooltip - the exact bug
    # this reason exists to close.
    reason = str(lane.get("reason") or "").strip()
    status, reason = _capacity_verdict(status, meters, binding_pct, reason)
    icon, provider = _PROVIDER.get(kind, ("🤖", kind or "provider"))
    return {
        "id": lane_id,
        "kind": kind,
        "provider": provider,
        "identity": _lane_identity(kind, lane_id, provider),
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


EMPTY: dict[str, Any] = {
    "lanes": [],
    "registry": [],
    "age_s": None,
    "stale": False,
    "interval_s": None,
    "observed_at": None,
    "next_sample_in_s": None,
    "error": "report missing, unreadable or malformed",
}


def _sample_clock(raw: dict[str, Any], now: float) -> tuple[float | None, float | None, str]:
    """The report's own cadence and age, plus why either is uncertified.

    Review B4: a MISSING cadence is not a 900-second cadence. Guessing one let
    the page certify staleness and project a next-sample time from an invented
    number; both stay uncertified until the report declares its own interval.

    A negative or non-finite age is the same class of lie in the other
    direction (a clock we cannot reason about), so it degrades to ``None`` with
    a reason attached rather than being rendered as fresh.
    """
    raw_interval = raw.get("intervalSeconds")
    interval = _pct(raw_interval)
    if interval is not None and interval <= 0:
        interval = None
    age = _age_s(raw.get("generatedAt"), now)
    error = ""
    if age is None or age < 0 or not math.isfinite(age):
        age = None
        error = "observation timestamp missing, invalid or in the future"
    if interval is None:
        detail = (
            "sampling interval missing" if raw_interval is None else "sampling interval invalid"
        )
        error = " · ".join(filter(None, [error, detail]))
    return interval, age, error


def _lane_rows(raw: dict[str, Any], *, stale: bool, error: str, now: float) -> list[dict[str, Any]]:
    """Normalize every lane in the report, degrading to ``unknown`` on error.

    UNKNOWN IS NOT HEALTHY: a lane the probe reported as ``ok`` cannot stay
    ``ok`` once the report's own clock is uncertified — it is re-labelled
    ``unknown`` and inherits the reason, so no consumer can read a stale reading
    as a live one.
    """
    lanes = []
    for source in raw["lanes"]:
        if not isinstance(source, dict):
            continue
        lane = _normalize(source, stale or bool(error), now)
        if error and source.get("status") == "ok":
            lane["status"] = "unknown"
            lane["reason"] = " · ".join(filter(None, [lane.get("reason"), error]))
        lanes.append(lane)
    # Family first so the review-family grouping the dashboard relies on is a
    # property of the payload, not of the renderer.
    lanes.sort(key=lambda lane: (lane["family"], lane["id"]))
    return lanes


def _registry_rows(raw: dict[str, Any]) -> list[dict[str, str]]:
    """Provider rows for the fleet strip, straight from the report's id list."""
    providers = raw.get("registryProviders")
    if not isinstance(providers, list):
        return []
    rows = []
    for provider_id in providers:
        if not isinstance(provider_id, str):
            continue
        icon, provider = _PROVIDER.get(provider_id, ("\U0001f916", provider_id))
        rows.append({"id": provider_id, "icon": icon, "provider": provider})
    return rows


def _read(now: float, path: str | None = None) -> dict[str, Any]:
    path = report_path() if path is None else path
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
    interval, age, error = _sample_clock(raw, now)
    stale = age is not None and interval is not None and age > interval * _STALE_INTERVALS
    lanes = _lane_rows(raw, stale=stale, error=error, now=now)
    return {
        "lanes": lanes,
        "registry": _registry_rows(raw),
        "age_s": age,
        "stale": stale,
        "interval_s": interval,
        "observed_at": datetime.fromtimestamp(now - age, UTC).strftime("%d/%m/%Y %H:%M UTC")
        if age is not None
        else None,
        "next_sample_in_s": max(0, interval - age)
        if interval is not None and age is not None
        else None,
        "error": error,
    }


def plan_meter_used_percent(lane_id: str, now: float) -> float | None:
    """Binding used-% of a plan lane's fullest meter, or None without fresh evidence.

    The caller is classifying an upstream response that carries no budget
    headers of its own (a MiMo Token Plan 429) and needs to know whether the
    plan is actually near its allowance. ``None`` is the fail-closed answer and
    it covers every way the evidence can be missing: no lane id, no report, a
    stale report, a lane the probe could not read, or meters without a readable
    percentage.
    """
    if not lane_id:
        return None
    for lane in view(now).get("lanes") or []:
        if not isinstance(lane, dict) or str(lane.get("id") or "") != lane_id:
            continue
        if str(lane.get("status") or "unknown") != "ok":
            return None
        pcts = [
            float(meter["used_pct"])
            for meter in lane.get("meters") or []
            if isinstance(meter, dict) and isinstance(meter.get("used_pct"), int | float)
        ]
        # The fullest meter decides: a plan is spent when its tightest window is.
        return max(pcts) if pcts else None
    return None


def view(now: float) -> dict[str, Any]:
    """Lane view for the dashboard. TTL-cached; empty when no report exists.

    The cache expires on a bounded window, not on ``elapsed < TTL``: with the
    unbounded form a clock that moves BACKWARD (NTP step, a VM restored from
    snapshot, a suspend/resume on a laptop) made ``now - cached`` negative, the
    test passed, and the previously fresh snapshot was served without ever
    re-reading the report — a stale reading that heals itself only once wall
    time catches back up (cross-family review, 18/09/2026). A negative age is
    not freshness; it is a clock we cannot reason about.
    """
    global _cache
    if _cache is not None and 0.0 <= now - _cache[0] < TTL_S:
        return _cache[1]
    snapshot = _read(now)
    # MiMo is sampled by the authenticated browser host, independently of the
    # local lane timer. Reuse the same reader so its own timestamp/cadence,
    # not another provider's successful refresh, decides freshness.
    mimo_path = os.environ.get("THROTTLE_MIMO_REPORT", "").strip()
    if mimo_path:
        mimo = _read(now, mimo_path)
        rows = [
            lane
            for lane in mimo["lanes"]
            if lane["id"] == _MIMO_PLAN_LANE_ID and lane["kind"] == "mimo"
        ]
        if len(rows) != 1:
            rows = [
                _normalize(
                    {
                        "id": _MIMO_PLAN_LANE_ID,
                        "kind": "mimo",
                        "status": "unknown",
                        "reason": "MiMo report missing, malformed or ambiguous",
                    },
                    False,
                    now,
                )
            ]
        snapshot = {
            **snapshot,
            "lanes": [lane for lane in snapshot["lanes"] if lane["id"] != _MIMO_PLAN_LANE_ID]
            + rows,
        }
    _cache = (now, snapshot)
    return snapshot
