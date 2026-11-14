#!/usr/bin/env python3
"""Bounded, explicit same-model revalidation of local Claude subscriptions.

This diagnostic ignores the local gateway's cached quota verdict and reports
fresh provider evidence without exposing secrets. The former restart-based
release mode is disabled; diagnostics never change gateway state.
It never reads API keys, changes a selected model, retries, or follows redirects.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import math
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
    if route != "health" or post:
        raise ValueError("mutating local gateway operations are disabled")
    with contextlib.closing(
        http.client.HTTPConnection("127.0.0.1", 8765, timeout=10)
    ) as connection:
        connection.request("POST" if post else "GET", "/__throttle/" + route)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"local gateway operation failed: HTTP {response.status}")
        return json.loads(response.read(65536))


def idle_gateway():
    """Retired: closing shared admission is not an eligible recovery action."""
    raise ValueError("restart-based hold release is disabled; admission must remain open")


def release_verified_holds(results: list[dict]) -> dict:
    """Retired entry point kept to make old callers fail before any side effect."""
    raise ValueError("restart-based hold release is disabled; preserve ongoing requests")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Exact currently selected Claude model")
    parser.add_argument("--account", action="append", required=True, metavar="LABEL:PATH")
    parser.add_argument(
        "--release-verified-holds",
        action="store_true",
        help="Retired; disabled because it interrupted shared admission and restarted the gateway",
    )
    args = parser.parse_args()
    if args.release_verified_holds:
        parser.error(
            "restart-based hold release is disabled; preserve admission and ongoing requests"
        )
    accounts = []
    for account in args.account:
        label, separator, path = account.partition(":")
        if not separator or not label or not path:
            parser.error("--account must be LABEL:PATH")
        accounts.append((label, Path(path)))
    return run_checks(accounts, args.model, release=False)


def run_checks(accounts: list[tuple[str, Path]], model: str, *, release: bool) -> int:
    if release:
        raise ValueError("restart-based hold release is disabled before provider diagnostics")
    okay = True
    for label, path in accounts:
        try:
            result = check(label, path, model)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            result = {"label": label, "message_ok": False, "error_type": type(exc).__name__}
        print(json.dumps(result, allow_nan=False), flush=True)
        okay = okay and result["message_ok"]
    return 0 if okay else 1


if __name__ == "__main__":
    raise SystemExit(main())
