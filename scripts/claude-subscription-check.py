#!/usr/bin/env python3
"""Bounded, explicit same-model revalidation of local Claude subscriptions.

This diagnostic ignores the local gateway's cached quota verdict and reports
fresh provider evidence without exposing secrets. State changes require the
explicit recovery flag and successful checks of every supplied account.
It never reads API keys, changes a selected model, retries, or follows redirects.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import math
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

MARKER = "You are Claude Code, Anthropic's official CLI for Claude."


def check(label: str, path: Path, model: str) -> dict:
    """One small subscription message; only allowlisted evidence leaves here."""
    observed = time.time()
    credential = json.loads(path.expanduser().read_text())
    oauth = credential.get("claudeAiOauth", {}) if isinstance(credential, dict) else {}
    if not isinstance(oauth, dict):
        raise ValueError(f"{label}: invalid subscription credential")
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        raise ValueError(f"{label}: no subscription OAuth access token")
    expiry = oauth.get("expiresAt")
    if type(expiry) not in (int, float) or not math.isfinite(expiry):
        raise ValueError(f"{label}: invalid subscription expiry")
    if expiry <= observed * 1000:
        raise ValueError(f"{label}: subscription token expired; use the token broker")
    payload = {
        "model": model,
        "max_tokens": 8,
        "system": [{"type": "text", "text": MARKER}],
        "messages": [{"role": "user", "content": "Reply OK."}],
    }
    if model.startswith("claude-fable-"):
        # Fable requires adaptive thinking; eight tokens can yield no visible answer.
        payload.update(max_tokens=64, thinking={"type": "adaptive"})
        payload["output_config"] = {"effort": "low"}
    # A fixed HTTPS connection cannot follow redirects or change credential origin.
    with contextlib.closing(
        http.client.HTTPSConnection("api.anthropic.com", timeout=45)
    ) as connection:
        connection.request(
            "POST",
            "/v1/messages?beta=true",
            body=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        status = response.status
        headers = {
            k.lower(): v
            for k, v in response.headers.items()
            if k.lower().startswith("anthropic-ratelimit-unified-") or k.lower() == "retry-after"
        }
        try:
            body = json.loads(response.read(65536))
        except ValueError:
            body = {}
    if not isinstance(body, dict):
        body = {}
    content = body.get("content")
    message_ok = (
        status == 200
        and body.get("type") == "message"
        and body.get("role") == "assistant"
        and body.get("model") == model
        and body.get("stop_reason") in ("end_turn", "max_tokens")
        and isinstance(content, list)
        and any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].strip()
            for block in content
        )
    )
    error = body.get("error", {})
    return {
        "label": label,
        "bearer_id": hashlib.sha256(f"Bearer {token}".encode()).hexdigest()[:8],
        "observed_at": observed,
        "model": model,
        "status": status,
        "message_ok": message_ok,
        "stop_reason": body.get("stop_reason"),
        "usage": body.get("usage"),
        "headers": headers,
        "error_type": error.get("type") if isinstance(error, dict) else None,
        "error_message": (
            str(error.get("message", "")).replace(token, "[redacted]")[:240]
            if isinstance(error, dict)
            else None
        ),
    }


def local_request(route: str, *, post: bool = False) -> dict:
    if route not in ("health", "quiesce", "resume"):
        raise ValueError("unsupported local gateway operation")
    with contextlib.closing(
        http.client.HTTPConnection("127.0.0.1", 8765, timeout=10)
    ) as connection:
        connection.request("POST" if post else "GET", "/__throttle/" + route)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"local gateway operation failed: HTTP {response.status}")
        return json.loads(response.read(65536))


def _require_idle(health: dict) -> None:
    if not isinstance(health, dict) or any(
        type(health.get(key)) is not int or health[key] != 0
        for key in ("inflight", "queued", "admitted_holds")
    ):
        raise ValueError("gateway idle state unknown or has admitted work; let it finish")


@contextlib.contextmanager
def idle_gateway():
    """Prevent a newer client 429 from racing the successful recovery evidence."""
    try:
        local_request("quiesce", post=True)
        health = local_request("health")
        _require_idle(health)
        yield
    finally:
        local_request("resume", post=True)


def release_verified_holds(results: list[dict]) -> dict:
    """Reconcile only successful, fresh subscription checks at an idle boundary."""
    if not results or not all(r.get("message_ok") for r in results):
        raise ValueError("every subscription check must succeed")
    bids = {r["bearer_id"] for r in results}
    keys = bids | {
        r["bearer_id"] + ":model:fable"
        for r in results
        if str(r.get("model", "")).startswith("claude-fable-")
    }
    if any(not 0 <= time.time() - r["observed_at"] <= 120 for r in results):
        raise ValueError("fresh successful message evidence required")
    env = subprocess.check_output(
        [
            "/run/current-system/sw/bin/systemctl",
            "--user",
            "show",
            "throttler-gateway.service",
            "-p",
            "Environment",
            "--value",
        ],
        text=True,
        timeout=10,
    )
    settings = dict(entry.split("=", 1) for entry in shlex.split(env) if "=" in entry)
    raw_path = settings.get("THROTTLE_RETRY_AFTER_STATE_FILE")
    if not raw_path or not Path(raw_path).is_absolute():
        raise ValueError("gateway must declare its persistent Retry-After state path")
    path = Path(raw_path)
    if path.is_symlink():
        raise ValueError("refusing a symlink state file")
    socket_active = (
        subprocess.run(
            [
                "/run/current-system/sw/bin/systemctl",
                "--user",
                "is-active",
                "--quiet",
                "throttler-gateway.socket",
            ],
            check=False,
            timeout=10,
        ).returncode
        == 0
    )
    stopped = False
    try:
        health = local_request("health")
        _require_idle(health)
        if not bids.issubset(health.get("bearers", {})):
            raise ValueError("checked accounts do not match the live gateway bearers")
        stopped = True
        subprocess.run(
            [
                "/run/current-system/sw/bin/systemctl",
                "--user",
                "stop",
                "throttler-gateway.socket",
                "throttler-gateway.service",
            ],
            check=True,
            timeout=30,
        )
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            raise ValueError("invalid Retry-After state")
        before = {key: state.pop(key) for key in keys if key in state}
        backup = path.with_name(path.name + f".before-recheck-{time.time_ns()}")
        with backup.open("x") as handle:
            os.chmod(backup, 0o600)
            json.dump({**state, **before}, handle, allow_nan=False)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {
            "released_bearers": sorted(bids.intersection(before)),
            "released_keys": sorted(before),
            "backup": str(backup),
        }
    finally:
        if stopped:
            subprocess.run(
                [
                    "/run/current-system/sw/bin/systemctl",
                    "--user",
                    "start",
                    "throttler-gateway.service",
                ],
                check=True,
                timeout=30,
            )
            if socket_active:
                subprocess.run(
                    [
                        "/run/current-system/sw/bin/systemctl",
                        "--user",
                        "start",
                        "throttler-gateway.socket",
                    ],
                    check=True,
                    timeout=30,
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Exact currently selected Claude model")
    parser.add_argument("--account", action="append", required=True, metavar="LABEL:PATH")
    parser.add_argument(
        "--release-verified-holds",
        action="store_true",
        help="Release only freshly successful accounts' stale local holds; restarts idle gateway",
    )
    args = parser.parse_args()
    accounts = []
    for account in args.account:
        label, separator, path = account.partition(":")
        if not separator or not label or not path:
            parser.error("--account must be LABEL:PATH")
        accounts.append((label, Path(path)))
    gate = idle_gateway() if args.release_verified_holds else contextlib.nullcontext()
    with gate:
        return run_checks(accounts, args.model, release=args.release_verified_holds)


def run_checks(accounts: list[tuple[str, Path]], model: str, *, release: bool) -> int:
    okay = True
    results = []
    for label, path in accounts:
        try:
            result = check(label, path, model)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            result = {"label": label, "message_ok": False, "error_type": type(exc).__name__}
        print(json.dumps(result, allow_nan=False), flush=True)
        results.append(result)
        okay = okay and result["message_ok"]
    if release:
        if not okay:
            print(json.dumps({"recovery": "refused", "reason": "not every account succeeded"}))
            return 1
        print(json.dumps(release_verified_holds(results)), flush=True)
    return 0 if okay else 1


if __name__ == "__main__":
    raise SystemExit(main())
