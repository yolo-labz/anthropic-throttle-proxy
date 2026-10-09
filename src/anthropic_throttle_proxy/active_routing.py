"""Active routing — per-request lane/credential selection by measured state (spec 337).

The throttler today walks a role's static lane chain and takes the first open
lane. This module ranks every surviving candidate by *measured* capacity headroom,
health and cost, and keeps a cooldown table fed by observed 429/5xx so a lane
that just pushed back drops out of rotation for a bounded window.

Design contract (specs/337-active-routing/DESIGN.md):

- Ranking only ever reorders candidates that already passed the existing hard
  filters (``routing.lane_usable`` / ``routing.bearer_usable``); it never makes an
  unusable lane usable.
- Cost only breaks ties between candidates with comparable capacity and health —
  it never overrules a hard signal.
- A request is never parked to wait for a candidate: selection is in-memory and
  immediate, failover moves the SAME request to the next candidate. The existing
  bounded fair queue stays the safety net (``queue_mode`` untouched).
- Everything is behind ``INGRESS_ACTIVE_ROUTING`` (default ``off``): with the
  flag off, callers short-circuit to the legacy first-open selection.

No key material is read or stored here — candidates carry credential *handles*
(bearer ids) only.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

# Outcome classification fed by the ingress after each upstream attempt.
OUTCOME_SUCCESS = "success"
OUTCOME_RETRYABLE = "retryable"  # 429 / saturation-503: spill to the next lane
OUTCOME_FATAL = "fatal"  # 5xx / connection error: also a failover signal

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_COOLDOWN_S = 5.0
DEFAULT_COOLDOWN_MAX_S = 120.0
DEFAULT_COST_CEILING_PER_MTOK = 60.0  # USD per 1M tokens; above this cost scores ~0


@dataclass(frozen=True)
class Candidate:
    """One routable (lane, credential) pair. ``credential_id`` is a handle."""

    lane_id: str
    credential_id: str = ""
    chain_rank: int = 0


@dataclass
class Gauge:
    """Measured state for one candidate, sampled just before ranking."""

    util: float = 0.0  # 1 - headroom; from unified 5h/7d gauges or plan meters
    inflight: int = 0
    error_rate: float = 0.0  # EWMA of observed 429/5xx rate, 0..1
    cost_per_mtok: float = 0.0  # observed USD per 1M tokens for this candidate
    latency_ms: float = 0.0  # last observed hop latency


@dataclass
class RoutingPolicy:
    """Weights and bounds. Immutable in practice; built from env per request."""

    w_capacity: float = 0.5
    w_health: float = 0.3
    w_cost: float = 0.2
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    cooldown_s: float = DEFAULT_COOLDOWN_S
    cooldown_max_s: float = DEFAULT_COOLDOWN_MAX_S
    cost_ceiling_per_mtok: float = DEFAULT_COST_CEILING_PER_MTOK

    def __post_init__(self) -> None:
        total = self.w_capacity + self.w_health + self.w_cost
        if total <= 0:
            raise ValueError("routing policy weights must sum to > 0")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


def active_routing_enabled(env: dict[str, str] | None = None) -> bool:
    """``INGRESS_ACTIVE_ROUTING=on`` opts in; everything else is legacy behavior."""
    source = os.environ if env is None else env
    return source.get("INGRESS_ACTIVE_ROUTING", "off").strip().lower() == "on"


def policy_from_env(env: dict[str, str] | None = None) -> RoutingPolicy:
    """Build a policy from ``INGRESS_ACTIVE_ROUTING_*`` env vars (bounded parses)."""

    def _float(key: str, default: float) -> float:
        try:
            return float((os.environ if env is None else env).get(key, "") or default)
        except ValueError:
            return default

    def _int(key: str, default: int) -> int:
        try:
            return int((os.environ if env is None else env).get(key, "") or default)
        except ValueError:
            return default

    return RoutingPolicy(
        w_capacity=_float("INGRESS_ACTIVE_ROUTING_W_CAPACITY", 0.5),
        w_health=_float("INGRESS_ACTIVE_ROUTING_W_HEALTH", 0.3),
        w_cost=_float("INGRESS_ACTIVE_ROUTING_W_COST", 0.2),
        max_attempts=_int("INGRESS_ACTIVE_ROUTING_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
        cooldown_s=_float("INGRESS_ACTIVE_ROUTING_COOLDOWN_S", DEFAULT_COOLDOWN_S),
        cooldown_max_s=_float("INGRESS_ACTIVE_ROUTING_COOLDOWN_MAX_S", DEFAULT_COOLDOWN_MAX_S),
        cost_ceiling_per_mtok=_float(
            "INGRESS_ACTIVE_ROUTING_COST_CEILING_PER_MTOK", DEFAULT_COST_CEILING_PER_MTOK
        ),
    )


@dataclass
class CooldownTable:
    """Per-candidate failure memory: cooldown windows + error EWMA.

    Deliberately small and in-memory: this is steering state, not a ledger. A
    proxy restart forgetting it degrades to gauge-only ranking, never to a
    refusal.
    """

    ewma_alpha: float = 0.3
    until: dict[tuple[str, str], float] = field(default_factory=dict)
    streak: dict[tuple[str, str], int] = field(default_factory=dict)
    error_rate: dict[tuple[str, str], float] = field(default_factory=dict)

    @staticmethod
    def _key(candidate: Candidate) -> tuple[str, str]:
        return (candidate.lane_id, candidate.credential_id)

    def cooling(self, candidate: Candidate, now: float) -> float:
        """Seconds left in cooldown (0.0 when not cooling)."""
        return max(0.0, self.until.get(self._key(candidate), 0.0) - now)

    def observe(
        self,
        candidate: Candidate,
        outcome: str,
        *,
        now: float,
        retry_after_s: float | None = None,
        policy: RoutingPolicy | None = None,
    ) -> None:
        """Record one attempt outcome and update cooldown/EWMA."""
        policy = policy or RoutingPolicy()
        key = self._key(candidate)
        if outcome == OUTCOME_SUCCESS:
            self.streak[key] = 0
            self.error_rate[key] = (1 - self.ewma_alpha) * self.error_rate.get(
                key, 0.0
            )  # success pulls the rate down
            self.until.pop(key, None)
            return
        self.streak[key] = self.streak.get(key, 0) + 1
        self.error_rate[key] = (1 - self.ewma_alpha) * self.error_rate.get(
            key, 0.0
        ) + self.ewma_alpha
        if retry_after_s is not None and retry_after_s > 0:
            window = min(retry_after_s, policy.cooldown_max_s)
        else:
            window = min(policy.cooldown_s * (2 ** (self.streak[key] - 1)), policy.cooldown_max_s)
        self.until[key] = max(self.until.get(key, 0.0), now + window)


def score(gauge: Gauge, policy: RoutingPolicy, cooldown_s: float = 0.0) -> float:
    """Weighted score in [0, 1]; higher is better.

    ``cooldown_s > 0`` collapses the score so a cooling candidate sorts after
    every non-cooling one without being hidden from the fallback path.
    """
    if cooldown_s > 0:
        return -1.0
    headroom = max(0.0, min(1.0, 1.0 - max(0.0, gauge.util)))
    inflight_penalty = min(0.25, 0.05 * max(0, gauge.inflight))
    capacity = max(0.0, headroom - inflight_penalty)
    health = max(0.0, min(1.0, 1.0 - gauge.error_rate))
    if policy.cost_ceiling_per_mtok > 0:
        cost = max(0.0, 1.0 - (gauge.cost_per_mtok / policy.cost_ceiling_per_mtok))
    else:
        cost = 1.0
    total = policy.w_capacity + policy.w_health + policy.w_cost
    return (policy.w_capacity * capacity + policy.w_health * health + policy.w_cost * cost) / total


def rank(
    candidates: list[Candidate],
    gauges: dict[tuple[str, str], Gauge],
    policy: RoutingPolicy,
    *,
    now: float,
    cooldowns: CooldownTable | None = None,
) -> list[Candidate]:
    """Order candidates best-first. Never filters: cooling candidates sort last.

    Tie-break (after score) is lexicographic on (capacity, health, cost) then the
    static chain rank — so cost only decides between genuinely comparable
    candidates.
    """
    cooldowns = cooldowns or CooldownTable()

    def axes(candidate: Candidate) -> tuple[float, float, float]:
        gauge = gauges.get((candidate.lane_id, candidate.credential_id), Gauge())
        headroom = max(0.0, min(1.0, 1.0 - max(0.0, gauge.util)))
        health = max(0.0, min(1.0, 1.0 - gauge.error_rate))
        cost = 1.0
        if policy.cost_ceiling_per_mtok > 0:
            cost = max(0.0, 1.0 - (gauge.cost_per_mtok / policy.cost_ceiling_per_mtok))
        return (headroom, health, cost)

    def sort_key(candidate: Candidate) -> tuple[float, tuple[float, float, float], int, str, str]:
        gauge = gauges.get((candidate.lane_id, candidate.credential_id), Gauge())
        cooling = cooldowns.cooling(candidate, now)
        return (
            -score(gauge, policy, cooling),
            tuple(-x for x in axes(candidate)),
            candidate.chain_rank,
            candidate.lane_id,
            candidate.credential_id,
        )

    return sorted(candidates, key=sort_key)


def plan(
    candidates: list[Candidate],
    gauges: dict[tuple[str, str], Gauge],
    policy: RoutingPolicy,
    *,
    now: float,
    cooldowns: CooldownTable | None = None,
    already_tried: set[str] | None = None,
) -> list[Candidate]:
    """Ordered attempts for one request, bounded by ``max_attempts``.

    ``already_tried`` lane ids (the spill loop's ``tried`` set) are honored: the
    plan only proposes candidates the loop has not attempted yet.
    """
    tried = already_tried or set()
    ordered = rank(candidates, gauges, policy, now=now, cooldowns=cooldowns)
    fresh = [c for c in ordered if c.lane_id not in tried]
    return fresh[: max(1, policy.max_attempts)]


@dataclass
class SpendRow:
    """One cost-accounting row per completed request (lane/seat/role/model)."""

    ts: float
    lane: str
    seat: str
    role: str
    model: str
    attempts: int
    latency_ms: float
    input_tokens: int
    output_tokens: int
    usd: float


def spend_row(
    *,
    lane: str,
    seat: str,
    role: str,
    model: str,
    attempts: int,
    latency_ms: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    usd: float = 0.0,
    now: float | None = None,
) -> SpendRow:
    return SpendRow(
        ts=time.time() if now is None else now,
        lane=lane,
        seat=seat or "unknown",
        role=role,
        model=model,
        attempts=max(1, int(attempts)),
        latency_ms=float(latency_ms),
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        usd=float(usd),
    )
