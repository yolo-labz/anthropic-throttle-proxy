"""Read additive API funding facts from the existing throttle-lanes report.

No provider calls or credentials live here. Wallets, rate windows and spending
ceilings retain their own units; this metadata never authorizes a paid request.
Timestamps are UTC ISO8601 with an explicit offset. Unknown is not capacity.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) and 0 < len(value) <= 256 else ""


def _alias(value: Any) -> str:
    text = _text(value)
    return text if re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", text) else ""


def normalize(raw: Any, now: float, *, report_stale: bool = False) -> dict[str, Any]:
    """Normalize schema 1 ``apiCredits``; unsupported versions expose no accounts.

    ``authorized`` records the collector's explicit permission fact, independently
    of a positive wallet. A router must still check its actual credentials,
    estimated request cost, model and workspace before dispatching.
    """
    result: dict[str, Any] = {"schema": 1, "accounts": []}
    if not isinstance(raw, dict) or type(raw.get("schema")) is not int or raw["schema"] != 1:
        return result
    accounts = raw.get("accounts")
    if not isinstance(accounts, list):
        return result
    for item in accounts:
        if not isinstance(item, dict) or item.get("provider") not in ("openai", "anthropic"):
            continue
        fields = {
            key: _alias(item.get(key))
            for key in ("id", "provider", "accountId", "workspaceId", "currency", "source")
        }
        if fields["source"] not in {
            "billing-console",
            "admin-api",
            "provider-headers",
            "operator-receipt",
        }:
            fields["source"] = ""
        observed = _timestamp(item.get("observedAt"))
        expires = _timestamp(item.get("validUntil"))
        fresh = (
            not report_stale
            and observed is not None
            and expires is not None
            and observed <= now < expires
            and expires > observed
        )
        valid = all(fields.values()) and len(fields["currency"]) == 3
        credit = item.get("credit")
        credit = credit if isinstance(credit, dict) else {}
        amount = _number(credit.get("remaining"))
        credit_expiry = _timestamp(credit.get("expiresAt"))
        status = credit.get("status")
        status = status if status in ("available", "pending", "unknown", "refused") else "unknown"
        if not valid or (status == "available" and amount is None):
            status = "unknown"
        if credit.get("expiresAt") is not None and credit_expiry is None:
            status = "unknown"
        elif credit_expiry is not None and credit_expiry <= now:
            status = "expired"
        if not fresh:
            status = "stale"
        # Pending and unknown cannot display an amount as available funding.
        amount = amount if status == "available" else None
        limits = []
        for rate in item.get("rateLimits", []) if isinstance(item.get("rateLimits"), list) else []:
            if not isinstance(rate, dict):
                continue
            limits.append(
                {
                    "model": _alias(rate.get("model")),
                    "scope": _alias(rate.get("scope")),
                    "rpmRemaining": _number(rate.get("rpmRemaining")) if fresh else None,
                    "tpmRemaining": _number(rate.get("tpmRemaining")) if fresh else None,
                }
            )
        spend = item.get("spendLimit")
        spend = spend if isinstance(spend, dict) else {}
        result["accounts"].append(
            {
                **fields,
                "observedAt": item.get("observedAt") if observed is not None else None,
                "validUntil": item.get("validUntil") if expires is not None else None,
                "credit": {
                    "status": status,
                    "remaining": amount,
                    "expiresAt": credit.get("expiresAt") if credit_expiry is not None else None,
                },
                "rateLimits": limits,
                "spendLimit": {
                    "remaining": _number(spend.get("remaining")) if fresh else None,
                    "enforced": spend.get("enforced")
                    if isinstance(spend.get("enforced"), bool)
                    else None,
                },
                "authorized": bool(valid and fresh and item.get("authorized") is True),
            }
        )
    return result


def capacity_for(account: dict[str, Any], model: str, scope: str) -> bool:
    """Whether fresh metadata supports a route, not permission to spend money.

    Unknown rate windows fail closed. A configured informational spend ceiling
    is not a hard refusal; only explicitly enforced ceilings bind this verdict.
    """
    credit = account["credit"]
    spend = account["spendLimit"]
    if not account["authorized"] or credit["status"] != "available":
        return False
    if credit["remaining"] is None or credit["remaining"] <= 0:
        return False
    if spend["enforced"] is None or spend["remaining"] is None:
        return False
    if spend["enforced"] and (spend["remaining"] is None or spend["remaining"] <= 0):
        return False
    matches = [r for r in account["rateLimits"] if r["model"] == model and r["scope"] == scope]
    return bool(matches) and all(
        rate[key] is not None and rate[key] > 0
        for rate in matches
        for key in ("rpmRemaining", "tpmRemaining")
    )
