"""Cached local-Pi completion accounting. Never add overlapping proxy counters."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .ui import signals

WINDOW_S = 60
MAX_READ_BYTES = 2 * 1024 * 1024
EVENT_STALE_S = 120
CACHE_STALE_S = 15


@dataclass(frozen=True)
class Snapshot:
    gauge: signals.TpsGauge
    sampled_at: float
    reason: str = ""
    output_tokens: int = 0
    providers: tuple[str, ...] = ()
    identity: tuple[int, int] | None = None
    last_event_at: float | None = None


_cache: Snapshot | None = None


def _unknown(now: float, reason: str, *, stale: bool = False, identity=None) -> Snapshot:
    gauge = signals.tps_gauge([])._replace(window_s=WINDOW_S, stale=stale)
    return Snapshot(gauge, now, reason, identity=identity)


def _completion(line: bytes, now: float) -> tuple[float, int, str]:
    """Validate the writer boundary before any count enters the aggregate."""
    row = json.loads(line)
    if not isinstance(row, dict) or not isinstance(row.get("ts"), str):
        raise ValueError("unsupported completion record")
    stamp = datetime.fromisoformat(row["ts"])
    if stamp.tzinfo is None:
        raise ValueError("completion timestamp has no timezone")
    timestamp = stamp.timestamp()
    output, provider = row.get("output"), row.get("provider")
    if type(output) is not int or not 0 <= output <= 2**53:
        raise ValueError("invalid output token count")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("missing completion provider")
    if timestamp > now:
        raise ValueError("future completion timestamp")
    return timestamp, output, provider


def _read_tail(path: Path | None, max_bytes: int) -> tuple[bytes, tuple[int, int]]:
    if path is None:
        default = Path(
            os.environ.get("PI_USAGE_STATE_DIR", Path.home() / ".local/state/pi-harness")
        )
        path = Path(os.environ.get("THROTTLE_PI_USAGE_PATH", default / "usage.jsonl")).expanduser()
    with path.open("rb") as file:
        before = os.fstat(file.fileno())
        file.seek(max(0, before.st_size - max_bytes))
        trimmed = file.tell() > 0
        data = file.read(max_bytes)
        after = os.fstat(file.fileno())
        current = path.stat()

    def signature(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    if signature(before) != signature(after) or signature(before) != signature(current):
        raise ValueError("client journal changed while sampling; retrying")
    if trimmed:
        _, _, data = data.partition(b"\n")
    return data, (before.st_dev, before.st_ino)


def read_usage(
    path: Path | None = None, *, now: float | None = None, max_bytes: int = MAX_READ_BYTES
) -> Snapshot:
    """Replaceable snapshot of exact output in [now-60, now); bounded local I/O."""
    try:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("invalid journal read bound")
        data, identity = _read_tail(path, min(MAX_READ_BYTES, max_bytes))
    except OSError:
        return _unknown(time.time() if now is None else now, "client output journal unavailable")
    except ValueError as exc:
        return _unknown(time.time() if now is None else now, str(exc))
    if now is None:
        now = time.time()
    if not data:
        return _unknown(now, "empty client journal; no window coverage", identity=identity)
    if not data.endswith(b"\n"):
        return _unknown(
            now, "partial client journal write; awaiting complete record", identity=identity
        )
    # ponytail: bounded tail, not a second telemetry pipeline. If the boundary
    # is absent, coverage is unknown; upgrade to a cursor if 2 MiB/min is exceeded.
    start = now - WINDOW_S
    counts = [0] * 6
    providers = set()
    oldest, newest = now, 0.0
    try:
        for line in data.splitlines():
            stamp, output, provider = _completion(line, now)
            oldest, newest = min(oldest, stamp), max(newest, stamp)
            if start <= stamp < now:
                counts[min(5, int((stamp - start) / 10))] += output
                providers.add(provider)
    except (ValueError, TypeError, OverflowError):
        return _unknown(now, "invalid client accounting; coverage unknown", identity=identity)
    if now - newest > EVENT_STALE_S:
        return _unknown(now, "client completion evidence stale", stale=True, identity=identity)
    if oldest > start:
        return _unknown(
            now, "warming up or truncated journal; full 60s window unverified", identity=identity
        )
    gauge = signals.tps_gauge([[count, 0] for count in counts])._replace(
        value=sum(counts) / WINDOW_S, seen=True, sample_age_s=now - newest
    )
    return Snapshot(
        gauge,
        now,
        output_tokens=sum(counts),
        providers=tuple(sorted(providers)),
        identity=identity,
        last_event_at=newest,
    )


async def refresh(*, now: float | None = None) -> None:
    """Only the background collector reads the journal, off the event loop."""
    global _cache
    _cache = await asyncio.to_thread(read_usage, now=now)


def cached(*, now: float | None = None) -> Snapshot:
    """Pure render projection; HTML refresh cannot freshen a sample."""
    if now is None:
        now = time.time()
    if _cache is None:
        return _unknown(now, "awaiting client output accounting")
    if now < _cache.sampled_at or now - _cache.sampled_at > CACHE_STALE_S:
        return replace(
            _cache,
            gauge=_cache.gauge._replace(seen=False, stale=True),
            reason="client accounting cache stale",
        )
    if _cache.last_event_at is not None:
        return replace(_cache, gauge=_cache.gauge._replace(sample_age_s=now - _cache.last_event_at))
    return _cache
