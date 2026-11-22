"""Pure offline metadata readiness. No allowance, capacity, or debit inference."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class State(str, Enum):
    READY = "READY"
    HOLD = "HOLD"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Bucket:
    account_id: str
    scope_id: str
    poll_time: float
    source_observed_at: float
    source_clock_type: str
    source_clock_provenance: str | None
    reset_epoch: float
    utilization_fraction: float | None
    duplicate_sample: bool | None
    producer_version: str | None
    source_timestamp_identity: str | None
    attested: bool
    source_evidence_id: str | None
    required: bool = True
    quantization: float | None = None
    clock_skew_bound_s: float | None = None
    restoration_attested: bool | None = None
    derived_display_projection: bool = False


def _finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _identity(value) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _result(state: State, reasons: list[str]) -> dict:
    result = {
        "state": state.value,
        "reasons": sorted(set(reasons)),
        "capacity": None,
        "cumulative_debit": None,
    }
    if state is State.READY:
        result["meaning"] = "metadata_ready_only; available headroom and debit remain unknown"
    return result


def readiness(
    *,
    now: float,
    buckets: list[Bucket],
    required_scopes: set[str] | frozenset[str] | list[str] | tuple[str, ...],
    max_source_age_s: float,
    max_poll_age_s: float,
    max_clock_skew_s: float,
) -> dict:
    """Validate a single account's observations under an explicit scope/age policy.

    Scope collections may be lists or tuples so repeated declarations can be
    rejected before conversion to a set. Source attestations require upstream
    authentication. READY certifies only this metadata gate.
    """
    reasons: list[str] = []
    if not _finite_number(now):
        reasons.append("invalid_now")
    thresholds = {
        "max_source_age_s": max_source_age_s,
        "max_poll_age_s": max_poll_age_s,
        "max_clock_skew_s": max_clock_skew_s,
    }
    for name, value in thresholds.items():
        if not _finite_number(value) or value < 0:
            reasons.append("invalid_policy:" + name)
    if not isinstance(required_scopes, (set, frozenset, list, tuple)):
        reasons.append("invalid_required_scopes")
    elif not required_scopes:
        reasons.append("empty_required_scopes")
    elif any(not _identity(scope) for scope in required_scopes):
        reasons.append("invalid_required_scope_identity")
    elif len(required_scopes) != len(set(required_scopes)):
        reasons.append("duplicate_required_scope")
    if not isinstance(buckets, (list, tuple)):
        reasons.append("invalid_buckets")
    if reasons:
        return _result(State.HOLD, reasons)
    if not buckets:
        return _result(State.UNKNOWN, ["no_observations"])

    # Validate every supplied identity and field type before indexing or arithmetic.
    seen: set[tuple[str, str]] = set()
    for index, bucket in enumerate(buckets):
        if not isinstance(bucket, Bucket):
            reasons.append(f"bucket_{index}:invalid_bucket_type")
            continue
        prefix = bucket.scope_id if _identity(bucket.scope_id) else f"bucket_{index}"
        if not _identity(bucket.account_id) or not _identity(bucket.scope_id):
            reasons.append(f"{prefix}:invalid_bucket_identity")
        else:
            identity = (bucket.account_id, bucket.scope_id)
            if identity in seen:
                reasons.append(f"{prefix}:duplicate_bucket_scope")
            seen.add(identity)
        for name in ("attested", "required", "derived_display_projection"):
            if type(getattr(bucket, name)) is not bool:
                reasons.append(f"{prefix}:invalid_boolean:{name}")
        for name in ("duplicate_sample", "restoration_attested"):
            value = getattr(bucket, name)
            if value is not None and type(value) is not bool:
                reasons.append(f"{prefix}:invalid_boolean:{name}")
        for name in ("poll_time", "source_observed_at", "reset_epoch"):
            if not _finite_number(getattr(bucket, name)):
                reasons.append(f"{prefix}:invalid_time:{name}")
        for name in (
            "source_clock_type",
            "source_clock_provenance",
            "producer_version",
            "source_timestamp_identity",
            "source_evidence_id",
        ):
            value = getattr(bucket, name)
            if value is not None and not _identity(value):
                reasons.append(f"{prefix}:invalid_identity:{name}")
        value = bucket.utilization_fraction
        if value is not None and (not _finite_number(value) or not 0 <= value <= 1):
            reasons.append(f"{prefix}:invalid_fraction")
        value = bucket.quantization
        if value is not None and (not _finite_number(value) or not 0 < value <= 1):
            reasons.append(f"{prefix}:invalid_quantization")
        value = bucket.clock_skew_bound_s
        if value is not None and (
            not _finite_number(value) or value < 0 or value > max_clock_skew_s
        ):
            reasons.append(f"{prefix}:clock_skew_bound_exceeded")
    if reasons:
        return _result(State.HOLD, reasons)

    if len({bucket.account_id for bucket in buckets}) != 1:
        return _result(State.HOLD, ["mixed_accounts"])
    required = set(required_scopes)
    by_scope = {bucket.scope_id: bucket for bucket in buckets}
    missing = sorted(required - set(by_scope))
    if missing:
        reasons.append("missing_required_buckets:" + ",".join(missing))
    for scope in sorted(required & set(by_scope)):
        bucket = by_scope[scope]
        if bucket.attested is not True or not bucket.source_evidence_id:
            reasons.append(f"{scope}:untrusted_attestation")
        if (
            bucket.source_clock_type != "local_proxy_wall_clock_at_header_application"
            or not bucket.source_clock_provenance
        ):
            reasons.append(f"{scope}:source_clock_provenance_unknown")
        if (
            bucket.poll_time > now + max_clock_skew_s
            or bucket.source_observed_at > bucket.poll_time + max_clock_skew_s
        ):
            reasons.append(f"{scope}:clock_skew")
        if now - bucket.poll_time > max_poll_age_s:
            reasons.append(f"{scope}:poll_expired")
        if now - bucket.source_observed_at > max_source_age_s:
            reasons.append(f"{scope}:source_age_expired")
        if bucket.duplicate_sample is None and (
            not bucket.producer_version or not bucket.source_timestamp_identity
        ):
            reasons.append(f"{scope}:duplicate_status_unknown")
        if bucket.derived_display_projection:
            reasons.append(f"{scope}:derived_display_projection_not_measurement")
        if bucket.utilization_fraction is None:
            reasons.append(f"{scope}:utilization_missing")
        if now >= bucket.reset_epoch:
            if bucket.source_observed_at < bucket.reset_epoch:
                reasons.append(f"{scope}:cross_reset_preboundary_observation")
            if bucket.restoration_attested is not True:
                reasons.append(f"{scope}:restoration_unverified")
            if bucket.duplicate_sample is True:
                reasons.append(f"{scope}:duplicate_cannot_establish_restoration")
    if reasons:
        return _result(State.HOLD, reasons)
    return _result(State.READY, [])
