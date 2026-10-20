"""Free-tier budget admission for the OpenCode free gateways (:8770 go, :8774 zen).

WHAT THIS IS
============

A per-model, per-window **request allowance** consulted before a slot is granted
and again immediately before upstream transport, plus the **upstream wall** an
honest ``Retry-After`` gives us. When either says stop, the request is refused
locally with a bounded ``Retry-After`` — no slot is burned, no vendor request is
manufactured, and no AIMD signal is produced (the refusal is minted here, never
observed as pushback).

WHY THE BUDGET HAS TO BE DECLARED, NOT READ (19/10/2026 receipts)
=================================================================

The mission assumes a readable "remaining free-tier budget". It is not readable
anywhere this host can see. Measured, not guessed:

1. **Not on the wire.** A real ``POST /chat/completions`` against the Go free
   catalog (``longcat-2.5-preview-free``, 19/10/2026 19:24 BRT, HTTP 200) came
   back with exactly ``date, content-type, x-opencode-log-id, x-zen-model,
   content-encoding, server, cf-ray`` — no ``x-ratelimit-*``, no ``retry-after``,
   no quota/usage header of any kind. The 429 class is headerless too: the go
   gate logged ``rate-pushback-retry status=429 retry_after=0.0
   synthetic_pause=True`` (19/10 19:20:15).
2. **Not in the lane report.** ``/run/user/1000/throttle-lanes.json`` carries
   ``codex:a``, ``codex:c``, ``copilot:personal``, ``zai:plan`` and
   ``anthropic:proxy`` — no ``opencode:*`` row exists, so
   ``lanes.plan_meter_used_percent`` can never answer for these gateways.
3. **Not in the client.** ``~/.local/share/opencode/opencode.db`` has no usage/
   quota/limit table (tables: session, message, part, account, credential, kv,
   event, …), and the gateway itself serves only ``chat/completions``,
   ``/health``, ``/healthz``, ``/status`` — there is no usage endpoint to poll.

So there is exactly one source, and this module owns it:

* **declared** — an operator states the allowance per model
  (``THROTTLE_FREE_BUDGETS``); we count our own dispatches against it and stop
  *before* the vendor does. This is the only way to "queue, never cross the
  line": the ceiling is discovered by declaring it, never by hitting it.

The obvious second source — a vendor ``Retry-After`` wall — is deliberately NOT
reimplemented here. It already has two owners on this host, both live and both
per-bearer: ``FairBearerLimiter.note_retry_after`` latches it and
``_pre_dispatch_retry_after`` fast-fails every later request for its duration
(the zen gate did exactly this at 19/10 18:57:20: ``retry-after-fast-fail
remaining=7361 max_hold=240``). A second oracle for a fact the limiter already
publishs is how this repo got bitten before. And a *headerless* 429 (the go
  gate's 19:20:15 case) carries no wall at all — that is precisely the case a
local count answers and a header cannot.

DEFAULT OFF. With neither source configured the module is inert and every
caller keeps its historical behaviour — an unconfigured gate must never start
refusing traffic, and an unknown budget must never be invented.

RELATIONSHIP TO THE EXISTING METER MACHINERY (read before extending)
====================================================================

* ``config.PLAN_METER_LANE`` / ``_plan_lane_has_headroom`` classify a *headerless
  429* against ``lanes.plan_meter_used_percent``. That is a **plan-lane** reader
  over the lane report; it is short-circuited to ``False`` (no headroom ⇒ treat
  as budget) when the knob is unset, which is the case on both free gateways.
  This module is the equivalent evidence source for lanes that have no row in
  that report, and it feeds the same predicate.
* ``THROTTLE_PROSPECTIVE_MODE`` + ``ledger.Budgets`` are a **token reservation**
  system over a fixed ``ledger.WINDOW_S`` (60 s) rolling window, driven by a
  JSON manifest with exclusive file custody. Its window is a *rate* window, not
  a per-day allowance, and its refusal payload is what this module deliberately
  **reuses** (``ProspectiveRefusal``/``local_refusal_response``) so both local
  refusal paths speak one bounded, provenance-stamped protocol. The reservation
  machinery itself is untouched.
* The proxy's local-refusal provenance flags (``local_prospective_refused`` +
  ``aimd_owned``) are reused for exactly what they already guarantee: no
  pushback-retry, no AIMD feedback, no fast-fail rewriting.

CEILINGS (stated, not hidden)
=============================

* Fixed windows, not sliding: a model can spend its whole allowance in the last
  second of one window and again in the first of the next (classic fixed-window
  boundary burst). Chosen because only a fixed window yields an honest
  ``Retry-After`` ("retry at window end"); a sliding window has no such answer.
  Upgrade path: replace ``_windows`` bookkeeping with a sliding ring.
* Counts are **dispatch attempts**, not vendor-acknowledged consumption: an
  attempt that dies in TLS/connect still counted. Conservative on our side,
  which is the safe direction for a budget.
* One process = one counter. This host's go-gate spend is fully covered by its
  own process; another host behind the same OpenCode account is invisible here.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass

from .config import log
from .prospective_refusal import (
    RETRY_AFTER_MAX_S,
    RETRY_AFTER_MIN_S,
    ProspectiveRefusal,
    RefusalReason,
    local_refusal_response,
)

__all__ = [
    "Decision",
    "DEFAULT_PRESSURE_PCT",
    "DEFAULT_WINDOW_S",
    "admit",
    "admit_and_count",
    "budget_blocks",
    "enabled",
    "load_from_env",
    "reset_for_tests",
    "snapshot",
]

# Per-day is the mission's window; seconds so a caller can shorten it per model.
DEFAULT_WINDOW_S = 86400
# Classification line (NOT the admission line): at/above this used-% a pushback
# is read as budget pressure rather than concurrency. Admission stops at 100 %.
DEFAULT_PRESSURE_PCT = 90.0
# Retry-After offered when we must refuse but have no clock to offer (an
# undeclared model in required mode, or an invalid declaration).
UNDECLARED_RETRY_AFTER_S = 60

# Operator-facing budget label charset, inherited from ProspectiveRefusal.
_LABEL_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,62}\Z")
# ``\d`` rather than ``[0-9]`` (concise character class). No ``re.ASCII``: the
# flag would ALSO narrow ``\s`` in the model-name group, changing which model
# names parse. ``\d`` widens digits to Unicode Nd, and ``int()`` accepts those
# too (``int("٣") == 3``), so the parsed allowance is unchanged and no new
# conversion path can raise.
_ENTRY_RE = re.compile(r"\A([^\s=]+)=(\d+)(?:/(\d+))?\Z")
_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BudgetSpec:
    """One declared allowance: ``model`` may be dispatched ``budget`` times per ``window_s``."""

    model: str
    budget: int
    window_s: int
    label: str


@dataclass(frozen=True)
class Decision:
    """The verdict for one admission consult.

    ``reason`` is a stable machine token (never free text): ``disabled`` /
    ``ok`` / ``undeclared`` (allowed) and ``budget-exhausted`` / ``undeclared`` /
    ``invalid-config`` (refused).
    """

    allowed: bool
    reason: str
    retry_after_s: int
    budget_label: str
    model: str
    used: int
    budget: int | None

    @property
    def refusal(self) -> ProspectiveRefusal | None:
        """Bounded local-refusal payload for a refused decision, else ``None``."""
        if self.allowed:
            return None
        reason = {
            "budget-exhausted": RefusalReason.EXHAUSTED,
            "undeclared": RefusalReason.UNBOUND,
            "invalid-config": RefusalReason.UNKNOWN,
        }.get(self.reason, RefusalReason.UNKNOWN)
        return ProspectiveRefusal(reason, self._bounded_retry(), self.budget_label)

    @property
    def response(self):
        """aiohttp 503 for a refused decision, else ``None``."""
        refusal = self.refusal
        return None if refusal is None else local_refusal_response(refusal)

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "retry_after_s": self.retry_after_s,
            "budget": self.budget_label,
            "model": self.model,
            "used": self.used,
            "budget_requests": self.budget,
        }

    def _bounded_retry(self) -> int:
        return max(RETRY_AFTER_MIN_S, min(RETRY_AFTER_MAX_S, int(self.retry_after_s)))


# RLock: ``budget_blocks`` recurses over the declared models while holding it.
_lock = threading.RLock()
_specs: dict[str, BudgetSpec] = {}
_windows: dict[str, tuple[float, int]] = {}
_lane = "free"
_required = False
_invalid = False
_pressure_pct = DEFAULT_PRESSURE_PCT
_config_error = ""


def _slug(value: str) -> str:
    """Sanitize to a non-empty ``[a-z0-9][a-z0-9._-]{0,62}`` fragment."""
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-.")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"f{cleaned}" if cleaned else "f"
    return cleaned


def _label_for(model: str) -> str:
    """Public budget label for a refusal: lane + model, charset/length bounded."""
    label = f"{_lane}-{_slug(model or '*')}"
    return label[:63]


def _clamp_retry(seconds: float) -> int:
    return max(RETRY_AFTER_MIN_S, min(RETRY_AFTER_MAX_S, int(seconds + 0.999)))


def _current_window(model: str, moment: float, window_s: int) -> tuple[float, int]:
    """``(started, used)`` for ``model``'s window as of ``moment``. Caller holds the lock.

    First consult opens the window at ``moment`` (never at epoch 0 — a synthetic
    clock in a test must see the same fixed-window arithmetic production does),
    and a window that has elapsed, or whose start sits in the future after a
    clock step, reopens at ``moment`` (the bounded-clock rule ``lanes.view``
    already follows: negative age is not freshness).
    """
    state = _windows.get(model)
    if state is None or moment < state[0] or moment - state[0] >= window_s:
        return moment, 0
    return state


def _deny(
    model: str, reason: str, retry_after_s: float, *, used: int = 0, budget: int | None = None
) -> Decision:
    return Decision(
        False, reason, _clamp_retry(retry_after_s), _label_for(model), model, used, budget
    )


def reset_for_tests() -> None:
    """Return to the inert (default-off) state; no env is read."""
    global _specs, _windows, _lane, _required, _invalid, _pressure_pct, _config_error
    with _lock:
        _specs = {}
        _windows = {}
        _lane = "free"
        _required = False
        _invalid = False
        _pressure_pct = DEFAULT_PRESSURE_PCT
        _config_error = ""


def _env_text(env: Mapping[str, str], name: str) -> str:
    """Trimmed text of one knob; empty when unset or nullish."""
    return str(env.get(name, "") or "").strip()


def _parse_budgets(raw: str) -> tuple[dict[str, BudgetSpec], bool, list[str]]:
    """``(specs, invalid, errors)`` for ``THROTTLE_FREE_BUDGETS``.

    Each entry is ``model=budget[/window_s]``. ONE malformed entry invalidates
    the whole declaration (spec 289's rule — a bad entry must never vanish into
    feature-off) while every valid entry is still returned, so the journal can
    name exactly what failed to parse.
    """
    specs: dict[str, BudgetSpec] = {}
    errors: list[str] = []
    if not raw:
        return specs, False, errors
    invalid = False
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            invalid = True
            errors.append("empty entry")
            continue
        match = _ENTRY_RE.match(entry)
        if not match:
            invalid = True
            errors.append(entry)
            continue
        model, budget_raw, window_raw = match.groups()
        budget, window = int(budget_raw), int(window_raw or DEFAULT_WINDOW_S)
        if budget <= 0 or window <= 0 or model in specs:
            invalid = True
            errors.append(entry)
            continue
        specs[model] = BudgetSpec(model, budget, window, _label_for(model))
    return specs, invalid, errors


def _parse_pressure(raw: str) -> tuple[float, str | None]:
    """``(pressure_pct, error)`` for ``THROTTLE_FREE_BUDGET_PRESSURE_PCT``.

    Unset → the default with no error. Malformed → default + the raw text.
    Out-of-range → the PARSED value + the raw text: the declaration fails
    closed either way, and the caller stores exactly what it was given.
    """
    if not raw:
        return DEFAULT_PRESSURE_PCT, None
    try:
        pressure = float(raw)
    except ValueError:
        return DEFAULT_PRESSURE_PCT, raw
    return pressure, raw if not 0.0 <= pressure <= 100.0 else None


def _lane_from(lane_raw: str, upstream: str) -> str:
    """Declared lane, else the upstream's last path segment (…/go → go)."""
    if lane_raw:
        return _slug(lane_raw) or "free"
    parts = [p for p in upstream.rstrip("/").split("/") if p]
    return _slug(parts[-1] if parts else "free") or "free"


