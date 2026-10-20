"""Pure cross-gate spillover policy — spec 333, free gates (go ↔ zen).

This is a STANDALONE, dependency-free module living beside the guard tests.
It is NOT wired into `src/` (this slice forbids src edits); it is the design
artifact the guard tests validate and the lead's integration would lift into
`src/anthropic_throttle_proxy/free_gate_policy.py`.

The function is PURE: same inputs → same decision, no I/O, no clock except
the `now` argument. The server tier (a new :8776 role-aware OpenAI ingress)
wraps it with `_send_upstream`-style forwarding using the same
`x-anthropic-throttle-queue-timeout` stamp vocabulary the gate proxies emit.

Invariants encoded here (all map to spec.md §4 guard invariants G1–G10):
  R1 asymmetry  — only longcat-* has a free spillover pair (go ↔ zen).
  R2 429       — a raw 429 is budget-pushback/entitlement; NEVER spill.
  R3 headroom  — spill only if the target gate has a free slot, no live
                  budget hold (retry_after_until), and is not admission-closed.
  R3.4 budget  — per-family spill budget caps the cross-gate path.
  one-shot     — never ping-pong; never target the gate that just 503'd.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

QUEUE_TIMEOUT_HEADER = "x-anthropic-throttle-queue-timeout"
QUEUE_TIMEOUT_BODY_MARK = "slots saturated"

# Free-catalog families, per spec.md §3.2. `spill_pairs` is the ONLY spill
# symmetry that is provably safe: a family free on BOTH gates. A zen-only free
# family (mimo/beam/deepseek/glm/nemotron/space-bunny) has no go target — the
# go gateway does not carry its catalog, so a spill is a 404 round-trip.
FREE_FAMILIES: dict[str, tuple[str, ...]] = {
    "longcat": ("go", "zen"),  # free on go AND zen (same catalog id)
    "mimo": ("zen",),
    "beam": ("zen",),
    "deepseek": ("zen",),
    "glm": ("zen",),
    "nemotron": ("zen",),
    "space-bunny": ("zen",),
}

FREE_MODEL_PREFIXES: tuple[str, ...] = tuple(
    f"{family}-" for family in FREE_FAMILIES
) + ("space-bunny",)

Lit = Literal["serve", "spill", "hold"]


@dataclass(frozen=True)
class SpillDecision:
    action: Lit
    target_gate: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class GateHealth:
    """The R3 pre-flight view — mirrors the gate's /__throttle/health fields."""

    inflight: int
    max_concurrent: int
    retry_after_until: float  # epoch seconds; 0.0 = no hold
    admission_closed: bool
    upstream_egress_ok: bool


def _family_of(model: str) -> str | None:
    """Family for a FREE catalog model id, or None (R1/G9).

    A paid variant of the same family (``longcat-2.0``) is NOT free — the
    ``-free`` suffix is what models.dev marks as zero-cost, and only the free
    tier may ever move between gates. ``space-bunny-free`` is the one id that
    carries the suffix in the catalog.
    """
    low = model.lower()
    if not low.endswith("-free"):
        return None
    stem = low[: -len("-free")]
    for family in FREE_FAMILIES:
        if stem == family or stem.startswith(f"{family}-"):
            return family
    return None


class FreeGateSpillover:
    """The policy. Construct once per router; `decide()` per request."""

    def __init__(self, opt_in_families: frozenset[str] | set[str] = frozenset()):
        # Default OFF (G1): an empty opt-in set pins every family to its
        # configured gate. Each family is a separate switch.
        self._opt_in = frozenset(opt_in_families)

    def decide(
        self,
        *,
        request_model: str,
        current_gate: str,
        current_response,  # aiohttp.ClientResponse-like (.status, .headers)
        headroom: Mapping[str, GateHealth],  # gate id -> fresh R3 view
        now: float,
        spill_budget_left: int | None = None,
    ) -> SpillDecision:
        # ── G1: default OFF ────────────────────────────────────────────────
        family = _family_of(request_model)
        if family is None or family not in self._opt_in:
            return SpillDecision("serve", None, "no-opt-in")

        # ── R2: a raw 429 is budget-pushback, not capacity (G3) ───────────
        if current_response.status == 429:
            return SpillDecision("serve", None, "429-budget-pushback")

        # ── G10: only the stamped queue-timeout 503 is a saturation signal ─
        stamped = (
            current_response.headers.get(QUEUE_TIMEOUT_HEADER, "").strip() == "1"
        )
        if current_response.status != 503 or not stamped:
            return SpillDecision("serve", None, "not-a-saturation-signal")

        # ── R1: family must have a spillover pair away from current gate ──
        targets = [
            g for g in FREE_FAMILIES[family] if g != current_gate
        ]
        if not targets:
            return SpillDecision("serve", None, "family-not-spilloverable")

        # ── one-shot: never target the gate that just 503'd (G5/G6) ───────
        target = targets[0]

        # ── G7: per-family spill budget ────────────────────────────────────
        if spill_budget_left is not None and spill_budget_left <= 0:
            return SpillDecision("serve", None, "spill-budget-exhausted")

        # ── R3: headroom pre-flight against the TARGET gate (G4) ──────────
        view = headroom.get(target)
        if view is None:
            return SpillDecision("serve", None, "no-headroom-view")
        if not view.upstream_egress_ok:
            return SpillDecision("serve", None, "target-upstream-down")
        if view.inflight >= view.max_concurrent:
            return SpillDecision("serve", None, "target-full")
        if view.retry_after_until > now:
            # The 19/10 zen receipt: idle inflight, live budget hold.
            return SpillDecision("serve", None, "target-on-budget-hold")
        if view.admission_closed:
            return SpillDecision("serve", None, "target-admission-closed")

        return SpillDecision("spill", target, "queue-timeout-headroom-ok")


def make_off_policy() -> FreeGateSpillover:
    """The default: no family opted in — zero behavior change (G1)."""
    return FreeGateSpillover()


def policy_from_env(spill_env: str) -> FreeGateSpillover:
    """Parse a comma-separated opt-in list, e.g. ``free-spill-longcat=true``
    style flags. Unknown tokens are ignored (fail-closed to OFF)."""
    opt_in: set[str] = set()
    for token in spill_env.split(","):
        token = token.strip().lower()
        if not token or "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key.startswith("free-spill-") and value.strip() == "true":
            family = key[len("free-spill-"):]
            if family in FREE_FAMILIES:
                opt_in.add(family)
    return FreeGateSpillover(opt_in)
