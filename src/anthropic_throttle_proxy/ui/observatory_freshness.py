"""Explicit freshness policy for source observations used by Observatory views.

All times are Unix UTC seconds. The caller supplies its source owner's age
limit and an evaluation time; this module reads no clock, file or environment.
A new publication or generation never replaces the source observation time.
Generation is carried as provenance, not authenticated here. Freshness alone
does not establish source authorization, completeness or identity coverage.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal


@dataclass(frozen=True, slots=True)
class Freshness:
    state: Literal["fresh", "stale", "unknown"]
    reason: str
    generation: str | None
    observed_at: float | None
    published_at: float | None
    evaluated_at: float | None
    max_age_s: float | None
    source_age_s: float | None
    publication_age_s: float | None


def _seconds(value: object) -> float | None:
    """Accept finite, non-negative seconds, including epoch zero, but not booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def classify_freshness(
    *,
    observed_at: float | None,
    published_at: float | None,
    now: float,
    max_age_s: float | None,
    generation: str | None = None,
) -> Freshness:
    """Classify source age using an explicitly supplied positive age limit.

    Age equal to the limit is stale. Missing/invalid evidence or policy is
    unknown, never fresh. Future times and observations after publication are
    inconsistent; there is no implicit clock-skew allowance. Known ages remain
    visible even when another required input is unavailable. Callers must pass
    actual source times, never page-load time or file mtime as a substitute.
    """
    observed = _seconds(observed_at)
    published = _seconds(published_at)
    reference = _seconds(now)
    limit = _seconds(max_age_s)
    source_age = (
        reference - observed
        if reference is not None and observed is not None and observed <= reference
        else None
    )
    publication_age = (
        reference - published
        if reference is not None and published is not None and published <= reference
        else None
    )
    result = Freshness(
        state="unknown",
        reason="invalid_reference_time",
        generation=generation,
        observed_at=observed,
        published_at=published,
        evaluated_at=reference,
        max_age_s=limit,
        source_age_s=source_age,
        publication_age_s=publication_age,
    )
    if reference is None:
        return result
    if observed is None:
        reason = "missing_observation_time" if observed_at is None else "invalid_observation_time"
        return replace(result, reason=reason)
    if published is None:
        reason = "missing_publication_time" if published_at is None else "invalid_publication_time"
        return replace(result, reason=reason)
    if observed > reference or published > reference:
        return replace(result, reason="source_time_in_future")
    if observed > published:
        return replace(result, reason="observation_after_publication")
    if limit is None or limit == 0:
        reason = "missing_age_policy" if max_age_s is None else "invalid_age_policy"
        return replace(result, reason=reason)
    if source_age is not None and source_age >= limit:
        return replace(result, state="stale", reason="age_limit_reached")
    return replace(result, state="fresh", reason="within_age_limit")