def load_from_env(environ: Mapping[str, str] | None = None) -> None:
    """(Re)parse the knobs into module state. Malformed declarations fail closed.

    A bad entry is never silently feature-off: ``_invalid`` is set, every
    admission refuses with ``invalid-config``, and the parse error is logged so
    the journal names the mistake (spec 289's rule — a bad entry must not vanish
    into feature-off).

    One helper per knob, then a single state commit: the parse-everything form
    blew the cognitive-complexity budget (Sonar S3776, 33 vs 15, 19/10/2026),
    and splitting it keeps every decision local and re-readable. The state
    written — and the order labels are re-stamped in — is unchanged.
    """
    global _specs, _lane, _required, _invalid, _pressure_pct, _config_error
    env = os.environ if environ is None else environ
    specs, invalid, errors = _parse_budgets(_env_text(env, "THROTTLE_FREE_BUDGETS"))
    pressure, pressure_error = _parse_pressure(_env_text(env, "THROTTLE_FREE_BUDGET_PRESSURE_PCT"))
    if pressure_error is not None:
        invalid = True
        errors.append(pressure_error)
    lane = _lane_from(_env_text(env, "THROTTLE_FREE_LANE"), _env_text(env, "THROTTLE_UPSTREAM"))
    required = _env_text(env, "THROTTLE_FREE_BUDGET_REQUIRED").lower() in _TRUE
    config_error = ", ".join(errors[:4])

    with _lock:
        _lane = lane
        # Labels are lane-scoped: re-stamp them now that the lane is known.
        _specs = {
            model: BudgetSpec(spec.model, spec.budget, spec.window_s, _label_for(model))
            for model, spec in specs.items()
        }
        _required = required
        _invalid = invalid
        _pressure_pct = pressure
        _windows = {}
        _config_error = config_error
    # One journal line per parse outcome — the receipt an operator reads.
    if invalid:
        log(f"free-budget-config-error lane={lane} entries={config_error} (failing closed)")
    elif specs:
        log(
            f"free-budget-enabled lane={lane} models={len(specs)} "
            f"required={required} pressure={pressure:g} "
            f"window_s={sorted({s.window_s for s in specs.values()})}"
        )


