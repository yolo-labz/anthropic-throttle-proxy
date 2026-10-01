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


# The team seat row's lane id: sibling of the individual plan row, never a
# replacement for it and never additive with it (two independent allowances).
_TEAM_LANE_ID = "mimo:team-owner"


def _parse_console_ts(raw: str) -> datetime:
    """Console timestamps arrive with a `T` separator (live check 30/09);
    accept the space spelling too. Everything else raises ValueError."""
    if not isinstance(raw, str):
        raise ValueError("non-string timestamp")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"unparseable console timestamp: {raw!r}")


def _team_fail(reason: str) -> dict:
    """Fresh fail-closed team row: no meters, nothing measured implied.

    Team failures must never raise: a raise skips the report write, which
    silently retains the previously written HEALTHY team meter until the
    report goes stale. Every failure class instead rewrites the row as
    explicitly unusable (spec 281 falsifier).
    """
    return {"id": _TEAM_LANE_ID, "kind": "mimo", "status": "unknown", "reason": reason}


def team_seat_lane(seat_response: object, now: datetime) -> dict:
    """The `mimo:team-owner` row from one team-seat console reading.

    Counters-only truth: `creditsTotal`/`creditsUsed` derive every number; the
    payload's `usedPercent`/`historyUsedPercent` are redundant display values
    and are never read. bool/NaN/negative/non-numeric/ambiguous values are
    rejected (fail closed, no meters). An unassigned seat is not usable
    capacity and its counters are never fabricated into one. Project, user and
    seat ids stay in the payload — the allowlisted output must not carry them.
    A quota row is not proof of account authorization.
    """
    if not isinstance(seat_response, dict) or seat_response.get("code") != 0:
        return _team_fail("Team seat reading unavailable")
    data = seat_response.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("seat"), dict):
        return _team_fail("Team seat reading invalid")
    if type(data.get("expired")) is not bool or not isinstance(data.get("planName"), str):
        return _team_fail("Team seat reading invalid")
    seat = data["seat"]
    status_raw = seat.get("seatStatus")
    if not isinstance(status_raw, str):
        return _team_fail("Team seat reading invalid")
    if status_raw != "ASSIGNED":
        return _team_fail("Team seat unassigned — not usable capacity")
    total = seat.get("creditsTotal")
    used = seat.get("creditsUsed")
    history = seat.get("historyCreditsUsed")
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in (total, used, history)):
        return _team_fail("Team seat counters invalid")
    # Coordinator fix (live protocol check 30/09): a valid ASSIGNED seat may
    # report historyCreditsUsed < creditsUsed — history is an informational
    # prior counter, not an invariant bounding current usage. Rejecting it
    # fail-closed the only real seat this fleet has. Only type/finiteness/
    # non-negativity are provable invariants here.
    if total <= 0 or used < 0 or history < 0:
        return _team_fail("Team seat counters invalid")
    try:
        # Coordinator fix (live protocol check 30/09): the console serialises
        # team period ends with a literal `T` separator. Accept both
        # spellings; anything else fails closed.
        period_end = _parse_console_ts(data["currentPeriodEnd"])
    except (KeyError, TypeError, ValueError):
        return _team_fail("Team seat period unparseable")
    reset_raw = seat.get("nextResetTime")
    if reset_raw is None:
        reset = period_end
    elif isinstance(reset_raw, str):
        try:
            reset = _parse_console_ts(reset_raw)
        except ValueError:
            return _team_fail("Team seat reset unparseable")
    else:
        return _team_fail("Team seat reset ambiguous")
    status = "exhausted" if data["expired"] or period_end <= now or used >= total else "ok"
    return {
        "id": _TEAM_LANE_ID,
        "kind": "mimo",
        "status": status,
        "plan": f"{data['planName']} · {total / 1e9:g}B seat credits",
        "reason": "Team seat · console sample " + now.strftime("%d/%m/%Y %H:%M UTC"),
        "meters": [
            {
                "limitId": "seat",
                "usedPercent": used / total * 100,
                "allowance": total,
                "current": used,
                "remaining": max(0, total - used),
                "resetsAt": reset.timestamp(),
                # Same discipline as the plan row: no period-start observation.
                "windowMins": None,
            }
        ],
    }


def team_report(detail: dict, usage: dict, seat_response: object, now: datetime) -> dict:
    """`report(...)` plus the team row — always one fresh team row when the
    Team lane is configured, healthy or fail-closed (see `team_seat_lane`).
    """
    result = report(detail, usage, now)
    result["lanes"].append(team_seat_lane(seat_response, now))
    return result


def main() -> int:
    from lib import interactive

    observed = {}
    routes = {
        "/api/v1/tokenPlan/detail": "detail",
        "/api/v1/tokenPlan/usage": "usage",
        "/api/v1/userProfile": "profile",
    }
    # Team seat reading is opt-in via an explicit environment project id: no
    # real ids or credentials in source. Unset keeps legacy individual-only
    # behaviour exactly (spec 281).
    team_project = os.environ.get("MIMO_TEAM_PROJECT_ID", "").strip()
    if team_project:
        # Passive capture only (never fires on plan-manage navigation — live
        # check 30/09): the seat is read explicitly after identity check.
        routes[f"/api/v1/project/{team_project}/teamTokenPlan/my/seat"] = "seat"
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
        # Wait for the base plan readings only: the team seat never arrives
        # from this navigation and is fetched explicitly below.
        while len(observed) < 3 and time.monotonic() < deadline:
            page.wait_for_timeout(200)
        profile = observed["profile"]
        if profile.get("code") != 0 or str(profile["data"]["userId"]) != expected:
            raise ValueError("browser identity does not match the expected account")
        # Coordinator fix (live protocol check 30/09): the initial plan-manage
        # navigation does NOT request Team My Seats — waiting for the passive
        # capture always timed out into a fail-closed row. After identity
        # verification, read the seat route explicitly with the page's own
        # session (bounded, same-origin, read-only). A fetch failure degrades
        # to a fail-closed team row, never to a skipped report write.
        seat = None
        if team_project:
            try:
                seat = page.evaluate(
                    """async (path) => {
                        const ctl = new AbortController();
                        const t = setTimeout(() => ctl.abort(), 15000);
                        try {
                            const r = await fetch(path, {
                                signal: ctl.signal,
                                credentials: "include",
                            });
                            return await r.json();
                        } finally { clearTimeout(t); }
                    }""",
                    f"/api/v1/project/{team_project}/teamTokenPlan/my/seat",
                )
            except Exception:
                seat = None
        now = datetime.now(UTC)
        if team_project:
            # A missing team response is itself a team failure: still write one
            # fresh fail-closed team row rather than retaining the old meter.
            # Prefer the explicit read; the passive capture is only a fallback.
            seat_reading = seat if seat is not None else observed.get("seat")
            result = team_report(observed["detail"], observed["usage"], seat_reading, now)
        else:
            result = report(observed["detail"], observed["usage"], now)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Browser exceptions can contain account URLs; never log their messages.
        print(f"MiMo console reading unavailable ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
