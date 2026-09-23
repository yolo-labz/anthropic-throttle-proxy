#!/usr/bin/env python3
"""Read MiMo's console using an existing supervised browser, never model calls.

Run on the browser host with web-automation's virtualenv and PYTHONPATH.
stdout is a credential-free lane report; failures leave the last sample to expire.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit


def report(detail: dict, usage: dict, now: datetime) -> dict:
    """Allowlist measured fields; never forward cookies, API keys or raw responses."""
    if detail.get("code") != 0 or usage.get("code") != 0:
        raise ValueError("console rejected the reading")
    plan = detail["data"]
    items = usage["data"]["monthUsage"]["items"]
    counters = [x for x in items if x.get("name") == "month_total_token"]
    if len(counters) != 1:
        raise ValueError("monthly counter missing or ambiguous")
    used, limit = counters[0]["used"], counters[0]["limit"]
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in (used, limit)):
        raise ValueError("invalid credit counters")
    if used < 0 or limit <= 0:
        raise ValueError("invalid credit range")
    if type(plan.get("expired")) is not bool or not isinstance(plan.get("planName"), str):
        raise ValueError("plan state missing")
    reset = datetime.strptime(plan["currentPeriodEnd"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    if reset <= now or plan["expired"]:
        status = "exhausted"
    else:
        status = "ok"
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": 1,
        "generatedAt": stamp,
        "intervalSeconds": 900,
        "lanes": [
            {
                "id": "mimo:plan",
                "kind": "mimo",
                "status": status,
                "plan": f"{plan['planName']} · {limit / 1e9:g}B credits/month",
                "reason": (
                    "Public-class work only · console sample " + now.strftime("%d/%m/%Y %H:%M UTC")
                ),
                "meters": [
                    {
                        "limitId": "monthly",
                        "usedPercent": used / limit * 100,
                        "allowance": limit,
                        "current": used,
                        "remaining": max(0, limit - used),
                        "resetsAt": reset.timestamp(),
                        # No period-start observation: do not invent burn pace/window length.
                        "windowMins": None,
                    }
                ],
            }
        ],
    }


def main() -> int:
    from lib import interactive

    observed = {}
    routes = {
        "/api/v1/tokenPlan/detail": "detail",
        "/api/v1/tokenPlan/usage": "usage",
        "/api/v1/userProfile": "profile",
    }
    expected = os.environ.get("MIMO_EXPECTED_ACCOUNT_ID", "")
    if not expected:
        raise ValueError("expected account is required")
    with interactive.attach("xiaomi", start_if_down=False, timeout_ms=20000) as (_, _, _, page):

        def capture(response):
            key = routes.get(urlsplit(response.url).path)
            if key and response.status == 200:
                try:
                    observed[key] = response.json()
                except ValueError:
                    # Keep a failure sentinel without exporting an authenticated response body.
                    observed[key] = {"code": "invalid_response"}

        page.on("response", capture)
        page.goto(
            "https://platform.xiaomimimo.com/console/plan-manage",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        deadline = time.monotonic() + 20
        while len(observed) != 3 and time.monotonic() < deadline:
            page.wait_for_timeout(200)
        profile = observed["profile"]
        if profile.get("code") != 0 or str(profile["data"]["userId"]) != expected:
            raise ValueError("browser identity does not match the expected account")
        result = report(observed["detail"], observed["usage"], datetime.now(UTC))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Browser exceptions can contain account URLs; never log their messages.
        print(f"MiMo console reading unavailable ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