load_from_env()


def enabled() -> bool:
    """Is any consult meaningful on this process? False ⇒ every caller no-ops."""
    return _invalid or bool(_specs)


def admit(model: str, *, now: float | None = None, count: bool = False) -> Decision:
    """May this model be dispatched now? Pure check unless ``count=True``.

    ``count`` is taken only on a decision that actually allows, and is reserved
    for the point immediately before transport: a request that waits in the queue
    or dies client-side must not have spent budget. The pre-slot consult passes
    ``count=False`` so it can refuse early without claiming a unit.
    """
    moment = time.time() if now is None else float(now)
    with _lock:
        if _invalid:
            return _deny(model, "invalid-config", UNDECLARED_RETRY_AFTER_S)
        if not _specs:
            return Decision(True, "disabled", 0, _label_for(model), model, 0, None)
        spec = _specs.get(model)
        if spec is None:
            if _required:
                return _deny(model, "undeclared", UNDECLARED_RETRY_AFTER_S)
            return Decision(True, "undeclared", 0, _label_for(model), model, 0, None)
        started, used = _current_window(model, moment, spec.window_s)
        _windows[model] = (started, used)
        if used >= spec.budget:
            return _deny(
                model,
                "budget-exhausted",
                started + spec.window_s - moment,
                used=used,
                budget=spec.budget,
            )
        if count:
            used += 1
            _windows[model] = (started, used)
        return Decision(True, "ok", 0, spec.label, model, used, spec.budget)


