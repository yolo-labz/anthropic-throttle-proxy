"""Guard tests for spec 333 — cross-gate spillover for the OpenCode FREE gates.

Each test maps to a guard invariant G1–G10 in specs/333-free-gate-spillover/spec.md
and exercises the PURE policy function `FreeGateSpillover.decide()` with
synthetic responses + synthetic headroom — no network, no live traffic.

Run:  pytest specs/333-free-gate-spillover/guard_tests/test_free_gate_spillover.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# The policy module lives BESIDE this test file — this slice forbids src edits,
# so the design artifact is a standalone module the lead would later lift into
# src/anthropic_throttle_proxy/free_gate_policy.py.
sys.path.insert(0, str(Path(__file__).parent))

from free_gate_policy import (  # noqa: E402
    FreeGateSpillover,
    GateHealth,
    SpillDecision,
    make_off_policy,
)

# ── helpers ────────────────────────────────────────────────────────────────

class FakeResponse:
    """Minimal aiohttp.ClientResponse stand-in for decide()."""

    def __init__(self, status: int, headers: dict[str, str] | None = None):
        self.status = status
        self.headers = headers or {}


def _queue_timeout_503() -> FakeResponse:
    """The exact stamp the gate proxy emits on a queue-wait timeout (proxy.py:820)."""
    return FakeResponse(
        503,
        {"x-anthropic-throttle-queue-timeout": "1", "retry-after": "210"},
    )


def _raw_429() -> FakeResponse:
    """Budget-pushback / entitlement refusal — must NEVER spill (R2)."""
    return FakeResponse(429, {"retry-after": "60"})


def _ok() -> FakeResponse:
    return FakeResponse(200)


def _headroom(
    *,
    inflight: int = 0,
    max_concurrent: int = 12,
    retry_after_until: float = 0.0,
    admission_closed: bool = False,
    target_gate: str = "zen",
) -> dict:
    """Synthetic GateHealth for the R3 pre-flight (proxy.py health fields).

    Keyed by target gate id — the policy reads ``headroom[target_gate]``.
    """
    return {
        target_gate: GateHealth(
            inflight=inflight,
            max_concurrent=max_concurrent,
            retry_after_until=retry_after_until,
            admission_closed=admission_closed,
            upstream_egress_ok=True,
        )
    }


def _decide(
    router: FreeGateSpillover,
    *,
    model: str,
    current_gate: str,
    response: FakeResponse,
    headroom: dict,
    now: float | None = None,
    spill_budget_left: int | None = None,
) -> SpillDecision:
    return router.decide(
        request_model=model,
        current_gate=current_gate,
        current_response=response,
        headroom=headroom,
        now=now or time.time(),
        spill_budget_left=spill_budget_left,
    )


# ── G1 — default OFF ───────────────────────────────────────────────────────

def test_g1_default_off_serves_503_verbatim():
    router = make_off_policy()
    d = _decide(
        router,
        model="longcat-2.5-preview-free",
        current_gate="go",
        response=_queue_timeout_503(),
        headroom=_headroom(inflight=0),
    )
    assert d.action == "serve"
    assert d.target_gate is None
    # the caller relays go's 503 verbatim — zen receives ZERO requests.


def test_g1_default_off_never_reaches_zen():
    router = make_off_policy()
    d = _decide(
        router,
        model="beam-free",
        current_gate="zen",
        response=_queue_timeout_503(),
        headroom=_headroom(inflight=0, max_concurrent=1024),
    )
    assert d.action == "serve"
    assert d.target_gate is None


# ── G2 — longcat opt-in spills on a queue-timeout 503 ──────────────────────

class _OnPolicy(FreeGateSpillover):
    """longcat opt-in ON, everything else default OFF."""

    def __init__(self):
        super().__init__(opt_in_families={"longcat"})


def test_g2_longcat_spills_to_zen_on_saturation_503():
    router = _OnPolicy()
    d = _decide(
        router,
        model="longcat-2.5-preview-free",
        current_gate="go",
        response=_queue_timeout_503(),
        headroom=_headroom(inflight=0, max_concurrent=12),
    )
    assert d.action == "spill"
    assert d.target_gate == "zen"


# ── G3 — R2: raw 429 is NEVER spilled ──────────────────────────────────────

def test_g3_raw_429_never_spills():
    router = _OnPolicy()
    d = _decide(
        router,
        model="longcat-2.5-preview-free",
        current_gate="go",
        response=_raw_429(),
        headroom=_headroom(inflight=0),
    )
    assert d.action != "spill"
    # the client gets the 429 / existing in-lane retry; zen untouched


# ── G4 — R3 headroom gate blocks the spill ────────────────────────────────

def test_g4_zen_at_max_concurrent_blocks_spill():
    router = _OnPolicy()
    d = _decide(
        router,
        model="longcat-2.5-preview-free",
        current_gate="go",
        response=_queue_timeout_503(),
        headroom=_headroom(inflight=12, max_concurrent=12),
    )
    assert d.action == "serve"
    assert d.target_gate is None


def test_g4_zen_retry_after_until_in_future_blocks_spill():
    """19/10 incident: zen idle but held — inflight alone is NOT enough (R3.2)."""
    router = _OnPolicy()
    now = time.time()
    d = _decide(
        router,
        model="longcat-2.5-preview-free",
        current_gate="go",
        response=_queue_timeout_503(),
        headroom=_headroom(
            inflight=0, max_concurrent=12, retry_after_until=now + 3600
        ),
        now=now,
    )
    assert d.action == "serve"
    assert d.target_gate is None


def test_g4_zen_admission_closed_blocks_spill():
    router = _OnPolicy()
    d = _decide(
        router,
        model="longcat-2.5-preview-free",
        current_gate="go",
        response=_queue_timeout_503(),
        headroom=_headroom(inflight=0, admission_closed=True),
    )
    assert d.action == "serve"


# ── G5 — no ping-pong ─────────────────────────────────────────────────────

def test_g5_both_gates_saturated_serves_one_503():
    """B answers queue-timeout 503 too -> serve, do NOT bounce back to A."""
    router = _OnPolicy()
    d = _decide(
        router,
        model="longcat-2.5-preview-free",
        current_gate="go",
        response=_queue_timeout_503(),
        headroom=_headroom(inflight=12, max_concurrent=12),  # zen full
    )
    assert d.action == "serve"
    # exactly one spill attempt; the caller relays the 503 with retry-after.


# ── G6 — no double-slot (router holds credit on one gate at a time) ────────

def test_g6_decision_never_targets_the_current_gate():
    """A spill to the gate that just 503'd would double-slot the account."""
    router = _OnPolicy()
    d = _decide(
        router,
        model="longcat-2.5-preview-free",
        current_gate="go",
        response=_queue_timeout_503(),
        headroom=_headroom(inflight=0),
    )
    assert d.action != "spill" or d.target_gate != "go"


