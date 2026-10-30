"""Minute-by-minute persistence of the OAuth unified 5h/7d quota meter.

Measurement only. Nothing here touches a request, a header, a limiter or an
enforcement decision. Once a minute the recorder reads the last
``anthropic-ratelimit-unified-*`` sample each bearer already cached in
``config.bearer_state[bid]["unified"]`` and appends one JSON line per bearer.

The reason it exists: on 24-25/10/2026 the 7d meter read 0.61 and then 0.05
while still pointing at the SAME reset epoch. A fall like that is not
consumption, and it is not a window roll either. The upstream re-based the
meter. A log that only stores raw values makes every later reader re-derive
that, and most will get it wrong and report it as "usage collapsed".

So each window gets an ``event`` computed against the previous fresh reading:

* ``first``         no earlier reading for this bearer
* ``stale``         no new header since the last row (``unified_at`` unchanged);
                    persisted so the minute cadence is visible, never classified
* ``rolled``        the reset epoch changed: a new window, a fall is expected
* ``recalibration`` utilization FELL with the reset epoch UNCHANGED
* ``consumption``   utilization rose
* ``steady``        unchanged within the meter's rounding

A ``recalibration`` row also carries ``delta`` (the size of the fall), so a
re-base can be quantified instead of hidden inside the next minute's numbers.

The previous reading is re-seeded from the file's tail at startup, so a proxy
restart between the two readings does not erase a recalibration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

log = logging.getLogger(__name__)

# The headers publish two decimals, so any change below one step is rounding.
# Same tolerance as ai-stack-solver/model/meter_calibration.py (2 * ROUND).
ROUND = 0.005
TOLERANCE = 2 * ROUND

WINDOWS = ("5h", "7d")

# ponytail: one size cap and one `.1` generation instead of real rotation. At one
# ~300-byte line per bearer per minute the cap is weeks of history; the upgrade
# path is a dated rotation if anyone needs more than two generations.
MAX_BYTES = 32 * 1024 * 1024

# How far back to look for the last row per bearer when re-seeding.
_SEED_TAIL_BYTES = 256 * 1024

BearerSource = Callable[[], Mapping[str, Mapping[str, object]]]


def default_path() -> Path | None:
    """``THROTTLE_METER_LOG`` wins; ``off`` or empty disables the recorder."""
    raw = os.environ.get("THROTTLE_METER_LOG")
    if raw is not None:
        raw = raw.strip()
        return None if raw in ("", "off", "0") else Path(os.path.expanduser(raw))
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "anthropic-throttle-proxy" / "quota-meter.jsonl"


def default_interval() -> float:
    try:
        return max(5.0, float(os.environ.get("THROTTLE_METER_INTERVAL_S", "60")))
    except ValueError:
        return 60.0


def classify(prev: Mapping[str, object] | None, cur: Mapping[str, object], win: str) -> dict:
    """Event for one window, comparing two FRESH readings of the same bearer."""
    util, reset = cur.get(f"util_{win}"), cur.get(f"reset_{win}")
    if not isinstance(util, (int, float)):
        return {"event": "absent"}
    if prev is None:
        return {"event": "first"}
    p_util, p_reset = prev.get(f"util_{win}"), prev.get(f"reset_{win}")
    if not isinstance(p_util, (int, float)):
        return {"event": "first"}
    if reset != p_reset:
        return {"event": "rolled", "prev_reset": p_reset}
    delta = round(util - p_util, 4)
    if delta < -TOLERANCE:
        return {"event": "recalibration", "delta": delta, "prev_util": p_util}
    if delta > TOLERANCE:
        return {"event": "consumption", "delta": delta}
    return {"event": "steady"}


def _snapshot(bid: str, bstate: Mapping[str, object], now: float) -> dict | None:
    """The row for one bearer's cached unified sample, or None if it has never had one."""
    unified = bstate.get("unified")
    if not isinstance(unified, Mapping) or unified.get("util_7d") is None:
        return None
    sampled_at = bstate.get("unified_at")
    age = round(now - sampled_at, 1) if isinstance(sampled_at, (int, float)) else None
    return {
        "epoch": int(now),
        "bearer": bid,
        "sampled_at": sampled_at,
        "sample_age_s": age,
        **{k: unified.get(k) for k in ("util_5h", "reset_5h", "status_5h", "util_7d", "reset_7d")},
        "status_7d": unified.get("status_7d"),
        "status": unified.get("status"),
    }


