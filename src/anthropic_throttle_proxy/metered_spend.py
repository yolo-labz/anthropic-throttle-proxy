"""Durable USD ceiling for the gateway-owned metered Anthropic API key.

09/11/2026: ``overflow`` carried $299.27 of traffic against a $200 credit in
69 minutes. The receipt's ``spendLimit`` binds only when ``enforced`` is true,
the NixOS refresher writes seeded amounts with ``enforced: False``, and every
in-process counter resets on restart. This module keeps the window's measured
spend in a small state file, so a restart cannot reopen a spent window.

Fail closed: no ceiling configured, or a spend file that cannot be read, means
the metered candidate is refused. A corrupt file is never overwritten: an
operator must look at it, because rewriting it would forget the spend.

ponytail: spend is checked BEFORE dispatch and recorded AFTER the response, so
requests already in flight when the ceiling is crossed still complete. The
overshoot is at most API_KEY_MAX_CONCURRENT requests' cost. Upgrade path:
reserve an estimated cost at dispatch and settle it on completion. Writes are an
atomic replace without fsync: a process restart keeps them, a power cut may
lose the last seconds. Spend is bucketed per minute, so the file stays at most
window/60 rows.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading

_BUCKET_S = 60
_MAX_BYTES = 1024 * 1024
_lock = threading.Lock()


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _load(path: str) -> list[list[float]] | None:
    """Rows ``[bucket_start, usd]``; ``[]`` for a missing file; None when unknown."""
    try:
        with open(path, "rb") as handle:
            encoded = handle.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    if len(encoded) > _MAX_BYTES:
        return None
    try:
        raw = json.loads(encoded)
    except ValueError:
        return None
    rows = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return None
    out: list[list[float]] = []
    for row in rows:
        if not (isinstance(row, list) and len(row) == 2 and all(map(_number, row))):
            return None
        if row[1] < 0:
            return None
        out.append([float(row[0]), float(row[1])])
    return out


def window_spend(path: str, window_s: float, now: float) -> float | None:
    """USD recorded in ``(now - window_s, ...]``, or None when the file is unreadable.

    Rows stamped in the future (clock moved back) still count: dropping them
    would turn a clock jump into fresh budget.
    """
    rows = _load(path)
    if rows is None:
        return None
    return sum(usd for at, usd in rows if at + _BUCKET_S > now - window_s)


def record(path: str, usd: float, window_s: float, now: float) -> bool:
    """Add measured spend; False when the file is unknown (left untouched)."""
    if not _number(usd) or usd <= 0:
        return True
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    # The thread lock orders writers in this process; flock on a sibling lock
    # file orders processes (old + new gateway overlap during a restart): a
    # bare read-modify-write lost 273 of 600 recorded dollars to a second
    # writer (p2S adversarial read of #62, D1). The lock file is never
    # replaced, so every writer locks the same inode.
    with _lock, open(f"{path}.lock", "a", encoding="ascii") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # released when the file closes
        rows = _load(path)
        if rows is None:
            return False
        bucket = float(int(now // _BUCKET_S) * _BUCKET_S)
        rows = [row for row in rows if row[0] + _BUCKET_S > now - window_s]
        if rows and rows[-1][0] == bucket:
            rows[-1][1] += usd
        else:
            rows.append([bucket, usd])
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="ascii") as handle:
            json.dump({"schema": 1, "window_s": window_s, "entries": rows}, handle)
        os.replace(tmp, path)
    return True


def refusal(ceiling: float, path: str, window_s: float, now: float) -> str | None:
    """Why the metered candidate must not route right now, or None to allow."""
    if not ceiling > 0:  # 0.0 is config's "no ceiling"; NaN also refuses
        return "no-usd-ceiling"
    spend = window_spend(path, window_s, now)
    if spend is None:
        return "usd-spend-unknown"
    if spend >= ceiling:
        return f"usd-ceiling spend={spend:.2f} ceiling={ceiling:.2f} window_s={window_s:g}"
    return None
