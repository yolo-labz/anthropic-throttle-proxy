"""Pure exhaustion estimates for an already-admitted fixed quota window.

Admission, including restart/recalibration continuity and full-window account
mapping, belongs to the producer boundary. Callers must withhold history if
that evidence is missing. This consumer never accepts the current snapshot as
history and never reads a clock, policy, or provider data itself.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from itertools import pairwise
from typing import Literal, NotRequired, TypedDict

from .observatory_evidence import Evidence
from .observatory_freshness import Freshness, classify_freshness

# Computational bound, not a publisher retention or cadence policy. Never trim
# an overlong segment here: doing so could hide a discontinuity.
MAX_SAMPLES = 256
_MICROSECOND = timedelta(microseconds=1)


class StableAccount(TypedDict):
    kind: Literal["stable"]
    namespace: str
    account_key: str


class SourceLocalAccount(TypedDict):
    kind: Literal["source_local"]
    producer_scope: str
    native_key: str


class QuotaSnapshot(TypedDict):
    constraint_kind: Literal["subscription-window", "refill", "spend", "concurrency", "pacing"]
    native_meter_key: tuple[str, str, str | None] | None
    account_scope: StableAccount | SourceLocalAccount | None
    unit: str | None
    remaining: int | float | None
    used: int | float | None
    limit: int | float | None
    denominator: int | float | None
    observed_at: datetime | None
    observed_reset_at: datetime | None


class QuotaHistorySample(TypedDict):
    snapshot: QuotaSnapshot
    evidence: Evidence


class _Reason(TypedDict):
    code: str
    message: str


class QuotaHistoryResult(TypedDict):
    """Frozen quota_history member of QuotaConsumerInput, never its snapshot."""

    status: Literal["observed", "missing", "unknown", "rejected"]
    value: tuple[QuotaHistorySample, ...] | None
    evidence: Evidence
    reason: _Reason | None


class FreshnessInput(TypedDict):
    """N04 clocks and explicit age policy bound to this exact history source."""

    observed_at: float | None
    published_at: float | None
    now: float
    max_age_s: float | None
    generation: NotRequired[str | None]


@dataclass(frozen=True, slots=True)
class ForecastPolicy:
    """Caller-supplied fit span, continuity gap bound, and look-ahead cap, in seconds."""

    min_span_s: float
    max_gap_s: float
    max_horizon_s: float


@dataclass(frozen=True, slots=True)
class ExhaustionEstimate:
    method: str
    fit_observations: tuple[datetime, datetime]
    sample_count: int
    eta_after_observation_s: float
    uncertainty_after_observation_s: tuple[float, float]
    horizon_after_observation_s: float
    freshness: Freshness


@dataclass(frozen=True, slots=True)
class ExhaustionResult:
    state: Literal["unknown", "no_depletion", "exhausted_at_observation", "estimated"]
    reason: str
    observed_reset_at: datetime | None = None
    estimate: ExhaustionEstimate | None = None


def _nonnegative(value: object) -> bool:
    return (type(value) is int and value >= 0) or (
        type(value) is float and math.isfinite(value) and value >= 0
    )


def _positive_seconds(value: float) -> bool:
    return _nonnegative(value) and 0 < value <= sys.float_info.max


def _elapsed(start: datetime, end: datetime) -> Fraction:
    return Fraction((end - start) // _MICROSECOND, 1_000_000)


def _utc(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is UTC


def _covered(evidence: Evidence) -> bool:
    return (
        isinstance(evidence, Evidence)
        and evidence.source is not None
        and evidence.source.generation is not None
        and evidence.coverage.state == "complete"
    )


def _same_scope(sample: Evidence, history: Evidence) -> bool:
    return (
        _covered(sample)
        and sample.source == history.source
        and sample.scope == history.scope
        and sample.measure == history.measure
        and sample.unit == history.unit
    )


def _contains(evidence: Evidence, at: datetime) -> bool:
    return evidence.window is not None and evidence.window.start <= at < evidence.window.end


def _remaining_series(rows: list[QuotaSnapshot]) -> list[Fraction] | str:
    for row in rows:
        if any(
            value is not None and not _nonnegative(value)
            for value in (row["remaining"], row["used"], row["limit"], row["denominator"])
        ):
            return "invalid_quantity"
    limits = {row["limit"] for row in rows if row["limit"] is not None}
    if len(limits) > 1:
        return "limit_changed"
    if all(row["remaining"] is not None for row in rows):
        for row in rows:
            if row["limit"] is not None and row["remaining"] > row["limit"]:
                return "quantity_exceeds_limit"
        return [Fraction(row["remaining"]) for row in rows]
    if not all(row["used"] is not None and row["limit"] is not None for row in rows):
        return "amount_or_unit_unavailable"
    if any(row["used"] > row["limit"] for row in rows):
        return "quantity_exceeds_limit"
    remaining = [Fraction(row["limit"]) - Fraction(row["used"]) for row in rows]
    if any(
        row["remaining"] is not None and Fraction(row["remaining"]) != value
        for row, value in zip(rows, remaining, strict=True)
    ):
        return "inconsistent_quota_quantities"
    return remaining


def estimate_exhaustion(
    history: QuotaHistoryResult | None,
    *,
    freshness: FreshnessInput | None,
    policy: ForecastPolicy | None,
) -> ExhaustionResult:
    """Consume one continuous segment with at least three observed samples.

    Use one uniform basis (reported remaining, or used and directly attested
    limit at every reading). Fit elapsed-mean burn and use the extrema of
    positive adjacent burn rates for a sensitivity range, not a confidence
    interval. Offsets are from the last actual observation, not page load.
    All plausible ETAs must still be ahead of evaluation time and before the
    observed reset, and the entire range must fit in the explicit horizon.
    N14 coverage must be explicitly scoped to the stable account. A global
    checkpoint cannot qualify one account’s history. Non-windowed and
    refilling meters have no supported depletion model here.
    """
    if history is None:
        return ExhaustionResult("unknown", "quota_history_not_supplied")
    if history["status"] != "observed" or history["reason"] is not None:
        cause = history["reason"]
        return ExhaustionResult("unknown", cause["code"] if cause else "history_not_admitted")
    samples = history["value"]
    evidence = history["evidence"]
    if not isinstance(samples, tuple) or not samples or not _covered(evidence):
        return ExhaustionResult("unknown", "history_continuity_unknown")
    if len(samples) > MAX_SAMPLES:
        return ExhaustionResult("unknown", "history_exceeds_sample_bound")
    if policy is None or not all(
        _positive_seconds(value)
        for value in (policy.min_span_s, policy.max_gap_s, policy.max_horizon_s)
    ):
        return ExhaustionResult("unknown", "missing_or_invalid_forecast_policy")

    rows = [item["snapshot"] for item in samples]
    first = rows[0]
    account, meter, unit = first["account_scope"], first["native_meter_key"], first["unit"]
    if (
        account is None
        or account["kind"] != "stable"
        or not account["namespace"]
        or not account["account_key"]
        or evidence.scope is None
        or evidence.scope.identity_namespace != account["namespace"]
        or dict(evidence.scope.filters).get("account") != account["account_key"]
    ):
        return ExhaustionResult("unknown", "account_scope_unbound")
    if first["constraint_kind"] != "subscription-window":
        return ExhaustionResult("unknown", "unsupported_constraint_kind")
    if (
        type(meter) is not tuple
        or len(meter) != 3
        or not all(type(key) is str and key for key in meter)
    ):
        return ExhaustionResult("unknown", "native_meter_or_window_unbound")
    if not unit or unit != evidence.unit:
        return ExhaustionResult("unknown", "incompatible_unit")

    reset = first["observed_reset_at"]
    if reset is not None and not _utc(reset):
        return ExhaustionResult("unknown", "invalid_reset_time")
    times: list[datetime] = []
    for row, item in zip(rows, samples, strict=True):
        if row["account_scope"] != account:
            return ExhaustionResult("unknown", "identity_kind_or_scope_mismatch")
        if row["native_meter_key"] != meter or row["constraint_kind"] != first["constraint_kind"]:
            return ExhaustionResult("unknown", "constraint_kind_or_meter_mismatch")
        if row["unit"] != unit:
            return ExhaustionResult("unknown", "incompatible_unit")
        if not _same_scope(item["evidence"], evidence):
            return ExhaustionResult("unknown", "sample_evidence_mismatch")
        at = row["observed_at"]
        if not _utc(at) or not _contains(evidence, at) or not _contains(item["evidence"], at):
            return ExhaustionResult("unknown", "invalid_or_uncovered_observation_time")
        row_reset = row["observed_reset_at"]
        if row_reset is not None and not _utc(row_reset):
            return ExhaustionResult("unknown", "invalid_reset_time")
        if row_reset != reset or (reset is not None and reset <= at):
            return ExhaustionResult("unknown", "reset_boundary")
        times.append(at)

    def unknown(reason: str) -> ExhaustionResult:
        return ExhaustionResult("unknown", reason, observed_reset_at=reset)

    steps = [_elapsed(left, right) for left, right in pairwise(times)]
    if any(step <= 0 for step in steps):
        return unknown("non_monotone_observation_times")
    if any(step > policy.max_gap_s for step in steps):
        return unknown("observation_gap")
    span = _elapsed(times[0], times[-1])
    if len(times) < 3 or span < policy.min_span_s:
        return unknown("insufficient_observations")
    if freshness is None:
        return unknown("freshness_input_not_supplied")
    generation = evidence.source.generation.id
    age = classify_freshness(**freshness)
    if age.state == "unknown":
        return unknown(age.reason)
    if age.observed_at != times[-1].timestamp() or age.generation not in {None, generation}:
        return unknown("freshness_history_mismatch")
    if age.state == "stale":
        return unknown("stale_history")
    source_age = age.source_age_s
    assert source_age is not None  # PR130 fresh implies a valid observed and evaluation clock.
    if reset is not None and _elapsed(times[-1], reset) <= source_age:
        return unknown("reset_boundary")

    remaining = _remaining_series(rows)
    if isinstance(remaining, str):
        return unknown(remaining)
    burns = [left - right for left, right in pairwise(remaining)]
    if any(burn < 0 for burn in burns):
        return unknown("remaining_increased_or_recalibrated")
    if remaining[-1] == 0:
        return ExhaustionResult("exhausted_at_observation", "observed_zero_remaining", reset)
    if not any(burns):
        return ExhaustionResult("no_depletion", "flat_observed_series", reset)
    if not all(burns):
        return unknown("positive_rate_not_sustained")
    if reset is None:
        return unknown("observed_reset_not_supplied")

    rates = [burn / step for burn, step in zip(burns, steps, strict=True)]
    left = remaining[-1]
    earliest, latest = left / max(rates), left / min(rates)
    if earliest <= source_age:
        return unknown("projected_range_already_reached")
    if earliest < Fraction(1, 1_000_000):
        return unknown("projected_range_below_clock_resolution")
    if latest >= _elapsed(times[-1], reset):
        return unknown("projected_range_reaches_reset")
    if latest > policy.max_horizon_s:
        return unknown("projected_range_exceeds_horizon")
    eta = left * span / sum(burns)
    return ExhaustionResult(
        "estimated",
        "bounded_estimate",
        reset,
        ExhaustionEstimate(
            method="elapsed_mean_with_adjacent_rate_envelope",
            fit_observations=(times[0], times[-1]),
            sample_count=len(times),
            eta_after_observation_s=float(eta),
            uncertainty_after_observation_s=(float(earliest), float(latest)),
            horizon_after_observation_s=float(policy.max_horizon_s),
            freshness=age,
        ),
    )
