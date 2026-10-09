#!/usr/bin/env python3
"""Burst probe — bounded concurrency burst against a throttler lane.

Measures what the Active Routing design (docs/ACTIVE-ROUTING-DESIGN-2026-10-09.md)
promises: p50/p95 latency, relayed-throttle rate, and per-request throttle
markers, so "the fleet no longer stalls" is a number, not a feeling.

Usage (public-class payloads only; the key is read from a 0600 file or helper
and NEVER printed or stored in the output):

    python load/burst_probe.py \
        --url http://127.0.0.1:8766/v1/chat/completions \
        --n 12 --concurrency 4 --key-file - --out baseline.json   # key on stdin

Resolve the key through the fleet's gated helpers, never inline, e.g.
``pi-zai-token | python load/burst_probe.py ... --key-file -``.

Output: one JSON document (stdout or --out) with per-request records (status,
ttfb_s, total_s, retry_after, throttle markers) and aggregate percentiles.
Secrets: the Authorization value is sent but never echoed; 401/403 responses
are recorded by status only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
import uuid

import aiohttp


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, math.ceil((p / 100.0) * len(ordered)) - 1))
    return round(ordered[k], 4)


async def _one(session: aiohttp.ClientSession, url: str, payload: dict, timeout_s: float) -> dict:
    body = json.dumps(payload).encode()
    t0 = time.monotonic()
    ttfb = None
    rec: dict = {"status": 0}
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with session.post(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        ) as resp:
            ttfb = time.monotonic() - t0
            rec["status"] = resp.status
            rec["retry_after"] = resp.headers.get("Retry-After")
            rec["queue_timeout"] = resp.headers.get("x-anthropic-throttle-queue-timeout")
            rec["oauth_entitlement"] = resp.headers.get("x-anthropic-throttle-oauth-entitlement")
            # Drain body (usage blocks matter for cost accounting; keep totals only).
            text = await resp.text()
            rec["usage"] = None
            if text.startswith("{"):
                try:
                    obj = json.loads(text)
                    rec["usage"] = obj.get("usage")
                except ValueError as exc:
                    rec["usage_parse_error"] = type(exc).__name__
    except TimeoutError:
        rec["status"] = -1
        rec["error"] = "timeout"
    except (aiohttp.ClientError, OSError) as exc:  # record, never crash a burst run
        rec["status"] = -2
        rec["error"] = type(exc).__name__
    rec["ttfb_s"] = round(ttfb, 4) if ttfb is not None else None
    rec["total_s"] = round(time.monotonic() - t0, 4)
    return rec


async def burst(
    url: str, n: int, concurrency: int, payload: dict, timeout_s: float, key: str
) -> dict:
    sem = asyncio.Semaphore(concurrency)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    conn = aiohttp.TCPConnector(limit=concurrency)
    started = time.time()
    async with aiohttp.ClientSession(connector=conn, headers=headers) as session:

        async def run(i: int) -> dict:
            async with sem:
                rec = await _one(session, url, payload, timeout_s)
                rec["seq"] = i
                return rec

        records = await asyncio.gather(*(run(i) for i in range(n)))
    wall = round(time.time() - started, 4)
    totals = [r["total_s"] for r in records if r["total_s"] is not None]
    ttfbs = [r["ttfb_s"] for r in records if r["ttfb_s"] is not None]
    by_status: dict[str, int] = {}
    for r in records:
        by_status[str(r["status"])] = by_status.get(str(r["status"]), 0) + 1
    return {
        "url": url,
        "n": n,
        "concurrency": concurrency,
        "wall_s": wall,
        "status_counts": by_status,
        "total_s": {
            "p50": _pct(totals, 50),
            "p95": _pct(totals, 95),
            "max": max(totals) if totals else None,
        },
        "ttfb_s": {
            "p50": _pct(ttfbs, 50),
            "p95": _pct(ttfbs, 95),
            "max": max(ttfbs) if ttfbs else None,
        },
        "records": records,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True)
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout-s", type=float, default=120.0)
    ap.add_argument("--key-file", help="0600 file holding the key; '-' reads stdin")
    ap.add_argument("--prompt", default="Reply with the single word: OK")
    ap.add_argument("--max-tokens", type=int, default=8)
    ap.add_argument("--model", default="")
    ap.add_argument("--out")
    args = ap.parse_args()

    key = ""
    if args.key_file:
        key = (
            sys.stdin.read().strip() if args.key_file == "-" else open(args.key_file).read().strip()
        )
    if not key:
        print(
            "burst_probe: no key resolved (pipe a gated helper into --key-file - "
            "or give a 0600 --key-file; some lanes need none)",
            file=sys.stderr,
        )

    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_tokens,
        "temperature": 0,
        "stream": False,
    }
    if not args.model:
        payload.pop("model")

    result = asyncio.run(burst(args.url, args.n, args.concurrency, payload, args.timeout_s, key))
    result["probe_id"] = uuid.uuid4().hex[:12]
    result["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    text = json.dumps(result, indent=1)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        summary = {
            k: result[k]
            for k in ("url", "n", "concurrency", "wall_s", "status_counts", "total_s", "ttfb_s")
        }
        print(json.dumps(summary))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