# ── G7 — R3.4 spill budget ────────────────────────────────────────────────

def test_g7_spill_budget_exhausted_serves_verbatim():
    router = _OnPolicy()
    d = _decide(
        router,
        model="longcat-2.5-preview-free",
        current_gate="go",
        response=_queue_timeout_503(),
        headroom=_headroom(inflight=0),
        spill_budget_left=0,
    )
    assert d.action == "serve"
    assert d.target_gate is None


def test_g7_spill_budget_available_allows_spill():
    router = _OnPolicy()
    d = _decide(
        router,
        model="longcat-2.5-preview-free",
        current_gate="go",
        response=_queue_timeout_503(),
        headroom=_headroom(inflight=0),
        spill_budget_left=3,
    )
    assert d.action == "spill"
    assert d.target_gate == "zen"


# ── G8 — zen-only families never spill ────────────────────────────────────

@pytest.mark.parametrize(
    "model",
    [
        "mimo-v2.6-flash-free",
        "beam-free",
        "deepseek-v4.1-flash-free",
        "glm-5.3-flash-free",
        "nemotron-4-free",
        "space-bunny-free",
    ],
)
def test_g8_zen_only_families_stay_on_zen(model):
    router = _OnPolicy()
    d = _decide(
        router,
        model=model,
        current_gate="zen",
        response=_queue_timeout_503(),
        headroom=_headroom(inflight=0),  # go has headroom — irrelevant
    )
    assert d.action != "spill"


# ── G9 — non-free traffic never spills ─────────────────────────────────────

@pytest.mark.parametrize(
    "model",
    ["longcat-2.0", "claude-sonnet-4-5", "gpt-6.1-sol", "anthropic/claude-opus-5"],
)
def test_g9_paid_or_unknown_models_are_pinned(model):
    router = _OnPolicy()
    d = _decide(
        router,
        model=model,
        current_gate="zen",
        response=_queue_timeout_503(),
        headroom=_headroom(inflight=0),
    )
    assert d.action != "spill"


# ── G10 — 503 staleness / contract ─────────────────────────────────────────

def test_g10_only_queue_timeout_503_triggers_spill():
    """A 503 WITHOUT the stamp is not a saturation signal — never spill."""
    router = _OnPolicy()
    d = _decide(
        router,
        model="longcat-2.5-preview-free",
        current_gate="go",
        response=FakeResponse(503, {"retry-after": "30"}),  # no stamp
        headroom=_headroom(inflight=0),
    )
    assert d.action != "spill"


def test_g10_200_never_spills():
    router = _OnPolicy()
    d = _decide(
        router,
        model="longcat-2.5-preview-free",
        current_gate="go",
        response=_ok(),
        headroom=_headroom(),
    )
    assert d.action == "serve"  # caller relays the 200; no spill machinery engaged
