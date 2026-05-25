"""Environment-derived configuration + process-global mutable state.

Everything the proxy reads from the environment lives here, along with the
shared ``state`` dict and the per-bearer registries that the Prometheus
collectors and the dashboard read. Importing this module has no side effects
beyond reading ``os.environ`` once at import time.

Tunables that are monkeypatched by the test-suite (``UTILIZATION_TARGET``,
``ADVISOR_ENABLED``, ``ADVISOR_DEBOUNCE_S``) are intentionally NOT here — they
live in :mod:`anthropic_throttle_proxy.proxy` so a ``setattr(proxy, ...)`` is
seen by the functions that read them. The AIMD tunables below are read as
plain constants (never patched) and so are safe to centralize.
"""

from __future__ import annotations

import os
import sys

UPSTREAM = os.environ.get("THROTTLE_UPSTREAM", "https://api.anthropic.com")
LISTEN_HOST = os.environ.get("THROTTLE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("THROTTLE_PORT", "8765"))
MAX_CONCURRENT = int(os.environ.get("CLAUDE_API_THROTTLE_MAX", "32"))
QUEUE_MODE = os.environ.get("THROTTLE_QUEUE_MODE", "off").strip().lower()
# PR #580: `observe` mode — no queue (request acquires a slot instantly,
# no fair-RR dispatch), but the AIMD shrink/grow counters DO move on
# upstream pushback. Result: clients see no slow-down, /__throttle/health
# + Prometheus show `max_concurrent` falling on 429 storms, and the
# operator gets the early-warning signal the wide-open `off` mode loses.
# `fair` and `reactive` keep queueing (current behaviour, both identical).
if QUEUE_MODE not in {"off", "observe", "fair", "reactive"}:
    log_mode = QUEUE_MODE
    QUEUE_MODE = "off"
else:
    log_mode = ""

# Burst pacing (standalone-repo #1, 20/05/2026): minimum gap in milliseconds
# between consecutive dispatches to the upstream / central. 0 = disabled.
# Smooths the ms-scale dogpile that hits Anthropic when 15 parallel TUIs all
# fire a request inside the same millisecond — orthogonal to MAX_CONCURRENT
# (which is the in-flight ceiling, not a rate cap). Recommended floor: 50
# (= 20 req/s peak burst to upstream); 100 = gentler 10 req/s peak.
MIN_DISPATCH_GAP_S = float(os.environ.get("THROTTLE_MIN_DISPATCH_GAP_MS", "0")) / 1000.0

# Central-tier opt-in: when set, the local proxy forwards each request to
# this URL instead of straight to upstream. Empty = direct upstream.
CENTRAL_URL = os.environ.get("THROTTLE_CENTRAL_URL", "").rstrip("/")
CENTRAL_HEALTH_PATH = "/__throttle/health"
CENTRAL_HEALTH_INTERVAL = float(os.environ.get("THROTTLE_CENTRAL_HEALTH_INTERVAL", "30"))
CENTRAL_HEALTH_TIMEOUT = float(os.environ.get("THROTTLE_CENTRAL_HEALTH_TIMEOUT", "5"))
CENTRAL_FORWARD_TIMEOUT = float(os.environ.get("THROTTLE_CENTRAL_FORWARD_TIMEOUT", "10"))

# PR #575: AIMD reactive throttle. "Wide open + throttle as we need":
# start at MAX_CONCURRENT (the hard ceiling), shrink multiplicatively on
# upstream pushback (429/503/529), and ramp back up additively after a
# cooldown of consecutive successes. Net: opens to the full hardware
# parallelism Pedro asks for, but backs off the moment Anthropic pushes
# back — no static cap to babysit.
AIMD_MIN = int(os.environ.get("THROTTLE_AIMD_MIN", "1"))
AIMD_BACKOFF_S = float(os.environ.get("THROTTLE_AIMD_BACKOFF_S", "30"))
AIMD_RAMP_AFTER = int(os.environ.get("THROTTLE_AIMD_RAMP_AFTER", "10"))
# AIMD multiplicative-decrease factor. TCP Reno halves (0.5, deep teeth, fast
# convergence, more wasted headroom); CUBIC cuts ~30% (0.7, shallower sawtooth,
# higher average utilisation). We default to 0.7 to glide closer to the limit
# after each pushback. Floor is AIMD_MIN.
AIMD_DECREASE = float(os.environ.get("THROTTLE_AIMD_DECREASE", "0.7"))
# Rate pushback → AIMD multiplicative-decrease (YOUR usage is too high).
AIMD_STATUSES = {429, 503}
# 529 = upstream OVERLOADED (Anthropic-side capacity, NOT your usage). We honor
# any retry-after and count it separately, but do NOT shrink the ceiling —
# shrinking would throttle you for someone else's capacity problem.
OVERLOAD_STATUSES = {529}
# Any throttle-ish status worth an advisor diagnosis.
THROTTLE_STATUSES = AIMD_STATUSES | OVERLOAD_STATUSES

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

state: dict[str, object] = {
    "inflight": 0,
    "queued": 0,
    "served": 0,
    "client_disconnects": 0,
    "upstream_retries": 0,
    "central_status": "unknown",
    "central_last_check": 0,
    # last_advisor holds {"text", "ts", "trigger"} from the GROQ advisor.
    "last_advisor": None,
}
# PR #562 + PR #573: bearer_id → FairBearerLimiter(MAX_CONCURRENT). Replaces
# the plain asyncio.Semaphore so two distinct OAuth bearers still get
# independent slot pools, AND within one bearer the slots are dispatched
# round-robin across distinct client TCPs so no claude-TUI starves.
# bearer_limiters maps bearer_id → FairBearerLimiter.
bearer_limiters: dict[str, object] = {}
# bearer_state maps bearer_id → {inflight, queued, served, clients}.
bearer_state: dict[str, dict[str, object]] = {}


def log(msg: str) -> None:
    """Write a single timestamp-free diagnostic line to stderr (unbuffered)."""
    sys.stderr.write(f"[anthropic-throttle] {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Runtime overrides (PR #22 — /ui/config editor)
# ---------------------------------------------------------------------------
#
# The values defined above are loaded once at import from the environment.
# The /ui dashboard exposes a small subset of these as *runtime-mutable* via a
# POST /ui/config endpoint: setting one writes the value to a JSON state file
# AND mutates the module attribute in-place (`config.MAX_CONCURRENT = N`,
# `body_shrink.CAP_BYTES = N`, `proxy.UTILIZATION_TARGET = N`, …). The hot
# paths read these knobs via attribute access (`config.MAX_CONCURRENT`,
# `if CAP_BYTES <= 0`, `if UTILIZATION_TARGET > 0`), so the mutation is picked
# up by every subsequent request without restart.
#
# Knobs that CANNOT be hot-mutated (because they change wire topology —
# UPSTREAM, CENTRAL_URL, LISTEN_HOST/PORT, QUEUE_MODE) stay env-only and the
# UI shows them as `restart required`.

import json as _json  # noqa: E402 — kept inline for visibility of new surface
from pathlib import Path as _Path  # noqa: E402
from typing import Any as _Any  # noqa: E402


def _state_dir() -> _Path:
    """``$XDG_STATE_HOME/anthropic-throttle-proxy`` (or its fallback)."""
    base = os.environ.get("XDG_STATE_HOME") or str(_Path.home() / ".local" / "state")
    return _Path(base) / "anthropic-throttle-proxy"


OVERRIDES_FILE = _state_dir() / "overrides.json"

# ENV_DEFAULTS snapshots the env-derived value of every editable knob at
# import time so the UI can show "ENV default vs runtime override" and offer
# a reset button. Populated below after the knob schema is declared.
ENV_DEFAULTS: dict[str, _Any] = {}

# RUNTIME_OVERRIDES tracks what's been changed via the UI since startup.
# Empty == every knob still on its ENV_DEFAULTS value.
RUNTIME_OVERRIDES: dict[str, _Any] = {}


def _set_module_attr(module_path: str, attr: str, value: _Any) -> None:
    """``setattr(<module>, attr, value)`` with lazy import to avoid cycles."""
    import importlib

    mod = importlib.import_module(module_path)
    setattr(mod, attr, value)


def _set_max_concurrent(v: int) -> None:
    _set_module_attr("anthropic_throttle_proxy.config", "MAX_CONCURRENT", v)


def _set_min_dispatch_gap_ms(v: int) -> None:
    _set_module_attr("anthropic_throttle_proxy.config", "MIN_DISPATCH_GAP_S", float(v) / 1000.0)


def _set_aimd_min(v: int) -> None:
    _set_module_attr("anthropic_throttle_proxy.config", "AIMD_MIN", v)


def _set_aimd_backoff_s(v: float) -> None:
    _set_module_attr("anthropic_throttle_proxy.config", "AIMD_BACKOFF_S", v)


def _set_aimd_ramp_after(v: int) -> None:
    _set_module_attr("anthropic_throttle_proxy.config", "AIMD_RAMP_AFTER", v)


def _set_aimd_decrease(v: float) -> None:
    _set_module_attr("anthropic_throttle_proxy.config", "AIMD_DECREASE", v)


def _set_body_shrink_cap_bytes(v: int) -> None:
    _set_module_attr("anthropic_throttle_proxy.body_shrink", "CAP_BYTES", v)


def _set_body_shrink_keep_turns(v: int) -> None:
    _set_module_attr("anthropic_throttle_proxy.body_shrink", "KEEP_TURNS", max(2, v))


def _set_body_shrink_min_block_bytes(v: int) -> None:
    _set_module_attr("anthropic_throttle_proxy.body_shrink", "MIN_BLOCK_BYTES", v)


def _set_utilization_target(v: float) -> None:
    _set_module_attr("anthropic_throttle_proxy.proxy", "UTILIZATION_TARGET", v)


def _set_advisor_enabled(v: bool) -> None:
    os.environ["ADVISOR_ENABLED"] = "true" if v else "false"
    _set_module_attr("anthropic_throttle_proxy.proxy", "ADVISOR_ENABLED", bool(v))


# EDITABLE_KNOBS is the single source of truth the UI consumes. Each entry:
#   key:          identifier in URLs, form fields, state file
#   label:        operator-facing name
#   type:         "int" | "float" | "bool"  (parser + input type)
#   min/max:      validation bounds (None = unbounded)
#   getter:       returns current effective value (env default OR runtime override)
#   setter:       mutates the right module attr; called after override-store update
#   units/help:   tooltip text + suffix in the form
EDITABLE_KNOBS: dict[str, dict[str, _Any]] = {
    "max_concurrent": {
        "label": "Max concurrent (per bearer)",
        "type": "int",
        "min": 1,
        "max": 512,
        "getter": lambda: MAX_CONCURRENT,
        "setter": _set_max_concurrent,
        "units": "slots",
        "help": (
            "Hard ceiling on in-flight requests per bearer. "
            "AIMD shrinks live cap below this on pushback."
        ),
    },
    "min_dispatch_gap_ms": {
        "label": "Min dispatch gap",
        "type": "int",
        "min": 0,
        "max": 10000,
        "getter": lambda: int(MIN_DISPATCH_GAP_S * 1000),
        "setter": _set_min_dispatch_gap_ms,
        "units": "ms",
        "help": "Minimum gap between two consecutive upstream POSTs (burst pacing). 0 = disabled.",
    },
    "utilization_target": {
        "label": "Utilization target",
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "getter": lambda: _get_proxy_attr("UTILIZATION_TARGET", 0.0),
        "setter": _set_utilization_target,
        "units": "(0=off)",
        "help": (
            "Proactively shrink AIMD ceiling once the binding 5h/7d "
            "unified-window crosses this fraction."
        ),
    },
    "aimd_min": {
        "label": "AIMD floor",
        "type": "int",
        "min": 1,
        "max": 512,
        "getter": lambda: AIMD_MIN,
        "setter": _set_aimd_min,
        "units": "slots",
        "help": "Floor under multiplicative-decrease shrink. Live cap never drops below this.",
    },
    "aimd_backoff_s": {
        "label": "AIMD cooldown",
        "type": "float",
        "min": 0.0,
        "max": 3600.0,
        "getter": lambda: AIMD_BACKOFF_S,
        "setter": _set_aimd_backoff_s,
        "units": "s",
        "help": "After a shrink, wait this many seconds before additive-increase can resume.",
    },
    "aimd_ramp_after": {
        "label": "AIMD ramp threshold",
        "type": "int",
        "min": 1,
        "max": 10000,
        "getter": lambda: AIMD_RAMP_AFTER,
        "setter": _set_aimd_ramp_after,
        "units": "successes",
        "help": "Consecutive 200s past the cooldown before live cap grows by +1.",
    },
    "aimd_decrease": {
        "label": "AIMD shrink factor",
        "type": "float",
        "min": 0.1,
        "max": 0.95,
        "getter": lambda: AIMD_DECREASE,
        "setter": _set_aimd_decrease,
        "units": "x",
        "help": (
            "Multiplicative-decrease factor on 429/503. "
            "0.5 = TCP Reno; 0.7 = CUBIC-style (default)."
        ),
    },
    "body_shrink_cap_bytes": {
        "label": "Body-shrink cap",
        "type": "int",
        "min": 0,
        "max": 33_554_432,  # 32 MiB upper bound matches Anthropic's hard cap
        "getter": lambda: _get_body_shrink_attr("CAP_BYTES", 0),
        "setter": _set_body_shrink_cap_bytes,
        "units": "bytes",
        "help": "Soft cap below which body_shrink does not trim. 0 disables the whole feature.",
    },
    "body_shrink_keep_turns": {
        "label": "Body-shrink keep turns",
        "type": "int",
        "min": 2,
        "max": 64,
        "getter": lambda: _get_body_shrink_attr("KEEP_TURNS", 4),
        "setter": _set_body_shrink_keep_turns,
        "units": "msgs",
        "help": "Trailing messages left untouched by the trimmer.",
    },
    "body_shrink_min_block_bytes": {
        "label": "Body-shrink min block",
        "type": "int",
        "min": 0,
        "max": 1_048_576,
        "getter": lambda: _get_body_shrink_attr("MIN_BLOCK_BYTES", 2048),
        "setter": _set_body_shrink_min_block_bytes,
        "units": "bytes",
        "help": "Skip trimming tool_result blocks whose serialized size is below this threshold.",
    },
    "advisor_enabled": {
        "label": "GROQ advisor",
        "type": "bool",
        "getter": lambda: os.environ.get("ADVISOR_ENABLED", "false").strip().lower() == "true",
        "setter": _set_advisor_enabled,
        "help": "Auto-fire a GROQ diagnosis on throttle events. Requires GROQ_API_KEY.",
    },
}


def _get_body_shrink_attr(name: str, default: _Any) -> _Any:
    """Read a body_shrink module attr defensively (module may not be imported yet)."""
    try:
        import importlib

        mod = importlib.import_module("anthropic_throttle_proxy.body_shrink")
        return getattr(mod, name, default)
    except Exception:
        return default


def _get_proxy_attr(name: str, default: _Any) -> _Any:
    """Read a proxy module attr defensively."""
    try:
        import importlib

        mod = importlib.import_module("anthropic_throttle_proxy.proxy")
        return getattr(mod, name, default)
    except Exception:
        return default


def _capture_env_defaults() -> None:
    """Snapshot each knob's current effective value before any overrides apply."""
    for key, spec in EDITABLE_KNOBS.items():
        try:
            ENV_DEFAULTS[key] = spec["getter"]()
        except Exception:
            ENV_DEFAULTS[key] = None


def _coerce(spec: dict[str, _Any], raw: _Any) -> _Any:
    """Parse a raw form/JSON value into the declared type, with bounds check."""
    t = spec["type"]
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"true", "1", "on", "yes"}
    if t == "int":
        v = int(raw)
    elif t == "float":
        v = float(raw)
    else:
        raise ValueError(f"unsupported knob type: {t!r}")
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and v < lo:
        raise ValueError(f"{spec['label']}: value {v} below min {lo}")
    if hi is not None and v > hi:
        raise ValueError(f"{spec['label']}: value {v} above max {hi}")
    return v


def set_override(key: str, raw_value: _Any) -> _Any:
    """Validate, persist, and propagate a single knob override.

    Returns the coerced value that's now live. Raises ``KeyError`` for unknown
    keys and ``ValueError`` for type / bounds violations.
    """
    if key not in EDITABLE_KNOBS:
        raise KeyError(f"unknown knob: {key!r}")
    spec = EDITABLE_KNOBS[key]
    value = _coerce(spec, raw_value)
    RUNTIME_OVERRIDES[key] = value
    spec["setter"](value)
    save_overrides()
    log(f"config override set: {key}={value} (was env default {ENV_DEFAULTS.get(key)})")
    return value


def reset_override(key: str) -> _Any:
    """Drop a runtime override; restore the env default value. Returns the restored value."""
    if key not in EDITABLE_KNOBS:
        raise KeyError(f"unknown knob: {key!r}")
    spec = EDITABLE_KNOBS[key]
    RUNTIME_OVERRIDES.pop(key, None)
    default = ENV_DEFAULTS.get(key)
    if default is not None:
        spec["setter"](default)
    save_overrides()
    log(f"config override reset: {key} → env default {default}")
    return default


def load_overrides() -> None:
    """Read the on-disk overrides file (if any) and re-apply each entry.

    Called once from the proxy entrypoint *after* every module that owns an
    editable attr has been imported, so the setters can find the targets.
    Missing or unreadable file is a no-op (all knobs stay on env defaults).
    """
    _capture_env_defaults()
    if not OVERRIDES_FILE.is_file():
        return
    try:
        data = _json.loads(OVERRIDES_FILE.read_text())
    except Exception as exc:
        log(f"config: cannot read {OVERRIDES_FILE} ({exc!r}); skipping overrides")
        return
    if not isinstance(data, dict):
        log(f"config: {OVERRIDES_FILE} not a JSON object; skipping")
        return
    for key, raw_value in data.items():
        if key not in EDITABLE_KNOBS:
            log(f"config: ignoring unknown override key {key!r}")
            continue
        try:
            value = _coerce(EDITABLE_KNOBS[key], raw_value)
        except ValueError as exc:
            log(f"config: ignoring invalid override {key}={raw_value!r} ({exc})")
            continue
        RUNTIME_OVERRIDES[key] = value
        EDITABLE_KNOBS[key]["setter"](value)
    log(f"config: loaded {len(RUNTIME_OVERRIDES)} override(s) from {OVERRIDES_FILE}")


def save_overrides() -> None:
    """Persist RUNTIME_OVERRIDES to disk. Best-effort; logs on failure."""
    try:
        OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDES_FILE.write_text(_json.dumps(RUNTIME_OVERRIDES, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        log(f"config: cannot persist overrides to {OVERRIDES_FILE} ({exc!r})")


def knob_snapshot() -> list[dict[str, _Any]]:
    """Render each editable knob as a row for the UI form.

    Each row carries enough fields to fully render itself (label, type, value,
    default, override flag, help text, units, min/max bounds).
    """
    rows: list[dict[str, _Any]] = []
    for key, spec in EDITABLE_KNOBS.items():
        try:
            current = spec["getter"]()
        except Exception:
            current = None
        rows.append(
            {
                "key": key,
                "label": spec["label"],
                "type": spec["type"],
                "value": current,
                "default": ENV_DEFAULTS.get(key),
                "override": key in RUNTIME_OVERRIDES,
                "help": spec.get("help", ""),
                "units": spec.get("units", ""),
                "min": spec.get("min"),
                "max": spec.get("max"),
            }
        )
    return rows
