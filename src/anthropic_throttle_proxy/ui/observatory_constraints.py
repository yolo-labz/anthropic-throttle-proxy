"""Read-only typed projection of the published api-capacity.v1 snapshot.

Keys are local to a snapshot/receipt. They must not join to usage or account
history. A compatible header family alone does not identify the provider.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ConstraintKind(StrEnum):
    SUBSCRIPTION_WINDOW = "subscription window"
    CONTINUOUS_REFILL = "continuous refill"
    SPENDING_LIMIT = "spending limit"
    CONCURRENCY = "concurrency"
    LOCAL_PACING = "local pacing"


class ConstraintSource(StrEnum):
    API_HEADERS = "API-key rate-limit headers (Anthropic format)"
    API_SPEND_RECEIPT = "API spend-limit receipt"
    UNAVAILABLE = "no admitted source"


class ConstraintState(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ConstraintObservation:
    kind: ConstraintKind
    source: ConstraintSource
    state: ConstraintState
    reason: str
    scope: str | None = None
    provider: str | None = None
    dimension: str | None = None
    unit: str | None = None
    denominator: int | float | None = None
    remaining: int | float | None = None
    used: int | float | None = None
    reset_at: str | None = None
    observed_at: str | None = None
    enforced: bool | None = None


_HEADER_UNITS = {
    "requests": "requests",
    "tokens": "tokens",
    "input_tokens": "input tokens",
    "output_tokens": "output tokens",
}
_NO_SOURCE = {
    ConstraintKind.SUBSCRIPTION_WINDOW: "No admitted subscription window observation",
    ConstraintKind.CONTINUOUS_REFILL: "No API-key rate-limit dimensions observed",
    ConstraintKind.SPENDING_LIMIT: "No admitted API spend-limit receipt account",
    ConstraintKind.CONCURRENCY: "No admitted concurrency observation",
    ConstraintKind.LOCAL_PACING: "No admitted local pacing observation",
}


def _object(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _objects(value: object) -> list[Mapping]:
    return (
        [entry for entry in value if isinstance(entry, Mapping)] if isinstance(value, list) else []
    )


def _clock(value: object) -> tuple[str | None, float | None]:
    if isinstance(value, str) and len(value) <= 64:
        try:
            clock = datetime.fromisoformat(value)
            if clock.utcoffset() is not None:
                return value, clock.timestamp()
        except (ValueError, OverflowError, OSError):
            return None, None  # No usable source clock; the caller marks this unknown.
    return None, None


def _header_state(
    upstream: object, observed: float | None, reset: float | None, now: float, max_age: object
) -> tuple[ConstraintState, str]:
    if observed is None or observed > now:
        return ConstraintState.UNKNOWN, "Observation clock is missing or invalid"
    if reset is not None and reset <= now:
        return ConstraintState.EXPIRED, "This dimension's observed reset has passed"
    if type(max_age) is not int or max_age <= 0:
        return ConstraintState.UNKNOWN, "Maximum observation age is unavailable"
    if now - observed > max_age:
        return ConstraintState.STALE, "Header capture exceeds its maximum observation age"
    if reset is None:
        return ConstraintState.UNKNOWN, "This dimension's reset is unknown"
    if upstream != "current":
        return ConstraintState.UNKNOWN, "This dimension has no qualified current reading"
    return ConstraintState.CURRENT, "API rate limit; refill interval and rate not observed"


def _header_rows(snapshot: Mapping, now: float) -> list[ConstraintObservation]:
    max_age = snapshot.get("ratelimit_max_age_s")
    lanes = [
        (name, _object(lane.get("rate_limit")))
        for lane in _objects(snapshot.get("header_lanes"))
        if isinstance(name := lane.get("lane"), str) and re.fullmatch(r"api:[a-z0-9-]{1,40}", name)
    ]
    counts = Counter(name for name, _ in lanes)
    rows = []
    for name, rate in lanes:
        if counts[name] != 1:
            continue
        observed_at, observed = _clock(rate.get("observed_at"))
        for dimension, entry in _object(rate.get("dimensions")).items():
            dim = _object(entry)
            if dimension not in _HEADER_UNITS or dim.get("status") in (None, "absent"):
                continue
            reset_at, reset = _clock(_object(dim.get("historical")).get("reset_at"))
            state, reason = _header_state(dim.get("status"), observed, reset, now, max_age)
            remaining, denominator = dim.get("current_remaining"), dim.get("ratio_denominator")
            if state == ConstraintState.CURRENT and (type(remaining) is not int or remaining < 0):
                state, reason = (
                    ConstraintState.UNKNOWN,
                    "This dimension's remaining amount is unknown",
                )
            current = state == ConstraintState.CURRENT
            rows.append(
                ConstraintObservation(
                    ConstraintKind.CONTINUOUS_REFILL,
                    ConstraintSource.API_HEADERS,
                    state,
                    reason,
                    scope=f"Proxy snapshot: {name}",
                    dimension=dimension,
                    unit=_HEADER_UNITS[dimension],
                    remaining=remaining if current else None,
                    denominator=denominator
                    if current and type(denominator) is int and 0 < denominator >= remaining
                    else None,
                    reset_at=reset_at,
                    observed_at=observed_at,
                )
            )
    return rows


def _funding_key(account: Mapping) -> tuple[str, str, str] | None:
    provider, account_ref, workspace_ref = (
        account.get(key) for key in ("provider", "account_ref", "workspace_ref")
    )
    if provider not in ("openai", "anthropic"):
        return None
    if not isinstance(account_ref, str) or not re.fullmatch(r"h:[a-f0-9]{8}", account_ref):
        return None
    if not isinstance(workspace_ref, str) or (
        workspace_ref and not re.fullmatch(r"h:[a-f0-9]{8}", workspace_ref)
    ):
        return None
    return provider, account_ref, workspace_ref


def _valid_amount(value: object) -> bool:
    return (type(value) is int and value >= 0) or (
        type(value) is float and math.isfinite(value) and value >= 0
    )


def _spend_rows(snapshot: Mapping, now: float) -> list[ConstraintObservation]:
    accounts = [
        (key, _object(_object(account.get("meters")).get("spend_limit")))
        for account in _objects(snapshot.get("accounts"))
        if (key := _funding_key(account)) is not None
    ]
    counts = Counter(key for key, _ in accounts)
    rows = []
    for key, meter in accounts:
        if counts[key] != 1:
            continue
        provider, account_ref, workspace_ref = key
        observed_at, observed = _clock(meter.get("observed_at"))
        _, valid_until = _clock(meter.get("valid_until"))
        amount, currency, enforced = (meter.get(k) for k in ("remaining", "currency", "enforced"))
        valid = (
            observed is not None
            and valid_until is not None
            and observed <= now
            and observed < valid_until
        )
        fresh = valid and now < valid_until and meter.get("fresh") is True
        known_amount = (
            fresh
            and _valid_amount(amount)
            and isinstance(currency, str)
            and re.fullmatch(r"[A-Z]{3}", currency) is not None
        )
        known_enforced = fresh and type(enforced) is bool
        if known_amount or known_enforced:
            state, reason = ConstraintState.CURRENT, "Spending limit, not credit balance"
        elif valid and now >= valid_until:
            state, reason = ConstraintState.STALE, "Spend-limit receipt validity has ended"
        else:
            state, reason = (
                ConstraintState.UNKNOWN,
                "Spend-limit evidence is unavailable or unqualified",
            )
        scope = " / ".join(part for part in (account_ref, workspace_ref) if part)
        rows.append(
            ConstraintObservation(
                ConstraintKind.SPENDING_LIMIT,
                ConstraintSource.API_SPEND_RECEIPT,
                state,
                reason,
                scope=f"Receipt scope: {scope}",
                provider=provider,
                dimension="spend",
                unit=currency if known_amount else None,
                remaining=amount if known_amount else None,
                enforced=enforced if known_enforced else None,
                observed_at=observed_at,
            )
        )
    return rows


def project_api_constraints(
    snapshot: Mapping | None, *, now: float
) -> tuple[ConstraintObservation, ...]:
    """Project existing observations; no I/O, identity join, ratios, or inferred resets."""
    snap = _object(snapshot)
    rows = []
    if snap.get("schema") == "api-capacity.v1" and math.isfinite(now):
        rows = _header_rows(snap, now) + _spend_rows(snap, now)
    present = {row.kind for row in rows}
    return tuple(rows) + tuple(
        ConstraintObservation(
            kind, ConstraintSource.UNAVAILABLE, ConstraintState.UNAVAILABLE, reason
        )
        for kind, reason in _NO_SOURCE.items()
        if kind not in present
    )
