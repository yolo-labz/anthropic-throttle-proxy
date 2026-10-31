"""Session -> account affinity for Claude account routing (TD-B0a).

Prompt caches are per account. Before this module every message was routed
independently, so a session alternated between accounts and re-wrote its whole
conversation on each flip (measured 31/10/2026: an invalidation rate of 5.5 %
on calls whose bearer flipped vs 0.3 % otherwise; 94 % of the 428 resolved
invalidations in a 3 h sample followed a bearer flip).

Policy: a session stays on the account it started on. The account router still
chooses for a NEW session (budget pacing / #370 round-robin fairness are
untouched); a pinned session migrates only when its account is not routable
under the strict gates (Retry-After/429, brake, dead credential, util >= warn).

The key is ``metadata.user_id`` when present (the same derivation as
``routing.session_key_from_body`` used by the ingress lane pin, so lane and
account affinity agree), else a hash of model + system prompt + first user
message. Only the hash is stored or logged; no content, no token.

ponytail: in-process LRU with a TTL; a restart re-pins sessions (one cold
turn each). Upgrade path: persist the pins if restarts become frequent.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from contextvars import ContextVar

from . import config
from .routing import session_key_from_body

MAX_PINS = 4096
# The cache TTL we pay for is 1 h; keep a pin well past it so an idle session
# that comes back inside the TTL still finds its warm account.
TTL_S = 2 * 3600

_current: ContextVar[str | None] = ContextVar("session_affinity_key", default=None)
_pins: OrderedDict[str, tuple[str, float]] = OrderedDict()
_stats = {"new": 0, "kept": 0, "migrated": 0, "evicted": 0}


def enabled() -> bool:
    return getattr(config, "SESSION_AFFINITY", "on") != "off"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def derive_key(body: bytes | None, model: str = "") -> str | None:
    """Stable hashed session key from a request body; ``None`` when none can be derived."""
    if not body:
        return None
    uid = session_key_from_body(body)
    if uid:
        return _digest("uid:" + uid)
    try:
        obj = json.loads(body)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    messages = obj.get("messages")
    first = (
        next((m for m in messages if isinstance(m, dict) and m.get("role") == "user"), None)
        if isinstance(messages, list)
        else None
    )
    if first is None:
        return None
    parts = [model or str(obj.get("model") or ""), obj.get("system"), first.get("content")]
    return _digest("h:" + json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str))


def begin_request(body: bytes | None, model: str = "") -> str | None:
    """Bind this request's session key to the current task (read by the router and the ledger)."""
    key = derive_key(body, model) if enabled() else None
    _current.set(key)
    return key


def current_key() -> str | None:
    return _current.get()


def pinned(key: str, now: float | None = None) -> str | None:
    now = time.time() if now is None else now
    entry = _pins.get(key)
    if entry is None:
        return None
    if now - entry[1] > TTL_S:
        del _pins[key]
        _stats["evicted"] += 1
        return None
    return entry[0]


def pin(key: str, bid: str, now: float | None = None) -> None:
    """Record the account a session was just routed to; refreshes the idle clock."""
    now = time.time() if now is None else now
    previous = _pins.get(key)
    if previous is None:
        _stats["new"] += 1
    elif previous[0] != bid:
        _stats["migrated"] += 1
    else:
        _stats["kept"] += 1
    _pins[key] = (bid, now)
    _pins.move_to_end(key)
    while len(_pins) > MAX_PINS:
        _pins.popitem(last=False)
        _stats["evicted"] += 1


def stats() -> dict[str, int]:
    return {**_stats, "pins": len(_pins)}


def reset() -> None:
    """Test hook."""
    _pins.clear()
    for k in _stats:
        _stats[k] = 0
