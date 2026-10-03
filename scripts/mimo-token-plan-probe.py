#!/usr/bin/env python3
"""Read MiMo's console using an existing supervised browser, never model calls.

Run on the browser host with web-automation's virtualenv and PYTHONPATH.
stdout is a credential-free lane report; failures leave the last sample to expire.
"""

from __future__ import annotations

import json
import math
import os
import re
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
    # A fully-consumed meter REFUSES regardless of what the calendar says: the
    # observed individual plan reported usedPercent 100.04 with a future reset
    # and the row still read "ok" (receipt 02/10 23:44 BRT). Classify the
    # source truthfully so no consumer has to re-derive it; the team seat row
    # already applies the same used >= total rule.
    if reset <= now or plan["expired"] or used >= limit:
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
# The Team B seat's lane id (second Team seat, per-assigned-seat catalog): its
# own allowance, never summed with the owner seat or the individual plan row.
# Id spelling follows the wiring plan (spec 279 G2): `mimo:team-b`.
_TEAM_B_LANE_ID = "mimo:team-b"


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
    try:
        valid = all(type(x) in (int, float) and math.isfinite(x) for x in (total, used))
    except OverflowError:
        valid = False
    if not valid:
        return _team_fail("Team seat counters invalid")
    # Historical/display counters are not prerequisites for current quota.
    if total <= 0 or used < 0:
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


def team_seat_b_lane(seat_response: object, now: datetime) -> dict:
    """The `mimo:team-b` row — the second Team seat — from one seat reading.

    The B seat is a distinct account's assigned seat: same validation and
    fail-closed rules as `team_seat_lane`, its own allowance, never summed
    with the owner seat or the individual plan. Only the lane id differs, so
    validation lives in exactly one place and the two rows cannot diverge.
    """
    row = team_seat_lane(seat_response, now)
    row["id"] = _TEAM_B_LANE_ID
    return row


# Sentinel: the B seat is not configured at all. Distinct from None, which
# means the B seat WAS read and failed — that still rewrites a fresh
# fail-closed row so a previously healthy B meter is never silently retained.
_UNSET = object()


def team_report(
    detail: dict,
    usage: dict,
    seat_response: object,
    now: datetime,
    seat_b_response: object = _UNSET,
) -> dict:
    """`report(...)` plus one fresh row per configured Team seat, healthy or
    fail-closed (see `team_seat_lane`). `seat_b_response` absent keeps the
    spec-281 two-row shape exactly; anything else adds the B seat's own row.
    """
    result = report(detail, usage, now)
    result["lanes"].append(team_seat_lane(seat_response, now))
    if seat_b_response is not _UNSET:
        result["lanes"].append(team_seat_b_lane(seat_b_response, now))
    return result


# One bounded in-page fetch per seat read; shared by the owner and Team B
# explicit reads (spec 279 G2), so the two cannot drift apart.
_SEAT_FETCH_SCRIPT = """async (path) => {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 15000);
    try {
        const r = await fetch(path, {
            signal: ctl.signal,
            credentials: "include",
        });
        return await r.json();
    } finally { clearTimeout(t); }
}"""


def read_team_b_seat(interactive, project: str, expected: str) -> object:
    """Read only the independently authenticated B profile; never borrow owner identity."""
    with interactive.attach("xiaomi-mimo2", start_if_down=False, timeout_ms=20000) as (
        _,
        _,
        _,
        page,
    ):
        page.goto(
            "https://platform.xiaomimimo.com/console/plan-manage",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        profile = page.evaluate(_SEAT_FETCH_SCRIPT, "/api/v1/userProfile")
        if profile.get("code") != 0 or str(profile["data"]["userId"]) != expected:
            raise ValueError("Team B browser identity does not match the expected account")
        seat = page.evaluate(_SEAT_FETCH_SCRIPT, f"/api/v1/project/{project}/teamTokenPlan/my/seat")
        verified = page.evaluate(_SEAT_FETCH_SCRIPT, "/api/v1/userProfile")
        if verified.get("code") != 0 or str(verified["data"]["userId"]) != expected:
            raise ValueError("Team B browser identity changed during the reading")
        return seat


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
    if team_project and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", team_project):
        raise ValueError("invalid team project identifier")
    # Team B (spec 279 G2) is OFF unless explicitly configured; the same path
    # discipline applies before anything reaches the browser.
    team_b_project = os.environ.get("MIMO_TEAM_B_PROJECT_ID", "").strip()
    if team_b_project and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", team_b_project):
        raise ValueError("invalid team B project identifier")
    # A B seat is a Team seat: reject B-without-Team BEFORE the browser, the
    # same rule the wrapper enforces (spec 279 G1/G2) — never silently drop a
    # configured lane.
    if team_b_project and not team_project:
        raise ValueError("team B requires team configuration")
    if team_project:
        # Passive capture only (never fires on plan-manage navigation — live
        # check 30/09): the seat is read explicitly after identity check.
        routes[f"/api/v1/project/{team_project}/teamTokenPlan/my/seat"] = "seat"
    expected = os.environ.get("MIMO_EXPECTED_ACCOUNT_ID", "")
    if not expected:
        raise ValueError("expected account is required")
    expected_b = os.environ.get("MIMO_TEAM_B_EXPECTED_ACCOUNT_ID", "")
    if team_b_project and (not expected_b or expected_b == expected):
        raise ValueError("team B requires an independent expected account")
    with interactive.attach("xiaomi", start_if_down=False, timeout_ms=20000) as (_, _, _, page):

        def capture(response):
            url = urlsplit(response.url)
            key = routes.get(url.path)
            if (
                key
                and url.scheme == "https"
                and url.netloc == "platform.xiaomimimo.com"
                and response.status == 200
            ):
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
        while not {"profile", "detail", "usage"} <= observed.keys() and time.monotonic() < deadline:
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
                    _SEAT_FETCH_SCRIPT,
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
            # Team B (spec 279 G2): default OFF — the row exists only when the
            # env is configured. A configured-but-failed read still rewrites a
            # fresh fail-closed B row (team-line semantics), and seats are
            # never aggregated.
            seat_b = None
            if team_b_project:
                try:
                    seat_b = read_team_b_seat(interactive, team_b_project, expected_b)
                except Exception:
                    seat_b = None
            if team_b_project:
                result = team_report(
                    observed["detail"], observed["usage"], seat_reading, now, seat_b_response=seat_b
                )
            else:
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