class MeterRecorder:
    """Owns the per-bearer previous reading and the append-only log."""

    def __init__(self, path: Path, source: BearerSource) -> None:
        self.path = path
        self.source = source
        # bid -> last FRESH row written (the comparison base for classify).
        self.last: dict[str, dict] = {}
        self.recalibrations = 0

    def seed(self) -> None:
        """Re-load the last fresh row per bearer so detection survives a restart."""
        try:
            with self.path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - _SEED_TAIL_BYTES))
                tail = fh.read().decode("utf-8", "replace").splitlines()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("meter recorder: cannot seed from %s: %s", self.path, exc)
            return
        for line in tail:
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a torn first line from the seek, or a crash mid-write
            if isinstance(row, dict) and row.get("bearer") and not row.get("stale"):
                self.last[row["bearer"]] = row

    def rows(self, now: float) -> list[dict]:
        """One row per bearer that has ever cached a unified sample."""
        out = []
        for bid, bstate in sorted(self.source().items()):
            row = _snapshot(bid, bstate, now)
            if row is None:
                continue
            prev = self.last.get(bid)
            if prev is not None and prev.get("sampled_at") == row["sampled_at"]:
                # No new header since the last row: keep the cadence, skip the verdict.
                row["stale"] = True
            else:
                self._judge(bid, prev, row)
                self.last[bid] = row
            out.append(row)
        return out

    def _judge(self, bid: str, prev: Mapping[str, object] | None, row: dict) -> None:
        """Attach the per-window verdict to a fresh row; count and log recalibrations."""
        for win in WINDOWS:
            verdict = classify(prev, row, win)
            row[f"event_{win}"] = verdict.pop("event")
            for key, value in verdict.items():
                row[f"{key}_{win}"] = value
            if row[f"event_{win}"] == "recalibration":
                self.recalibrations += 1
                log.warning(
                    "meter recalibration: bearer=%s window=%s %.2f->%.2f same reset %s",
                    bid,
                    win,
                    prev[f"util_{win}"] if prev else None,
                    row[f"util_{win}"],
                    row[f"reset_{win}"],
                )

    def _size(self) -> int:
        """Current log size; a log that does not exist yet is empty, not an error."""
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def write(self, rows: list[dict]) -> None:
        if not rows:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._size() > MAX_BYTES:
            self.path.replace(self.path.with_name(self.path.name + ".1"))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))

    async def tick(self, now: float | None = None) -> list[dict]:
        rows = self.rows(time.time() if now is None else now)
        # The append is microseconds, but never let a slow disk stall the event loop.
        await asyncio.to_thread(self.write, rows)
        return rows


async def _loop(recorder: MeterRecorder, interval: float) -> None:
    while True:
        try:
            await recorder.tick()
        except Exception:  # measurement must never take the proxy down
            log.exception("meter recorder tick failed")
        await asyncio.sleep(interval)


def meter_recorder_context(source: BearerSource):
    """aiohttp cleanup context: start the minute loop, cancel it on shutdown."""

    async def context(_app):
        path = default_path()
        if path is None:
            yield
            return
        recorder = MeterRecorder(path, source)
        await asyncio.to_thread(recorder.seed)
        task = asyncio.create_task(_loop(recorder, default_interval()))
        try:
            yield
        finally:
            task.cancel()
            # Collect the loop's own CancelledError as a result. If THIS coroutine is cancelled
            # while waiting, gather re-raises it instead of the cleanup swallowing it.
            await asyncio.gather(task, return_exceptions=True)

    return context
