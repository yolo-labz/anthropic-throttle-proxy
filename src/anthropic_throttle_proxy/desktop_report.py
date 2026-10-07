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


def read_credentials(root: Path) -> dict | None:
    """Read encrypted seed without ConfigManager's destructive recovery writes."""
    for name in ("config.json", ".secret_key"):
        info = (root / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
    # Do not import ConfigManager: its load-error/legacy path SAVES defaults
    # over the seed. A telemetry read must never mutate credential storage.
    seed = json.loads((root / "config.json").read_text(encoding="utf-8"))
    accounts = seed.get("mimo_accounts") if isinstance(seed, dict) else None
    # ponytail: one independently observed account; multiple accounts require
    # per-account report identities, never an invented average/summed quota.
    if not isinstance(accounts, list) or len(accounts) != 1 or not isinstance(accounts[0], dict):
        return None
    fields = {
        "mimoPassToken": "mimo_pass_token",
        "mimoUserId": "mimo_user_id",
        "mimoCUserId": "mimo_c_user_id",
    }
    encrypted = {key: accounts[0].get(field) for key, field in fields.items()}
    if any(
        not isinstance(value, str) or not value.startswith("enc:v1:")
        for value in encrypted.values()
    ):
        return None
    # Optional dependencies belong to the installed backend Python, not the
    # proxy package. Reuse its Fernet format and existing SSO implementation.
    box = importlib.import_module("cryptography.fernet").Fernet(
        (root / ".secret_key").read_bytes().strip()
    )
    credentials = {
        key: box.decrypt(value[7:].encode()).decode() for key, value in encrypted.items()
    }
    return credentials if all(credentials.values()) else None


async def collect(root: Path) -> dict:
    """Reuse the installed bridge/SSO rather than another credential transport."""
    credentials = read_credentials(root)
    if credentials is None:
        return {"error": "invalid-seed"}
    sys.path.insert(0, str(root))
    module = importlib.import_module("app.desktop_session")
    if Path(module.__file__) != root / "app" / "desktop_session.py":
        return {"error": "backend-origin"}
    # The backend owns its existing 30s SSO / 15s usage clients. Bound the
    # complete read as well; there is no second HTTP transport in this adapter.
    return await asyncio.wait_for(module.get_account_usage(credentials), timeout=45)


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
