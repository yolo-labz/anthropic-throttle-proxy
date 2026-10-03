"""Prospective-admission LOCAL refusal: typed immutable payload + bounded 503 factory.

Spec 245 T004 (``specs/245-prospective-admission/t004-refusal-plan.md``).

Default-off by construction: nothing in the hot path imports this module and no
route/wiring references it. It is a small payload library for the future
prospective-admission boundary, deliberately self-contained (stdlib + aiohttp
only, no config/accounts/lanes imports).

Design constraints honored here:

* The refusal is LOCAL policy. Provenance headers are module constants applied by
  the factory itself — **never** inferred from incoming headers, query markers or
  request metadata of any kind.
* Reasons are a finite allowlist (the meter-state taxonomy minus ``ok``): a
  caller cannot invent a reason string.
* ``Retry-After`` is a bounded positive integer (``1..RETRY_AFTER_MAX_S``), never
  a float, bool or unbounded value.
* The budget label is a *public* label whose CHARSET AND LENGTH are validated
  (no CRLF/control/quote/``/``-path injection possible by construction). The
  validator CANNOT know which strings are secret or account-derived, and it
  does not reject ``..``: the caller must supply an operator-configured public
  label (e.g. a config-declared name), never derive one from request data.
* Only the payload is produced. The actual SSE terminal helper is
  ``proxy._emit_sse_error_terminal(sse_resp, message, error_type)`` — it takes
  ``(sse_resp, message, error_type)``, NOT an envelope dict, so an **adapter is
  required** at wiring time (see the plan); this module never writes streams
  and does not claim drop-in compatibility.

Provenance stripping (``strip_incoming_provenance``) exists for FUTURE trust
boundaries (proxy-to-proxy hops, ingestion of forwarded headers). It is NOT
wired anywhere yet; see the plan's "Remaining wiring".
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "ERROR_TYPE",
    "PROVENANCE_HEADERS",
    "RETRY_AFTER_MAX_S",
    "RETRY_AFTER_MIN_S",
    "RefusalReason",
    "ProspectiveRefusal",
    "local_refusal_response",
    "strip_incoming_provenance",
]

# The error envelope ``type`` for this refusal class (Anthropic-protocol error
# envelope shape: ``{"type": "error", "error": {"type": ..., "message": ...}}``).
ERROR_TYPE: Final[str] = "prospective_admission_refused"

RETRY_AFTER_MIN_S: Final[int] = 1
RETRY_AFTER_MAX_S: Final[int] = 86400

# Provenance the LOCAL factory always stamps. Distinct headers, so a future
# boundary can classify the refusal. These values do not authenticate the peer;
# trust must come from the configured relay boundary, never an incoming marker.
PROVENANCE_SOURCE_HEADER: Final[str] = "x-throttle-refusal-source"
PROVENANCE_LOCAL_VALUE: Final[str] = "local"
PROVENANCE_CLASS_HEADER: Final[str] = "x-throttle-prospective-refusal"
PROVENANCE_BUDGET_HEADER: Final[str] = "x-throttle-prospective-budget"
PROVENANCE_REASON_HEADER: Final[str] = "x-throttle-prospective-reason"
PROVENANCE_HEADERS: Final[Mapping[str, str]] = {
    PROVENANCE_SOURCE_HEADER: PROVENANCE_LOCAL_VALUE,
    PROVENANCE_CLASS_HEADER: "1",
}

# Header names whose incoming claims are stripped at future trust boundaries
# (case-insensitive match; includes both our names and plausible spoof casing).
_PROVENANCE_HEADER_NAMES: Final[frozenset[str]] = frozenset(
    (*PROVENANCE_HEADERS, PROVENANCE_BUDGET_HEADER, PROVENANCE_REASON_HEADER)
)

# Public label charset: lowercase token, conservative charset, short. Rejects
# ':', '/', '\\', quotes, spaces, '=' and control characters — therefore no CRLF/
# header injection and no slash-paths by construction. It does NOT (and cannot)
# guarantee absence of secrets, account metadata or '..' tokens: that is the
# caller's duty (operator-configured public label only).
_BUDGET_LABEL_RE: Final = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,62}\Z")


class RefusalReason(StrEnum):
    """Finite allowlist of local refusal reasons (meter taxonomy minus ``ok``)."""

    EXHAUSTED = "exhausted"
    STALE = "stale"
    UNKNOWN = "unknown"
    UNBOUND = "unbound"


_MESSAGES: Final[Mapping[RefusalReason, str]] = {
    RefusalReason.EXHAUSTED: "local policy: prospective admission refused; budget is exhausted",
    RefusalReason.STALE: "local policy: prospective admission refused; budget evidence is stale",
    RefusalReason.UNKNOWN: (
        "local policy: prospective admission refused; budget evidence is unknown"
    ),
    RefusalReason.UNBOUND: (
        "local policy: prospective admission refused; no bound budget for this seat"
    ),
}


@dataclass(frozen=True)
class ProspectiveRefusal:
    """Immutable typed LOCAL refusal payload.

    Construction validates everything once; afterwards the object is frozen and
    its ``message``/fields contain no injectable characters (allowlisted reason,
    charset/length-bounded label), so a future adapter can pass them to
    ``_emit_sse_error_terminal`` without further escaping — the adapter itself is
    still required (see the plan).
    """

    reason: RefusalReason
    retry_after_s: int
    budget_label: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, RefusalReason):
            raise ValueError(f"reason must be one of {[r.value for r in RefusalReason]}")
        if isinstance(self.retry_after_s, bool) or not isinstance(self.retry_after_s, int):
            raise TypeError("retry_after_s must be int")
        if not RETRY_AFTER_MIN_S <= self.retry_after_s <= RETRY_AFTER_MAX_S:
            raise ValueError(
                f"retry_after_s must be within {RETRY_AFTER_MIN_S}..{RETRY_AFTER_MAX_S}"
            )
        if not isinstance(self.budget_label, str) or not _BUDGET_LABEL_RE.fullmatch(
            self.budget_label
        ):
            raise ValueError(
                "budget_label must match [a-z0-9][a-z0-9._-]{0,62} (operator-configured "
                "public label; charset/length bounded only — semantic purity is the "
                "caller's responsibility)"
            )

    @property
    def message(self) -> str:
        """Locally minted message; embeds only allowlisted reason + validated label."""

        return f"{_MESSAGES[self.reason]} ({self.budget_label})"

    def error_payload(self) -> dict[str, object]:
        """Anthropic-protocol error envelope (payload only).

        The actual SSE terminal helper,
        ``proxy._emit_sse_error_terminal(sse_resp, message, error_type)``, does
        NOT accept this dict: it takes ``(sse_resp, message, error_type)``.
        Wiring needs an ADAPTER mapping ``message=self.message`` and
        ``error_type=ERROR_TYPE`` into that call; the structured extras
        (reason/budget/retry_after_s) travel only on the JSON 503 path unless a
        future boundary extends the helper. No compatibility is claimed here.
        """

        return {
            "type": "error",
            "error": {
                "type": ERROR_TYPE,
                "message": self.message,
                "reason": self.reason.value,
                "budget": self.budget_label,
                "retry_after_s": self.retry_after_s,
            },
        }

    def response_headers(self) -> dict[str, str]:
        """503 headers: local provenance + bounded Retry-After."""

        return {
            **dict(PROVENANCE_HEADERS),
            PROVENANCE_BUDGET_HEADER: self.budget_label,
            PROVENANCE_REASON_HEADER: self.reason.value,
            "retry-after": str(self.retry_after_s),
        }


def local_refusal_response(refusal: ProspectiveRefusal):
    """HTTP 503 factory for a validated local refusal (aiohttp, JSON envelope)."""

    from aiohttp import web  # local import: payload use does not require aiohttp

    return web.Response(
        status=503,
        headers=refusal.response_headers(),
        content_type="application/json",
        text=json.dumps(refusal.error_payload()),
    )


def strip_incoming_provenance(headers: Mapping[str, str]) -> dict[str, str]:
    """Drop incoming claims of our provenance headers, case-insensitively.

    For FUTURE trust boundaries only (forwarded/proxied headers): a client must
    never be able to make its own refusal look local, or its upstream response
    look locally refused. **Not wired anywhere yet** — see the plan's
    "Remaining wiring". Everything else passes through untouched.
    """

    return {
        key: value for key, value in headers.items() if key.lower() not in _PROVENANCE_HEADER_NAMES
    }