def admit_and_count(model: str, *, now: float | None = None) -> Decision:
    """Admission consult that claims one unit when it allows (pre-transport seam)."""
    return admit(model, now=now, count=True)


def _state(model: str, now: float) -> tuple[int, int | None]:
    """``(used, budget)`` for a declared model in its current window, else (0, None)."""
    spec = _specs.get(model)
    if spec is None:
        return 0, None
    _started, used = _current_window(model, now, spec.window_s)
    return used, spec.budget


def budget_blocks(model: str = "", *, now: float | None = None) -> bool | None:
    """Three-valued budget verdict for pushback classification.

    ``True``  — we KNOW this is budget: the model's window allowance is spent, or
                used-% is at/over the pressure line. Never shrink on this: a
                budget wall is time-shaped, not concurrency-shaped, and width is
                exactly what must survive the reset (spec 335; the 19/10
                incident was a width collapse).
    ``False`` — we KNOW headroom: declared and below the pressure line. A
                headerless 429 here is concurrency/rate, so the short cooldown
                applies and the ceiling is untouched.
    ``None``  — no evidence (module off, or this model is not declared). The
                caller keeps its historical behaviour; UNKNOWN IS NOT HEADROOM
                is preserved by never returning ``False`` without a reading.
    """
    moment = time.time() if now is None else float(now)
    with _lock:
        if _invalid:
            return True
        if not _specs:
            return None
        if not model:
            # No model supplied: the WALLEST declared model decides (conservative).
            if not _specs:
                return None
            return any(budget_blocks(name, now=moment) for name in _specs)
        spec = _specs.get(model)
        if spec is None:
            return True if _required else None
        _started, used = _current_window(model, moment, spec.window_s)
        if used >= spec.budget:
            return True
        return (used / spec.budget) * 100.0 >= _pressure_pct


def snapshot(*, now: float | None = None) -> dict[str, object]:
    """Read-only view for logs/tests; a future ``/__throttle/health`` hook can
    publish this verbatim. Nothing here is secret (counts and public labels)."""
    moment = time.time() if now is None else float(now)
    with _lock:
        return {
            "enabled": enabled(),
            "invalid": _invalid,
            "lane": _lane,
            "required": _required,
            "pressure_pct": _pressure_pct,
            "models": {
                name: {
                    "budget": spec.budget,
                    "window_s": spec.window_s,
                    "used": _state(name, moment)[0],
                    "label": spec.label,
                }
                for name, spec in _specs.items()
            },
        }
