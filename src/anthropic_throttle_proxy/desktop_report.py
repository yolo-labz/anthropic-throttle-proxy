"""Out-of-process Desktop weekly quota producer; never imported by the hot path.

Run this file with the installed bridge's Python, --backend and --output.
Only allowlisted non-secret facts leave the bridge; no model call is made.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import math
import os
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

LANE_ID = "mimo:desktop-subscription"
INTERVAL_S = 300


def report(usage: dict, now: datetime) -> dict:
    """Desktop `percent` is REMAINING weekly quota, not purchased credits."""
    row = {
        "id": LANE_ID,
        "kind": "mimo",
        "plan": "Desktop subscription · weekly quota",
        "status": "unknown",
        "reason": "Desktop weekly usage unavailable",
        "meters": [],
    }
    remaining = usage.get("percent")
    valid = (
        "error" not in usage
        and type(remaining) in (int, float)
        and 0 <= remaining <= 100
        and math.isfinite(remaining)
    )
    if valid:
        reset = usage.get("resetAt")
        # No calendar/timezone guessing from resetDate; only measured epoch.
        reset = reset if type(reset) in (int, float) and 0 < reset < 1e12 else None
        row.update(
            status="ok" if remaining > 0 else "exhausted",
            reason="Desktop weekly remaining quota · not Token Plan credits",
            meters=[
                {
                    "limitId": "weekly",
                    "usedPercent": 100.0 - remaining,
                    "remainingPercent": remaining,
                    "unit": "percent",
                    "resetsAt": reset,
                }
            ],
        )
    return {
        "schema": 1,
        "generatedAt": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "intervalSeconds": INTERVAL_S,
        "lanes": [row],
    }


async def collect(root: Path) -> dict:
    """Reuse the installed bridge/SSO rather than another credential transport."""
    for name in ("config.json", ".secret_key"):
        info = (root / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return {"error": "seed-permissions"}
    sys.path.insert(0, str(root))
    # Explicit external backend plugin, not a dependency of the proxy package.
    # Verify module origin rather than accidentally loading a different `app`.
    backend = {}
    for name in ("config", "desktop_session"):
        module = importlib.import_module(f"app.{name}")
        if Path(module.__file__) != root / "app" / f"{name}.py":
            return {"error": "backend-origin"}
        backend[name] = module
    accounts = backend["config"].config_manager.config.mimo_accounts
    # ponytail: one independently observed account; multiple accounts require
    # per-account report identities, never an invented average/summed quota.
    if len(accounts) != 1:
        return {"error": "account-count-not-one"}
    account = accounts[0]
    # The backend owns its existing 30s SSO / 15s usage clients. Bound the
    # complete read as well; there is no second HTTP transport in this adapter.
    return await asyncio.wait_for(
        backend["desktop_session"].get_account_usage(
            {
                "mimoPassToken": account.mimo_pass_token,
                "mimoUserId": account.mimo_user_id,
                "mimoCUserId": account.mimo_c_user_id,
            }
        ),
        timeout=45,
    )


def publish(output: Path, payload: dict) -> None:
    """Atomic replacement, including failed probes; no retained healthy lie."""
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".desktop-quota-", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            os.fchmod(target.fileno(), 0o600)
            json.dump(payload, target, allow_nan=False)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, output)
    finally:
        Path(name).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.backend.resolve(), args.output.resolve()
    # The existing engine's SSO diagnostics may contain credential-bearing URLs.
    # Suppress its streams and never report exception text or raw responses.
    with (
        open(os.devnull, "w") as sink,
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        try:
            # Installed ConfigManager resolves config/key relative to cwd.
            # Keep that private seed lookup inside its own backend directory.
            with contextlib.chdir(root):
                usage = asyncio.run(collect(root))
        except Exception:
            usage = {"error": "collection-failed"}
        payload = report(usage if isinstance(usage, dict) else {}, datetime.now(UTC))
    publish(output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
