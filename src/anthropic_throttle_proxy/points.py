"""Points: the weekly Claude limit in one unit (100 points = one account-week).

Observe-only first step of the points-per-hour budget (pace-option-draft §4.1).
Pure decision rules; no I/O, no clock, no proxy imports, so every rule is testable
without a server. The proxy debits settled usage here and asks for a would-defer
verdict; this PR never acts on it.

Weights are the burn-watch ceiling-model constants (scripts/token-usage-lake/
science/burnwatch/burn_watch.py ``PRICE``/``W``/``points``), copied verbatim: the
golden table in tests/test_points.py fails loudly when they drift. A refit is one
edit here and one edit there.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MODES = ("off", "observe")  # enforce is a later PR; an unknown value fails closed to off
REGIMES = ("A",)  # even spread over the hours left; B and C are later PRs
WEEK_POINTS = 100.0
DEFAULT_BURST = 1.0  # points: one point is about 1 h of regime A
DEFAULT_FLOOR_PER_H = 0.05  # never-zero trickle, points/h

# List $ per million tokens: (input, output, cache read, cache write).
PRICE = {
    "opus": (4, 20, 0.2, 5),
    "fable": (8, 40, 0.2, 10),
    "sonnet": (2, 10, 0.2, 2.5),
    "haiku": (1, 5, 0.1, 1.25),
}
# Week-share per list dollar.
W = {"cache_read": 4.5e-4, "other": 4.7e-4, "fable": 12.0e-4, "l1": 11.1e-4}
# p90 of settled points per request by family (13/11 20:40-23:15): the reservation size.
P90 = {"fable": 0.17, "opus": 0.008, "sonnet": 0.0033, "haiku": 0.003}


def parse_mode(raw: str | None) -> str:
    value = (raw or "off").strip().lower()
    return value if value in MODES else "off"


def family(model: str | None) -> str | None:
    return next((key for key in PRICE if key in (model or "")), None)


def settled_points(
    model: str | None, usage: dict[str, int], status: int | None = None
) -> float | None:
    """Metered points for one settled request; ``None`` for a model with no weight.

    ``usage`` is the gateway's own vocabulary (input/output/cache_read/cache_creation).
    An aborted summary (>20k fresh input, no cache, HTTP 499) is priced at the L1 weight,
    exactly as burn_watch.classify/points do.
    """
    fam = family(model)
    if fam is None:
        return None
    p = PRICE[fam]
    cache_read = usage.get("cache_read", 0)
    cache_write = usage.get("cache_creation", 0)
    cr = cache_read * p[2] / 1e6
    rest = (usage.get("input", 0) * p[0] + usage.get("output", 0) * p[1] + cache_write * p[3]) / 1e6
    summary = usage.get("input", 0) > 20000 and not cache_read and not cache_write
    if summary and status == 499:
        weight = (cr + rest) * W["l1"]
    elif fam == "fable":
        weight = (cr + rest) * W["fable"]
    else:
        weight = cr * W["cache_read"] + rest * W["other"]
    return weight * 100


def regime_rate(
    util: float | None,
    reset: float | None,
    now: float,
    *,
    regime: str = "A",
    floor_per_h: float = DEFAULT_FLOOR_PER_H,
) -> float | None:
    """Refill rate in points/h: the remaining budget spread evenly over the hours left.

    ``None`` when the meter is unknown or ``reset`` is missing/past: no verdict is
    drawn from points then (fail open). Regime A only.
    """
    if regime != "A":
        raise ValueError(f"unsupported regime {regime!r}")
    if util is None or reset is None or not math.isfinite(util) or not math.isfinite(reset):
        return None
    hours_left = (reset - now) / 3600.0
    if hours_left <= 0:
        return None
    remaining = max(0.0, 1.0 - util) * WEEK_POINTS
    return max(floor_per_h, remaining / hours_left)


@dataclass
class Bucket:
    """Credit in points; a deficit is the memory, refill repays it, ``burst`` caps storage."""

    burst: float = DEFAULT_BURST
    credit: float = field(default=-1.0)
    last_refill: float | None = None
    reserved: float = 0.0

    def __post_init__(self) -> None:
        if self.credit == -1.0:
            self.credit = self.burst  # a new or unreadable bucket starts full

    def refill(self, now: float, rate_per_h: float | None) -> None:
        """Add ``rate × Δt``, clamped to ``burst``; never negative time, never without a rate."""
        if rate_per_h is None:
            return
        if self.last_refill is not None and now > self.last_refill:
            self.credit = min(
                self.burst, self.credit + rate_per_h * (now - self.last_refill) / 3600.0
            )
        self.last_refill = now  # a clock that went backwards just re-anchors

    def debit(self, points: float) -> None:
        self.credit -= max(0.0, points)  # may go below zero: debt is repaid by refill

    def available(self) -> float:
        return self.credit - self.reserved

    def reserve(self, points: float) -> bool:
        """Admit when ``credit − reservations`` is not negative; bounds overshoot by one p90."""
        if self.available() < 0:
            return False
        self.reserved += points
        return True

    def release(self, points: float) -> None:
        left = self.reserved - points
        self.reserved = left if left > 1e-12 else 0.0  # float dust is not a reservation

    def wait_s(self, rate_per_h: float, floor_per_h: float = DEFAULT_FLOOR_PER_H) -> float:
        """Seconds until ``credit − reservations`` is back to zero; 0 when it already is."""
        deficit = -self.available()
        if deficit <= 0:
            return 0.0
        return deficit / max(rate_per_h, floor_per_h) * 3600.0


RECENT_WINDOW_S = 3600.0
_RECENT_CAP = 8192


class Book:
    """Per-account buckets plus the observe-mode counters; process-local, no I/O."""

    def __init__(self, burst: float = DEFAULT_BURST) -> None:
        self.burst = burst
        self.buckets: dict[str, Bucket] = {}
        self.since_reset: dict[str, tuple[float | None, float]] = {}
        self.recent: list[tuple[float, str, str, float]] = []  # (t, model, project, points)
        self.would_defer_total: dict[str, int] = {}
        self.unsettled_total = 0
        self.unpriced_total = 0
        self.settled_total = 0.0
        self._unpriced_seen: set[str] = set()
        self._deferring = False

    def bucket(self, bid: str) -> Bucket:
        found = self.buckets.get(bid)
        if found is None:
            found = self.buckets[bid] = Bucket(self.burst)
        return found

    def debit(
        self,
        bid: str,
        points: float,
        now: float,
        *,
        rate: float | None,
        reset: float | None,
        model: str,
        project: str,
    ) -> float:
        """Debit settled points; returns the account's credit afterwards."""
        bucket = self.bucket(bid)
        bucket.refill(now, rate)
        bucket.debit(points)
        seen_reset, total = self.since_reset.get(bid, (reset, 0.0))
        self.since_reset[bid] = (reset, (0.0 if seen_reset != reset else total) + points)
        self.settled_total += points
        self.recent.append((now, model, project, points))
        self._prune(now)
        return bucket.credit

    def _prune(self, now: float) -> None:
        cutoff = now - RECENT_WINDOW_S
        drop = 0
        while drop < len(self.recent) and self.recent[drop][0] < cutoff:
            drop += 1
        if drop or len(self.recent) > _RECENT_CAP:
            self.recent = self.recent[max(drop, len(self.recent) - _RECENT_CAP) :]

    def settled_since_reset(self, bid: str, reset: float | None) -> float:
        seen_reset, total = self.since_reset.get(bid, (reset, 0.0))
        return total if seen_reset == reset else 0.0

    def note_unpriced(self, model: str) -> bool:
        """Count a model with no weight; True the first time, so the caller logs once."""
        self.unpriced_total += 1
        first = model not in self._unpriced_seen
        self._unpriced_seen.add(model)
        return first

    def admit(
        self, candidates: dict[str, float | None], now: float, reserve_points: float | None
    ) -> tuple[bool | None, str | None, float]:
        """Would a non-priority request be deferred, and the reservation it now holds.

        ``candidates`` maps each eligible account to its refill rate (``None`` = no
        verdict from that account). Returns ``(would_defer, bid, reserved)``: ``None``
        when no account can be judged or the model has no weight (never defer on a
        guess); otherwise whether every judged account is in debt. A request that fits
        is reserved on the account with the most credit.
        """
        judged = {bid: rate for bid, rate in candidates.items() if rate is not None}
        if not judged or reserve_points is None:
            return None, None, 0.0
        for bid, rate in judged.items():
            self.bucket(bid).refill(now, rate)
        best = max(judged, key=lambda bid: self.bucket(bid).available())
        if self.bucket(best).reserve(reserve_points):
            return False, best, reserve_points
        return True, None, 0.0

    def release(self, bid: str | None, points: float) -> None:
        if bid is not None and bid in self.buckets:
            self.buckets[bid].release(points)

    def note_verdict(self, would_defer: bool | None) -> bool:
        """True when the would-defer state flipped (so the caller logs once per transition)."""
        if would_defer is None or would_defer == self._deferring:
            return False
        self._deferring = would_defer
        if would_defer:
            self.would_defer_total["all_in_debt"] = self.would_defer_total.get("all_in_debt", 0) + 1
        return True

    def view(self, rates: dict[str, float | None], now: float) -> dict:
        self._prune(now)
        by_model: dict[str, float] = {}
        by_project: dict[str, float] = {}
        for _t, model, project, points in self.recent:
            by_model[model] = by_model.get(model, 0.0) + points
            by_project[project] = by_project.get(project, 0.0) + points
        return {
            "accounts": {
                bid: {
                    "credit": round(bucket.credit, 6),
                    "reserved": round(bucket.reserved, 6),
                    "rate_per_h": None if rates.get(bid) is None else round(rates[bid], 6),
                    "burst": bucket.burst,
                }
                for bid, bucket in self.buckets.items()
            },
            "regime": "A",
            "would_defer": self._deferring,
            "would_defer_total": dict(self.would_defer_total),
            "unsettled_total": self.unsettled_total,
            "unpriced_total": self.unpriced_total,
            "settled_total": round(self.settled_total, 6),
            "settled_60m_by_model": {k: round(v, 6) for k, v in sorted(by_model.items())},
            "settled_60m_by_project": {k: round(v, 6) for k, v in sorted(by_project.items())},
        }
