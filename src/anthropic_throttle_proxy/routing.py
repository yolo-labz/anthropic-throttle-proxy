"""Spec 093 routing logic — role inference (S2), lane selection (S3), model-remap (S4).

Kept out of ``ingress.py`` so the forward path stays a thin shape while the
role/gauge/remap policy grows here. Mirrors the repo's per-concern module layout
(``accounts.py``, ``pacing.py``, …).

## S2 scope (today): role inference from the request model

claude-code sends the model in the ``POST /v1/messages`` body; the ingress
infers the request's *role* from the model tier to pick the lane chain later
(S3). No new header (Spec 093 Q2). Roles:

- ``generate``  — premium generate: ``opus`` / ``fable``
- ``judge``     — eval/judge: ``sonnet-5``  (never the small tier)
- ``bulk``      — subagent/fan-out: ``sonnet-4-6`` / ``haiku``-slot

### Known drift (NixOS #1330) — resolved at deployment (S7), NOT here

Canon #647 + Spec 093 say the subagent slot runs ``claude-sonnet-4-6`` (PR #1183)
→ bulk. But Issue #1281 (18/07) re-pinned that slot to ``claude-opus-4-8[1m]``
AND added a CI guard locking it there. So today the fleet sends the SAME model
id for primary-generate and subagent-bulk, and model-tier inference alone maps
both to ``generate``. This function does *correct model-tier inference*; the
bulk-routing intent for opus-slot subagents is a deployment-config question
(either re-pin the fleet's subagent model to a distinct bulk id, or add a role
header — Spec 093 Q2's "optional later" path). S5's never-hard-fail guard keeps
the fleet served regardless; S7 reconciles the model the fleet actually sends.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

__all__ = [
    "ROLES",
    "ROLE_CHAINS",
    "Lane",
    "LaneState",
    "default_lanes",
    "infer_role",
    "infer_role_from_body",
    "bearer_usable",
    "lane_usable",
    "select_lane",
    "remap_body_model",
    "session_key_from_body",
]

ROLES = ("generate", "judge", "bulk")

# Spec 093 S3: per-role ordered lane chain. Walked first→last; the first OPEN
# lane serves the request. GLM is the floor (cheap, flat) so it closes every
# chain; Anthropic is RESERVED for premium generate + judge and NEVER appears in
# the bulk chain (invariant 2: bulk never touches Anthropic).
ROLE_CHAINS: dict[str, tuple[str, ...]] = {
    "generate": ("anthropic", "kimi", "glm"),
    "judge": ("anthropic", "glm", "kimi"),
    "bulk": ("kimi", "glm"),  # Anthropic deliberately absent
}


@dataclass(frozen=True)
class Lane:
    """A per-lane throttle the ingress can route to.

    ``url`` is the lane's base URL (requests forward to ``url + path_qs``);
    ``health_url`` defaults to ``url + /__throttle/health``. ``models`` maps a
    role → the model id the lane's upstream expects (S4 egress remap); an empty
    mapping (Anthropic) means the client's ``claude-*`` id is forwarded verbatim.
    """

    id: str
    url: str
    roles: frozenset[str]
    health_url: str = ""
    models: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.health_url:
            object.__setattr__(self, "health_url", f"{self.url.rstrip('/')}/__throttle/health")


@dataclass
class LaneState:
    """Cached gauge verdict for a lane, updated by the background poll loop."""

    open: bool
    checked_at: float
    detail: str = ""


def default_lanes() -> dict[str, Lane]:
    """The three-lane fleet (Spec 093). URLs env-overridable for test/non-local deploys.

    Egress model ids (S4 remap): Anthropic keeps the client's ``claude-*``; Kimi
    expects Moonshot ids (kimi-k3 for generate — GA 27/07; kimi-k2.6 for bulk/judge,
    verified 23/07); GLM expects glm-5.2 (verified). Overrides via
    ``INGRESS_<LANE>_<ROLE>_MODEL`` so the fleet can pin exact ids without a code change.
    """
    kimi_models = {
        "generate": os.environ.get("INGRESS_KIMI_GENERATE_MODEL", "kimi-k3"),
        "bulk": os.environ.get("INGRESS_KIMI_BULK_MODEL", "kimi-k2.6"),
        "judge": os.environ.get("INGRESS_KIMI_JUDGE_MODEL", "kimi-k2.6"),
    }
    glm_model = os.environ.get("INGRESS_GLM_MODEL", "glm-5.2")
    glm_models = {"generate": glm_model, "bulk": glm_model, "judge": glm_model}
    return {
        "anthropic": Lane(
            "anthropic",
            os.environ.get("INGRESS_ANTHROPIC_LANE_URL", "http://127.0.0.1:8765"),
            frozenset({"generate", "judge"}),
        ),
        "kimi": Lane(
            "kimi",
            os.environ.get("INGRESS_KIMI_LANE_URL", "http://127.0.0.1:8767"),
            frozenset({"generate", "bulk"}),
            models=kimi_models,
        ),
        "glm": Lane(
            "glm",
            os.environ.get("INGRESS_GLM_LANE_URL", "http://127.0.0.1:8766"),
            frozenset({"generate", "judge", "bulk"}),
            models=glm_models,
        ),
    }


def bearer_usable(bearer: dict, now: float | None = None) -> bool:
    """Is one bearer usable right now? Uniform across the three lanes (same proxy schema).

    A bearer is usable unless it carries a hard lock signal:
    - ``limiter.retry_after_until`` in the future (Retry-After active), OR
    - a unified window in ``rejected`` status (budget exhausted).

    Lanes without Anthropic-style unified gauges (Kimi/GLM) simply have no
    ``rejected`` field, so a reachable bearer with no retry-after is usable —
    the lane's own AIMD handles its internal pressure.
    """
    now = time.time() if now is None else now
    limiter = bearer.get("limiter") or {}
    # Defensive parse: a malformed (non-numeric) ``retry_after_until`` must not
    # crash request handling — treat unparseable as "not set" (0 = not paused).
    try:
        retry_after_until = float(limiter.get("retry_after_until", 0) or 0)
    except (TypeError, ValueError):
        retry_after_until = 0.0
    if retry_after_until > now:
        return False
    unified = bearer.get("unified") or {}
    if unified.get("status_5h") == "rejected" or unified.get("status_7d") == "rejected":
        return False
    return True


def lane_usable(health_json: dict, now: float | None = None) -> tuple[bool, str]:
    """Lane-level verdict from a ``/__throttle/health`` body.

    Returns ``(open, detail)``. Open requires the lane can reach its upstream
    AND at least one bearer is usable right now. This is the uniform gauge the
    ingress walks the role chain on (Spec 093 S3, invariant 3: a locked lane
    is skipped → the next request auto-advances).
    """
    if not health_json.get("upstream_egress_ok", False):
        return False, "upstream-egress-down"
    bearers = health_json.get("bearers") or {}
    if not bearers:
        return False, "no-bearers"
    now = time.time() if now is None else now
    for bearer in bearers.values():
        if bearer_usable(bearer, now):
            return True, "ok"
    return False, "no-usable-bearer"


def select_lane(
    role: str,
    state: dict[str, LaneState],
    chains: dict[str, tuple[str, ...]] | None = None,
) -> str | None:
    """Pick the first OPEN lane for the role, walking its chain. None = all capped (HOLD).

    Pure (no I/O) so it's testable directly by populating ``state``. Unknown
    role → generate chain. Bulk never returns ``anthropic`` (structurally — it's
    not in the bulk chain, invariant 2).
    """
    chain = (chains or ROLE_CHAINS).get(role, ROLE_CHAINS["generate"])
    for lane_id in chain:
        st = state.get(lane_id)
        if st is not None and st.open:
            return lane_id
    return None


# Context-size suffix on a model id, e.g. "claude-opus-4-8[1m]". Stripped
# before tier matching so a 1M-context opus is still "generate". No leading/
# trailing ``\s*`` — the surrounding ``.strip()`` handles whitespace, and a
# ``\s*`` anchor on user-controlled input is a ReDoS flag (CodeQL high); the
# bounded suffix alone is linear.
_CTX_SUFFIX = re.compile(r"\[\d+[mk]\]", re.IGNORECASE)


def _normalize(model: str) -> str:
    """Lowercase, strip whitespace, context suffix, and the vendor prefix.

    Strips both ``claude-`` and ``anthropic-`` prefixes (defensive — the fleet
    sends ``claude-*`` today, but the prefix shouldn't drive classification).
    Internal whitespace (``"claude -opus"``) is NOT handled: no real client
    sends a malformed id with internal spaces, and normalizing it would mask a
    genuinely broken caller.
    """
    m = model.lower().strip()
    m = _CTX_SUFFIX.sub("", m).strip()
    for prefix in ("claude-", "anthropic-"):
        if m.startswith(prefix):
            m = m[len(prefix) :]
            break
    return m


def infer_role(model: str | None) -> str:
    """Map a client model id to a routing role (Spec 093 S2 tier table).

    Unknown / empty → ``generate`` (premium-default: S3 then gauge-selects the
    lane and S5 holds rather than silently downgrades). The egress lane models
    (``glm-*``, ``kimi-*``) are never sent by the client — S4 remaps TO them —
    so they aren't classified here; if one ever reaches this function it defaults
    to ``generate`` (conservative).
    """
    if not model:
        return "generate"
    m = _normalize(model)
    if not m:
        return "generate"
    # Premium generate. Fable = current frontier; Opus = fallback/premium.
    if m.startswith(("fable", "opus")):
        return "generate"
    # Judge/eval — Sonnet 5 family. Match "sonnet-5…" / "sonnet 5…" / "sonnet-5".
    if re.match(r"sonnet[- ]?5", m):
        return "judge"
    # Bulk/subagent — Sonnet 4.6 (Spec 093 / PR #1183) + the haiku slot.
    if m.startswith(("sonnet-4", "sonnet 4")) or m.startswith("haiku"):
        return "bulk"
    return "generate"


def infer_role_from_body(raw: bytes) -> str:
    """Infer the role from a ``POST /v1/messages`` request body.

    Returns ``generate`` (default) when the body is absent, not JSON, or has no
    ``model`` field — the ingress never fails a request on inference failure.
    """
    if not raw:
        return "generate"
    # Bounded by the caller (ingress reads at most ROLE_BODY_READ_LIMIT bytes
    # before calling this). Broad except: the contract is "never fail a request
    # on inference" — covers ValueError (bad JSON), TypeError, and RecursionError
    # (a deeply-nested JSON bomb within the bounded prefix).
    try:
        obj = json.loads(raw)
    except Exception:
        return "generate"
    if isinstance(obj, dict):
        return infer_role(obj.get("model"))
    return "generate"


def remap_body_model(raw: bytes, new_model: str) -> bytes:
    """Return ``raw`` with its JSON ``model`` field set to ``new_model`` (S4 egress remap).

    Used when forwarding a ``claude-*`` client request to a non-Anthropic lane
    whose upstream expects its own id (``kimi-k2.6`` / ``glm-5.2``). The
    client-facing response keeps the original id (the lane returns its own; the
    ingress does not rewrite the response model — invariant 4 is about the
    *request* egress, the client sent its id and gets a coherent answer back).

    On any parse failure the body is returned UNCHANGED — remap never breaks a
    forward (a lane that can't parse the body fails its own way, same as today).
    """
    if not raw or not new_model:
        return raw
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(obj, dict):
        return raw
    obj["model"] = new_model
    return json.dumps(obj).encode()


def session_key_from_body(raw: bytes) -> str | None:
    """S4 session stickiness: a stable per-session key from the request body.

    claude-code sends ``metadata.user_id`` (a stable per-user id) — the cleanest
    session boundary for cache economics (a mid-session lane switch forces a slow
    uncached turn). Returns None when absent → no stickiness for that request.
    """
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    meta = obj.get("metadata")
    if isinstance(meta, dict):
        uid = meta.get("user_id")
        if isinstance(uid, str) and uid:
            return uid
    return None
