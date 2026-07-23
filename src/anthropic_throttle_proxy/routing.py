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
import re

__all__ = ["ROLES", "infer_role", "infer_role_from_body"]

ROLES = ("generate", "judge", "bulk")

# Context-size suffixes on a model id, e.g. "claude-opus-4-8[1m]". Stripped
# before tier matching so a 1M-context opus is still "generate".
_CTX_SUFFIX = re.compile(r"\s*\[\d+[mk]\]\s*", re.IGNORECASE)


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
