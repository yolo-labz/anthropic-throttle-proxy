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
- ``code``      — small agentic code task (#182): tool-use + ``max_tokens`` ≤
  ``CODE_MAX_TOKENS`` + a small body, classified from the BODY not the model
  tier — offloaded to the (idle) Codex lane ahead of any tier-based role

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
from typing import Final

__all__ = [
    "ROLES",
    "ROLE_CHAINS",
    "GENERATE_OVERFLOW_ENABLED",
    "CODE_MAX_TOKENS",
    "CODE_MAX_BODY_BYTES",
    "Lane",
    "LaneState",
    "body_has_tools",
    "default_lanes",
    "effective_chain",
    "infer_role",
    "infer_role_from_body",
    "code_role_rejection_reason",
    "is_small_agentic_code",
    "CODE_REJECT_EMPTY",
    "CODE_REJECT_BODY_TOO_LARGE",
    "CODE_REJECT_NO_TOOLS",
    "CODE_REJECT_UNPARSEABLE",
    "CODE_REJECT_MAX_TOKENS_ABSENT",
    "CODE_REJECT_MAX_TOKENS_INVALID",
    "CODE_REJECT_MAX_TOKENS_TOO_HIGH",
    "bearer_usable",
    "lane_usable",
    "select_lane",
    "remap_body_model",
    "session_key_from_body",
]

ROLES = ("generate", "judge", "bulk", "code")

# Spec 093 S5 invariant 6: don't silently serve a weaker model as Opus. Pre
# kimi-k3 GA (27/07), the Kimi generate model is kimi-k2.6 — a downgrade from
# Opus/Fable. So generate does NOT spill to Kimi/GLM until this flag is on
# (post-GA). Bulk/judge already run on the cheap lanes by design, so the guard
# only applies to generate. Flip via INGRESS_GENERATE_OVERFLOW=true after 27/07
# (once kimi-k3 is live + verified), or to force-overflow in a pinch.
GENERATE_OVERFLOW_ENABLED: bool = os.environ.get(
    "INGRESS_GENERATE_OVERFLOW", "false"
).strip().lower() in {"1", "true", "yes", "on"}

# Spec 093 S3: per-role ordered lane chain. Walked first→last; the first OPEN
# lane serves the request. GLM is the floor (cheap, flat) so it closes every
# chain; Anthropic is RESERVED for premium generate + judge and NEVER appears in
# the bulk chain (invariant 2: bulk never touches Anthropic).
#
# DeepSeek V4-Flash-0731 leads the bulk chain (04/08/2026): the only funded,
# reachable non-Anthropic lane at the time it was added — Kimi/GLM stay in the
# chain (unretired-by-URL, #166) as automatic fallback the day either is
# recharged, but neither answers today. V4-Flash-0731 beats V4-Pro-Preview on
# agent bench (Terminal-Bench 2.1 82.7% vs 72.1%) at $0.14/$0.28 per 1M — the
# cheap-AND-capable pick, not a cost-only tradeoff.
#
# DeepSeek also joins the generate chain (05/08/2026, Pedro-directed): with
# account A org-403'd and account C 7d-rejected, Anthropic generate capacity is
# down to one account, and Kimi/GLM remain unconfigured (empty URL), so
# invariant 6's overflow guard was leaving generate to HOLD for the whole
# fleet with no usable spill target. A live probe (05/08) sent an
# Anthropic-shape multi-turn tool-use request straight to the DeepSeek lane
# and got a clean 200 stream (thinking + tool_use block + stop_reason:
# "tool_use") — unlike Kimi (aborts multi-turn tool-use) or GLM (path/key
# blockers), DeepSeek is verified tools-capable, so the body_has_tools floor
# (agentic requests forced to "generate") now lands on a lane proven to handle
# it. Still gated behind GENERATE_OVERFLOW_ENABLED (effective_chain), so the
# guard is doing its job — this is an explicit opt-in, not a silent downgrade.
# The Codex lane (#182, 05/08/2026, Pedro-directed) is a CLASS-based offload,
# not a saturation spill: Codex's own usage meter sits idle while Claude's 7d
# window is tight, so a "code" request (small agentic turn — see
# ``is_small_agentic_code``) goes there FIRST regardless of whether Anthropic
# is capped. Anthropic/DeepSeek stay as fallback in case the Codex lane itself
# is down. Deliberately NOT gated behind GENERATE_OVERFLOW_ENABLED — that flag
# only guards the "generate" branch of ``effective_chain``, so "code" always
# walks its full chain.
ROLE_CHAINS: dict[str, tuple[str, ...]] = {
    "generate": ("anthropic", "deepseek", "kimi", "glm"),
    "judge": ("anthropic", "glm", "kimi"),
    "bulk": ("deepseek", "kimi", "glm"),  # Anthropic deliberately absent
    "code": ("codex", "anthropic", "deepseek"),
}

# #182: thresholds for the "code" role classifier (see ``is_small_agentic_code``).
# "Small agentic code task" = a short, cheap agentic turn Codex's idle meter can
# absorb — NOT the large tool-schema/long-history turns generate/judge/bulk
# already route correctly. Both env-overridable so the fleet can retune the
# class boundary without a code change.
CODE_MAX_TOKENS: Final[int] = int(os.environ.get("INGRESS_CODE_MAX_TOKENS", "4096"))
CODE_MAX_BODY_BYTES: Final[int] = int(os.environ.get("INGRESS_CODE_MAX_BODY_BYTES", str(32 * 1024)))


@dataclass(frozen=True)
class Lane:
    """A per-lane throttle the ingress can route to.

    ``url`` is the lane's base URL (requests forward to ``url + path_qs``);
    ``health_url`` and ``admission_url`` default to the lane control surface.
    A root health override also derives root admission, keeping protocol-prefixed
    forwarding away from both control endpoints. ``models`` maps a role → the
    model id the lane's upstream expects (S4 egress remap); an empty
    mapping (Anthropic) means the client's ``claude-*`` id is forwarded verbatim.
    """

    id: str
    url: str
    roles: frozenset[str]
    health_url: str = ""
    models: dict[str, str] = field(default_factory=dict)
    # True for a proxy-owns-key lane (Kimi :8767 injects its own credential and
    # tracks zero per-client bearers). Such a lane is usable with an empty
    # ``bearers`` map; a client-provides-key lane (GLM :8766) is NOT — empty
    # bearers means no client has a token routed through it, so traffic would 401.
    proxy_owns_key: bool = False
    # Appended after the historical positional fields; external Lane(...,
    # health_url, models, proxy_owns_key) callers keep their argument binding.
    admission_url: str = ""

    def __post_init__(self) -> None:
        if not self.health_url:
            object.__setattr__(self, "health_url", f"{self.url.rstrip('/')}/__throttle/health")
        if not self.admission_url:
            suffix = "/__throttle/health"
            normalized_health = self.health_url.rstrip("/")
            control_root = (
                normalized_health[: -len(suffix)]
                if normalized_health.endswith(suffix)
                else self.url.rstrip("/")
            )
            object.__setattr__(self, "admission_url", f"{control_root}/__throttle/admission")


@dataclass
class LaneState:
    """Cached gauge verdict for a lane, updated by the background poll loop."""

    open: bool
    checked_at: float
    detail: str = ""
    # ADR-6a (Fleet Foundry K3): the lane's credential class + reason, updated
    # by the poll. ``unknown`` is explicit, never an absent field; the reason
    # names why (e.g. lane-health-404 / fields-absent / no-upstream-allowlist).
    credential_mode: str = "unknown"
    credential_mode_reason: str = ""
    # r1/C1 CAPACITY level: E3 (≥1 fresh usable bearer with 5h/7d windows),
    # evaluated at poll time. The response stamp uses CLASS∧CAPACITY; refusals
    # count CLASS here for ``eligible_configured`` and CLASS∧CAPACITY∧open for
    # ``eligible_open`` so a capped subscription lane reads
    # ``eligible_lanes_exhausted``.
    credential_capacity_ok: bool = False
    # Earliest known future reset (epoch) when every bearer is budget-blocked;
    # used only as an optional refusal hint, never as a routing signal.
    credential_reset_at: float | None = None


def default_lanes() -> dict[str, Lane]:
    """The five-lane fleet (Spec 093 + #169 + #181 + #182). URLs env-overridable
    for test/non-local deploys.

    Egress model ids (S4 remap): Anthropic keeps the client's ``claude-*``; Kimi
    expects Moonshot ids (kimi-k3 for generate — GA 27/07; kimi-k2.6 for bulk/judge,
    verified 23/07); GLM expects glm-5.2 (verified); DeepSeek expects
    deepseek-v4-flash for both bulk and generate (v4-flash beats v4-pro on
    agent bench AND is cheaper — same pick for both roles, verified 04/08 and
    05/08). Overrides via ``INGRESS_<LANE>_<ROLE>_MODEL`` so the fleet can pin
    exact ids without a code change.
    """
    kimi_models = {
        "generate": os.environ.get("INGRESS_KIMI_GENERATE_MODEL", "kimi-k3"),
        "bulk": os.environ.get("INGRESS_KIMI_BULK_MODEL", "kimi-k2.6"),
        "judge": os.environ.get("INGRESS_KIMI_JUDGE_MODEL", "kimi-k2.6"),
    }
    glm_model = os.environ.get("INGRESS_GLM_MODEL", "glm-5.2")
    glm_models = {"generate": glm_model, "bulk": glm_model, "judge": glm_model}
    deepseek_models = {
        "bulk": os.environ.get("INGRESS_DEEPSEEK_BULK_MODEL", "deepseek-v4-flash"),
        "generate": os.environ.get("INGRESS_DEEPSEEK_GENERATE_MODEL", "deepseek-v4-flash"),
    }
    # The codex lane is the ONLY lane whose meter is a subscription window we
    # already pay for and currently under-spend: the Spark meter
    # (``codex_bengalfox``) was measured 15/08 at 22 % used / pace 0.56x on
    # account A and 12 % / 0.43x on B, i.e. ~44 % and ~57 % of two paid weekly
    # windows heading for expiry unused, while the same class of work was
    # being bought from DeepSeek for cash. Pinning an egress id here lets the
    # fleet spend that window instead of discarding it.
    #
    # Default EMPTY = today's behaviour byte-for-byte: no remap, the client's
    # id goes to the CCP sidecar and its own claude-* -> gpt-* alias table
    # decides. Setting it (``gpt-5.3-codex-spark``) pins the egress id.
    #
    # Deliberately scoped to the ``code`` role only. Spark is a small tier that
    # hallucinates on open-ended work, so it may serve ONLY the bounded shape
    # ``is_small_agentic_code`` already gates: tools present, body <=
    # CODE_MAX_BODY_BYTES, explicit positive ``max_tokens`` <= CODE_MAX_TOKENS,
    # failing closed into ``generate`` on anything ambiguous. It must never
    # reach eval/judge/done-state/adversarial review, which is why no
    # ``judge``/``generate`` key is offered here.
    codex_models = {
        role: model
        for role, model in (("code", os.environ.get("INGRESS_CODEX_CODE_MODEL", "")),)
        if model
    }
    lane_urls = {
        "anthropic": os.environ.get("INGRESS_ANTHROPIC_LANE_URL", "http://127.0.0.1:8765"),
        "kimi": os.environ.get("INGRESS_KIMI_LANE_URL", "http://127.0.0.1:8767"),
        "glm": os.environ.get("INGRESS_GLM_LANE_URL", "http://127.0.0.1:8766"),
        "deepseek": os.environ.get("INGRESS_DEEPSEEK_LANE_URL", "http://127.0.0.1:8768"),
        "codex": os.environ.get("INGRESS_CODEX_LANE_URL", "http://127.0.0.1:8769"),
    }

    def control_url(lane: str, kind: str, default: str = "") -> str:
        """Keep protocol-prefixed forwarding separate from lane control paths."""
        return (
            os.environ.get(f"INGRESS_{lane.upper()}_LANE_{kind.upper()}_URL", "").strip() or default
        )

    lanes = {
        "anthropic": Lane(
            "anthropic",
            lane_urls["anthropic"],
            frozenset({"generate", "judge"}),
            health_url=control_url("anthropic", "health"),
            admission_url=control_url("anthropic", "admission"),
        ),
        "kimi": Lane(
            "kimi",
            lane_urls["kimi"],
            frozenset({"generate", "bulk"}),
            health_url=control_url("kimi", "health"),
            admission_url=control_url("kimi", "admission"),
            models=kimi_models,
            proxy_owns_key=True,
        ),
        "glm": Lane(
            "glm",
            lane_urls["glm"],
            frozenset({"generate", "judge", "bulk"}),
            health_url=control_url("glm", "health"),
            admission_url=control_url("glm", "admission"),
            models=glm_models,
        ),
        "deepseek": Lane(
            "deepseek",
            lane_urls["deepseek"],
            frozenset({"bulk", "generate"}),
            health_url=control_url("deepseek", "health"),
            admission_url=control_url("deepseek", "admission"),
            models=deepseek_models,
            proxy_owns_key=True,
        ),
        # #182: the claude-code-proxy sidecar (raine/claude-code-proxy) speaks
        # Anthropic Messages and translates to the OpenAI Responses API itself
        # (claude-* -> gpt-* alias table), so no ``models`` remap on our side
        # — empty mapping (like Anthropic) forwards the client's id verbatim
        # and the sidecar does the rest. proxy_owns_key=True: the sidecar holds
        # its own OAuth/ChatGPT session (device-flow consent, Pedro's step),
        # not a per-client bearer.
        "codex": Lane(
            "codex",
            lane_urls["codex"],
            frozenset({"code"}),
            models=codex_models,
            proxy_owns_key=True,
            # The CCP sidecar does not serve /__throttle/health (health-404
            # finding, 06/08): it answers /healthz -> 200 {"ok": true}. The
            # probe normalizes that shape at the boundary (ingress._poll_one_lane).
            health_url=control_url("codex", "health", "http://127.0.0.1:8769/healthz"),
            admission_url=control_url("codex", "admission"),
        ),
    }
    # An EXPLICITLY empty lane URL retires that lane: it is not built, so it
    # never appears in the health payload, is never probed, and can never be
    # selected. Unset keeps the default (backwards compatible). This is how a
    # cancelled subscription leaves the router — z.ai was cancelled 31/07/2026
    # and Moonshot suspended, and both kept being probed, rendered on the
    # dashboard, and offered as spill targets that answer 401.
    return {
        name: lane
        for name, lane in lanes.items()
        if os.environ.get(f"INGRESS_{name.upper()}_LANE_URL", "unset").strip() != ""
    }


def unified_live_view(unified: dict, now: float | None = None) -> dict:
    """``unified`` with every window whose own reset epoch has already passed dropped.

    A bearer's unified gauges refresh ONLY from its own response headers, so a
    bearer that a gate turns off stops producing the very samples that would
    turn it back on: the snapshot freezes at ``rejected`` / ``util=1.0`` and
    stays frozen for the life of the process, long after the window it
    describes has reset. Measured 31/07/2026 — a bearer sat ``status_5h
    =rejected util_5h=1.0`` for 33 minutes on a snapshot whose ``reset_5h``
    had passed; when finally re-probed its real utilization was ``0.0``. Every
    request it should have served was queued behind a healthy sibling instead.

    A reading past its own reset epoch describes a window that no longer
    exists, so it must not gate anything. ``accounts._window_view`` has
    applied exactly this rule to the DISPLAY layer since the 10/06 outage
    ("the frozen stale reading"); this is the same rule for the paths that
    decide whether traffic may flow.

    Dropping the window does not force traffic at a genuinely-exhausted
    bearer: it only lets the bearer score finite again, and the half-open
    retry probe is what actually re-tests it. If upstream is still refusing,
    the probe's response headers re-populate the window and it re-gates.
    """
    now = time.time() if now is None else now
    # Shallow copy is deliberate and sufficient: ``ratelimit._parse_unified``
    # emits only scalars (str | float | int | None), so there is no nested
    # mutable to alias back into ``config.bearer_state``.
    live = dict(unified)
    # The bare ("") suffix is a real schema member, not a typo — _parse_unified
    # emits an unsuffixed "status"/"reset" pair for the representative window
    # alongside the 5h/7d ones, and _account_routing_candidate_score gates on it.
    for suffix in ("", "_5h", "_7d"):
        reset = live.get(f"reset{suffix}")
        if isinstance(reset, (int, float)) and not isinstance(reset, bool) and float(reset) <= now:
            for prefix in ("status", "util", "reset"):
                live.pop(f"{prefix}{suffix}", None)
    return live


def bearer_usable(bearer: dict, now: float | None = None) -> bool:
    """Is one bearer usable right now? Uniform across the three lanes (same proxy schema).

    A bearer is usable unless it carries a hard lock signal:
    - ``limiter.retry_after_until`` in the future (Retry-After active), OR
    - any aggregate/5h/7d unified status is ``rejected`` (budget exhausted), OR
    - either live utilization is >= 1.0 even if the accompanying status lags.

    Lanes without Anthropic-style unified gauges (Kimi/GLM) simply have no
    ``rejected`` field, so a reachable bearer with no retry-after is usable —
    the lane's own AIMD handles its internal pressure.

    A ``rejected`` window whose reset epoch has passed is a stale reading, not
    a lock — see ``unified_live_view``.
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
    unified = unified_live_view(bearer.get("unified") or {}, now)
    if "rejected" in (unified.get("status"), unified.get("status_5h"), unified.get("status_7d")):
        return False
    return not any(
        isinstance(util, (int, float)) and util >= 1.0
        for util in (unified.get("util_5h"), unified.get("util_7d"))
    )


def lane_usable(
    health_json: dict, now: float | None = None, proxy_owns_key: bool = False
) -> tuple[bool, str]:
    """Lane-level verdict from a ``/__throttle/health`` body.

    Returns ``(open, detail)``. Open requires the lane can reach its upstream
    (``upstream_egress_ok``). A proxy-owns-key lane (Kimi :8767) injects its own
    credential and legitimately tracks zero per-client bearers, so an empty
    ``bearers`` map is OPEN for it (it had falsely closed Kimi → bulk spilled to
    GLM, which needs a client key the tabs don't have). A client-provides-key
    lane (GLM :8766) with no bearers is CLOSED — no client token is routed
    through it, so traffic would 401. A lane that tracks bearers is open iff
    ≥1 bearer is usable (retry-aftered/rejected → skip).
    """
    if not health_json.get("upstream_egress_ok", False):
        return False, "upstream-egress-down"
    # DNS resolving says nothing about whether the lane's own key still works.
    # 04/08/2026: Kimi :8767 reported `upstream_egress_ok` while every request
    # returned "Incorrect API key provided", and the no-bearers-proxy-owns-key
    # rule below then held the lane OPEN — so a spill would 401 the caller.
    if health_json.get("upstream_auth_ok") is False:
        return False, "upstream-auth-rejected"
    bearers = health_json.get("bearers") or {}
    if not bearers:
        return (True, "no-bearers-proxy-owns-key") if proxy_owns_key else (False, "no-bearers")
    now = time.time() if now is None else now
    for bearer in bearers.values():
        if bearer_usable(bearer, now):
            return True, "ok"
    return False, "no-usable-bearer"


def effective_chain(role: str, overflow: bool | None = None) -> tuple[str, ...]:
    """The lane chain actually walked for ``role`` (S5).

    Generate is restricted to Anthropic-only while overflow is disabled
    (pre-kimi-k3 GA) so a capped Anthropic account HOLDs rather than silently
    downgrading to kimi-k2.6/GLM served as Opus (invariant 6). Bulk/judge keep
    their full chains (they're already the cheap lanes).

    ``overflow=None`` reads the LIVE module flag at call time (so a runtime
    toggle of INGRESS_GENERATE_OVERFLOW is honored, not the import-time snapshot).
    """
    if overflow is None:
        overflow = GENERATE_OVERFLOW_ENABLED
    chain = ROLE_CHAINS.get(role, ROLE_CHAINS["generate"])
    if role == "generate" and not overflow:
        return ("anthropic",)
    return chain


def select_lane(
    role: str,
    state: dict[str, LaneState],
    chains: dict[str, tuple[str, ...]] | None = None,
    overflow: bool | None = None,
) -> str | None:
    """Pick the first OPEN lane for the role, walking its effective chain. None = HOLD.

    Pure (no I/O) so it's testable directly by populating ``state``. Unknown
    role → generate chain. Bulk never returns ``anthropic`` (structurally — it's
    not in the bulk chain, invariant 2). Generate with overflow disabled can only
    return ``anthropic`` (invariant 6). ``overflow=None`` reads the live flag.
    """
    if chains is not None:
        chain = chains.get(role, ROLE_CHAINS["generate"])
    else:
        chain = effective_chain(role, overflow)
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


def body_has_tools(raw: bytes) -> bool:
    """True when the request body carries a non-empty ``tools`` array.

    Agentic (tool-use) requests need a tools-capable lane. Anthropic and
    DeepSeek (verified 05/08/2026: a multi-turn tool-use request to the
    DeepSeek lane streamed a clean thinking + tool_use block + stop_reason:
    "tool_use") both handle it reliably; Kimi aborts (stop_reason=null on
    multi-turn) and GLM has path+key blockers, so those two stay excluded even
    when the generate chain spills there. The ingress uses this as a safety
    FLOOR: agentic requests are forced to ``generate`` regardless of model
    tier or header hint, so a consumer can't overflow tool-use to a lane that
    can't handle it — the generate chain (not this function) decides which
    tools-capable lane actually serves it.
    """
    if not raw:
        return False
    try:
        obj = json.loads(raw)
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    tools = obj.get("tools")
    return isinstance(tools, list) and len(tools) > 0


# Why a body did NOT qualify for the "code" role. ``None`` means it did.
# These are the only values ``code_role_rejection_reason`` returns, so they are
# a bounded metric label set (no unbounded cardinality from request content).
CODE_REJECT_EMPTY: Final[str] = "empty_body"
CODE_REJECT_BODY_TOO_LARGE: Final[str] = "body_too_large"
CODE_REJECT_NO_TOOLS: Final[str] = "no_tools"
CODE_REJECT_UNPARSEABLE: Final[str] = "unparseable"
CODE_REJECT_MAX_TOKENS_ABSENT: Final[str] = "max_tokens_absent"
# Present but not a usable integer (``true``, ``"2048"``, a float). Split from
# ABSENT because the operator actions differ: absent means the client never
# declared a bound, invalid means it declared one this proxy cannot read.
CODE_REJECT_MAX_TOKENS_INVALID: Final[str] = "max_tokens_invalid"
CODE_REJECT_MAX_TOKENS_TOO_HIGH: Final[str] = "max_tokens_too_high"


def code_role_rejection_reason(raw: bytes) -> str | None:
    """``None`` when ``raw`` qualifies for the "code" role, else WHY it did not.

    ``ingress_route_decisions_total`` reports only what was *chosen*, so a
    ``code`` row that stays at zero does not say why. Measured 15/08/2026, with
    the Spark pin live and the lane open, the row was zero and diagnosing it
    meant grepping the service journal for ``max_tokens=`` — the answer being
    that 3483 of 3840 requests declared ``max_tokens=64000``, 15.6x the 4096
    ceiling. The shipped design says to revisit the envelope when that counter
    stays flat; this is the evidence that makes the envelope tunable instead of
    guessed.

    Scope, precisely: this names why AGENTIC bodies were *rejected*. It does
    NOT by itself distinguish "no agentic traffic at all" from "every agentic
    body qualified" — both leave it at zero. Pair it with
    ``ingress_route_decisions_total`` (a ``role="code"`` row proves traffic is
    landing) to separate those two.

    Order mirrors ``is_small_agentic_code`` exactly: cheapest checks first, and
    a body failing several gates reports the FIRST one, so the reason is
    deterministic rather than dependent on evaluation order.
    """
    if not raw:
        return CODE_REJECT_EMPTY
    if len(raw) > CODE_MAX_BODY_BYTES:
        return CODE_REJECT_BODY_TOO_LARGE
    if not body_has_tools(raw):
        return CODE_REJECT_NO_TOOLS
    try:
        obj = json.loads(raw)
    except Exception:
        return CODE_REJECT_UNPARSEABLE
    if not isinstance(obj, dict):
        return CODE_REJECT_UNPARSEABLE
    if "max_tokens" not in obj:
        return CODE_REJECT_MAX_TOKENS_ABSENT
    max_tokens = obj.get("max_tokens")
    # ``bool`` is a subclass of ``int``, so ``max_tokens: true`` would otherwise
    # read as 1 and qualify.
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
        return CODE_REJECT_MAX_TOKENS_INVALID
    if not 0 < max_tokens <= CODE_MAX_TOKENS:
        return CODE_REJECT_MAX_TOKENS_TOO_HIGH
    return None


def is_small_agentic_code(raw: bytes) -> bool:
    """True when ``raw`` is a "small agentic code task" (#182 "code" role).

    Defined as ``code_role_rejection_reason(raw) is None`` so the predicate and
    the reported reason can never disagree — a second copy of this ladder would
    drift, and the drift would be invisible (the metric would explain a
    decision the router did not actually make).

    Requires ALL of:
    - ``body_has_tools(raw)`` (agentic — Codex's whole reason to exist here)
    - ``len(raw) <= CODE_MAX_BODY_BYTES`` (small body — large tool-schema/
      long-history turns stay on the existing generate/judge/bulk chains)
    - a present, positive, integer ``max_tokens`` ≤ ``CODE_MAX_TOKENS`` (a
      short turn, not a big generation Codex's meter shouldn't eat)

    Any parse failure, or a body too large to even hold under
    ``CODE_MAX_BODY_BYTES``, returns False — fails INTO the existing
    body_has_tools floor (generate), never claims the "code" role on
    ambiguous input. Callers must pass the FULL buffered body, not a
    truncated prefix (same truncation-safety reasoning as the
    ``body_has_tools`` call site in ``ingress.py``): a prefix cut mid-JSON is
    invalid, and this function already fails closed on that, but a truncated
    ``tools``/``max_tokens`` read could otherwise misclassify a large body as
    small.
    """
    return code_role_rejection_reason(raw) is None


# The role-override header may only DOWNGRADE to a cheaper lane, never CLAIM the
# premium generate lane. Inference already defaults unknown/absent to generate,
# so no legitimate consumer needs to *assert* generate — and honoring a
# client-set ``generate`` would let any local process pin traffic onto the
# reserved Anthropic lane (GLM finding A). Restrict to the two spill-eligible
# roles; a ``generate`` claim falls through to inference.
_HEADER_OVERRIDABLE_ROLES = frozenset({"judge", "bulk"})


def role_from_header(value: str | None) -> str | None:
    """Spill-eligible role from the ``x-anthropic-throttle-role-hint`` request
    header, or ``None`` to fall back to model-tier inference.

    Only ``bulk``/``judge`` are honored — the header can DOWNGRADE traffic onto
    the cheap lanes but can NEVER claim the premium ``generate`` lane (a
    ``generate`` value → ``None`` → inference). This is Spec 093 Q2's "optional
    later" role header, made LOAD-BEARING by the NixOS #1281 drift: the fleet's
    subagent slot is ``claude-opus-4-8[1m]`` — the SAME id as primary-generate —
    so ``infer_role`` alone maps subagent fan-out to ``generate`` and it never
    reaches the bulk chain. A consumer (the NixOS bulk clients) sets this header
    to route bulk fan-out to Kimi/GLM WITHOUT changing the model id (CI-guarded,
    #1281). Absent/unknown/``generate`` → ``None``. Never raises — an attacker-set
    header can at worst downgrade its OWN request to a cheaper lane.
    """
    if not value:
        return None
    v = value.strip().lower()
    return v if v in _HEADER_OVERRIDABLE_ROLES else None


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
