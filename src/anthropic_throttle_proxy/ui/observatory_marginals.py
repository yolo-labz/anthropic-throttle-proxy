"""Prepare exact DP08 usage marginals; no I/O, authorization, or coverage proof.

Use the unchanged bounds preflight and P3/W07 owner gates at their respective
boundaries. This function only checks immutable bytes and normalizes the DP08
input wire. A result here is not the admitted W07 envelope.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal

from .observatory_evidence import Coverage, Evidence, Generation, Scope, SourceRef, Window

_SCHEMA = "dp08-utc-marginals-v1"
_SCOPE = Scope("gateway_requests_utc:all")
# Frozen P3/W07 wire bounds; not owner-specific authorization limits.
_CAP = 65536
_MAX_COUNT = 2**127 - 1
_WIDTH = {"minute": 60, "hour": 3600, "day": 86400}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DIGEST = re.compile(r"[0-9a-f]{64}")
_GENERATION_ID = re.compile(r"[A-Za-z0-9_.-]{1,96}")
_FIELDS = frozenset(
    "schema exposure population generation manifest_sha256 export_member_sha256 "
    "generated_at_epoch unplaced_observed_rows marginals".split()
)
_BUCKET_FIELDS = frozenset(
    "bucket_unit bucket_start_utc observed_rows output_tokens_observed_rows "
    "output_tokens_observed_sum".split()
)
_NO_PROOF = "independent_continuity_unverified"


@dataclass(frozen=True)
class MarginalReason:
    code: str
    message: str


@dataclass(frozen=True)
class MarginalBucket:
    """Known/unknown row counts and the observed token sum, never a complete total."""

    evidence: Evidence
    observed_rows: int
    known_rows: int
    value: int | None

    @property
    def unknown_rows(self) -> int:
        return self.observed_rows - self.known_rows

    @property
    def status(self) -> Literal["observed", "unknown"]:
        return "unknown" if self.value is None else "observed"

    @property
    def reason(self) -> MarginalReason | None:
        if self.value is not None:
            return None
        return MarginalReason("no_known_output_tokens", "No output token counts were measured.")


@dataclass(frozen=True)
class PreparedMarginals:
    bucket_unit: str
    generated_at_epoch: int
    export_member_sha256: str
    unplaced_observed_rows: int
    buckets: tuple[MarginalBucket, MarginalBucket]


@dataclass(frozen=True)
class MarginalsResult:
    status: Literal["observed", "missing", "unknown", "rejected"]
    evidence: Evidence
    value: PreparedMarginals | None = None
    reason: MarginalReason | None = None


class _Reject(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.reason = MarginalReason(code, message)


def _unavailable(
    status: Literal["missing", "unknown", "rejected"], reason: MarginalReason
) -> MarginalsResult:
    return MarginalsResult(
        status,
        Evidence(None, None, None, None, None, Coverage(status, (reason.code,))),
        reason=reason,
    )


def _digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_COUNT


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate key")
    return obj


def _reject_number(_: str) -> None:
    raise ValueError("non-integer number")


def _decode(payload: bytes, payload_sha256: str) -> dict:
    if type(payload) is not bytes:
        raise _Reject("payload_type", "Expected immutable JSON bytes.")
    if not 0 < len(payload) <= _CAP:
        raise _Reject("payload_size", "The payload exceeds the wire bound or is empty.")
    if sha256(payload).hexdigest() != payload_sha256:
        raise _Reject("payload_pin_mismatch", "The supplied bytes do not match their pin.")
    try:
        doc = json.loads(
            payload,
            object_pairs_hook=_unique,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (ValueError, RecursionError) as exc:
        raise _Reject(
            "invalid_json", "Expected bounded JSON with unique keys and integers."
        ) from exc
    if not isinstance(doc, dict) or doc.keys() != _FIELDS or doc["schema"] != _SCHEMA:
        raise _Reject("wire_shape", "Expected a DP08 marginal document with the frozen fields.")
    return doc


def _windows(windows: tuple[Window, Window], width: int) -> tuple[Window, Window]:
    message = "Expected two adjacent, aligned half-open UTC buckets at the requested grain."
    if not isinstance(windows, tuple) or len(windows) != 2:
        raise _Reject("window_mismatch", message)
    step = timedelta(seconds=width)
    for window in windows:
        if (
            not isinstance(window, Window)
            or window.half_open is not True
            or window.grain_seconds not in (None, width)
            or window.start < _EPOCH
            or window.end - window.start != step
            or (window.start - _EPOCH) % step
        ):
            raise _Reject("window_mismatch", message)
    first, second = windows
    if first.end != second.start:
        raise _Reject("window_mismatch", message)
    return Window(first.start, first.end, width, True), Window(
        second.start, second.end, width, True
    )


def _bucket(row: object, window: Window, unit: str, source: SourceRef) -> MarginalBucket:
    if not isinstance(row, dict) or row.keys() != _BUCKET_FIELDS:
        raise _Reject("wire_shape", "Expected only the frozen marginal fields in each bucket.")
    start = window.start.replace(tzinfo=None).isoformat(timespec="seconds")
    if row["bucket_unit"] != unit or row["bucket_start_utc"] != start:
        raise _Reject("window_mismatch", "The bucket grain or UTC start does not match.")
    n, known, total = (
        row["observed_rows"],
        row["output_tokens_observed_rows"],
        row["output_tokens_observed_sum"],
    )
    if not (
        _count(n)
        and _count(known)
        and known <= n
        and ((known == 0 and total is None) or (known > 0 and _count(total)))
    ):
        raise _Reject("counter_mismatch", "Observed rows and token counts are inconsistent.")
    coverage = (
        Coverage("partial", ("output_token_values_unknown", _NO_PROOF))
        if n != known
        else Coverage("unknown", (_NO_PROOF,))
    )
    evidence = Evidence(source, window, _SCOPE, "output_tokens", "tokens", coverage)
    return MarginalBucket(evidence, n, known, total)


def prepare_marginals(
    payload: bytes | None,
    *,
    payload_sha256: str | None,
    generation: Generation | None,
    export_member_sha256: str | None,
    windows: tuple[Window, Window] | None,
    bucket_unit: str | None,
    requested_scope: Scope | None = None,
) -> MarginalsResult:
    """Bind a DP08 sidecar to caller-provided expectations, not to its own claims.

    Use the caller's independently bound generation, manifest/member and byte
    pins; this loader cannot authenticate them. ``observed`` means the supplied
    partial measurements were prepared, not independently covered or admitted.
    Nonzero unplaced rows remain visible here and still HOLD at the W07 gate.
    ``generated_at_epoch`` is the producer's generation time, not source age.
    """
    if payload is None:
        return _unavailable(
            "missing", MarginalReason("missing_payload", "No marginal bytes supplied.")
        )
    if any(
        x is None for x in (payload_sha256, generation, export_member_sha256, windows, bucket_unit)
    ):
        return _unavailable(
            "unknown",
            MarginalReason("missing_expectation", "Independent marginal expectations are absent."),
        )
    try:
        if requested_scope is not None and requested_scope != _SCOPE:
            raise _Reject(
                "scope_mismatch", "Only the label-free gateway request population is supported."
            )
        if not _digest(payload_sha256) or not _digest(export_member_sha256):
            raise _Reject("invalid_pin", "Expected independently supplied SHA-256 pins.")
        if not isinstance(generation, Generation) or not _GENERATION_ID.fullmatch(generation.id):
            raise _Reject("invalid_pin", "Expected a bound generation compatible with W07.")
        if not isinstance(bucket_unit, str) or bucket_unit not in _WIDTH:
            raise _Reject("unsupported_grain", "Expected a minute, hour or day grain.")
        width = _WIDTH[bucket_unit]
        first, second = _windows(windows, width)
        doc = _decode(payload, payload_sha256)
        if doc["exposure"] != "aggregate-only" or doc["population"] != _SCOPE.population:
            raise _Reject(
                "scope_mismatch", "Only the label-free gateway request population is supported."
            )
        if (doc["generation"], doc["manifest_sha256"], doc["export_member_sha256"]) != (
            generation.id,
            generation.manifest_sha256,
            export_member_sha256,
        ):
            raise _Reject("lineage_mismatch", "Generation, manifest or member pin does not match.")
        generated, unplaced = doc["generated_at_epoch"], doc["unplaced_observed_rows"]
        if not _count(generated) or not _count(unplaced):
            raise _Reject(
                "counter_mismatch", "The generation time or unplaced row count is invalid."
            )
        rows = doc["marginals"]
        if not isinstance(rows, list) or len(rows) != 2:
            raise _Reject("wire_shape", "Expected exactly two marginal buckets.")
        source = SourceRef(_SCHEMA, payload_sha256, generation)
        buckets = (
            _bucket(rows[0], first, bucket_unit, source),
            _bucket(rows[1], second, bucket_unit, source),
        )
        if not any(bucket.observed_rows for bucket in buckets):
            raise _Reject("empty_slice", "The pair contains no recorded rows.")
    except _Reject as exc:
        return _unavailable("rejected", exc.reason)
    omissions = []
    if unplaced:
        omissions.append("unplaced_observations")
    if any(bucket.unknown_rows for bucket in buckets):
        omissions.append("output_token_values_unknown")
    coverage = Coverage("partial" if omissions else "unknown", (*omissions, _NO_PROOF))
    evidence = Evidence(
        source, Window(first.start, second.end, width, True), _SCOPE, None, None, coverage
    )
    return MarginalsResult(
        "observed",
        evidence,
        PreparedMarginals(bucket_unit, generated, export_member_sha256, unplaced, buckets),
    )
