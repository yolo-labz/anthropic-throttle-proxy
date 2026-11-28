"""Compare independently admitted W07 gateway marginals; never load raw telemetry."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from .observatory_evidence import Evidence

POPULATION = "gateway_requests_utc:all"
_WIDTH = {"minute": 60, "hour": 3600, "day": 86400}


def _count(value):
    return type(value) is int and value >= 0


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _span(start, end):
    return type(start) is type(end) is int and start < end


def _merge(spans):
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _summarize(side, cutoff, grain):
    lo, hi = side["window"]
    if not _span(lo, hi) or not all(_span(*span) for span in side["coverage"]):
        raise ValueError("invalid interval")
    coverage = _merge((max(lo, a), min(hi, b)) for a, b in side["coverage"] if a < hi and b > lo)
    width = _WIDTH[side["grain"]]
    cursor = None
    partial = bool(lo % grain or hi % grain)
    groups = {}
    for a, b, observed, known, value in sorted(side["buckets"], key=lambda row: row[0]):
        if (
            not _span(a, b)
            or a % width
            or b - a != width
            or (cursor is not None and cursor > a)
            or not (_count(observed) and _count(known) and known <= observed)
            or (value is not None if known == 0 else not _count(value))
        ):
            raise ValueError("invalid or overlapping marginal")
        cursor = b
        if b <= lo or a >= hi:
            continue
        if a < lo or b > hi or b > cutoff or not any(c <= a and b <= d for c, d in coverage):
            partial = True
            continue
        groups.setdefault(a - a % grain, []).append((a, b, observed, known, value))

    rows = []
    for start, group in groups.items():
        if _merge((a, b) for a, b, *_ in group) == [[start, start + grain]]:
            rows.extend(group)
        else:
            partial = True
    placed = _merge((a, b) for a, b, *_ in rows)
    n, k = (sum(row[i] for row in rows) for i in (2, 3))
    known_sum = sum(row[4] for row in rows if row[4] is not None) if k else None
    whole = coverage == placed == [[lo, hi]] and not partial and hi <= cutoff
    return dict(
        window=[lo, hi],
        coverage=coverage,
        placed=placed,
        observed_rows=n,
        known_rows=k,
        known_sum=known_sum,
        total=known_sum if whole and n == k else None,
    ), partial


def compare_windows(meta_left, meta_right, left, right):
    """Apply OBS-W19's whole-bucket rule; retain each side when a delta is withheld.

    This comparison math does not attest coverage or admit sources. W07 supplies one
    fixed grain; whole-bucket coalescing is retained for the frozen W19 contract.
    """
    grain = max(_WIDTH[left["grain"]], _WIDTH[right["grain"]])
    c0, c1 = meta_left["cutoff"], meta_right["cutoff"]
    if type(c0) is not int or type(c1) is not int:
        raise ValueError("invalid cutoff")
    a, partial_a = _summarize(left, c0, grain)
    b, partial_b = _summarize(right, c1, grain)
    reason = None
    if any(
        meta_left[k] != meta_right[k]
        for k in ("population", "generation", "filters", "measure", "unit")
    ):
        reason = "incompatible_identity"
    elif c0 != c1 or a["window"][1] > c0 or b["window"][1] > c1:
        reason = "incompatible_cutoff"
    elif partial_a or partial_b:
        reason = "unplaceable_partial_bucket"
    elif any(s["coverage"] != [s["window"]] or s["placed"] != [s["window"]] for s in (a, b)) or (
        a["window"][1] - a["window"][0] != b["window"][1] - b["window"][0]
    ):
        reason = "incompatible_coverage"
    elif a["total"] is None or b["total"] is None:
        reason = "unknown_measure"
    return dict(
        reason=reason,
        delta=None if reason else b["total"] - a["total"],
        grain_seconds=grain,
        left=a,
        right=b,
    )


class _Totals(TypedDict):
    observed_rows: int
    known_rows: int
    unknown_rows: int
    observed_sum: int | None
    complete_total: int | None


class _Interval(_Totals):
    start: int
    end: int


class W07Reconciliation(TypedDict):
    """Exact PASS_AGGREGATE_RECONCILIATION_ONLY fields from the W07 CLI."""

    status: Literal["PASS_AGGREGATE_RECONCILIATION_ONLY"]
    generation: str
    manifest_sha256: str
    source_age_seconds: int
    unit: Literal["minute", "hour", "day"]
    intervals: list[_Interval]
    union: _Totals


@dataclass(frozen=True, slots=True)
class Period:
    evidence: "Evidence"
    independently_verified_cutoff: datetime
    observed_rows: int
    known_rows: int
    observed_sum: int | None
    total: int | None

    @property
    def unknown_rows(self) -> int:
        return self.observed_rows - self.known_rows


@dataclass(frozen=True, slots=True)
class Comparison:
    status: Literal["unavailable", "partial", "comparable"] = "unavailable"
    reason: str = "No admitted pair with independently verified coverage."
    periods: tuple[Period, ...] = ()
    delta: int | None = None
    grain: str | None = None
    source_cutoff: datetime | None = None
    coverage_sha256: str | None = None
    member_sha256: str | None = None


def _evidence_matches(evidence, row, width, generation, coverage_sha256):
    source, window, scope, coverage = (
        evidence.source,
        evidence.window,
        evidence.scope,
        evidence.coverage,
    )
    return (
        source.generation == generation
        and _digest(source.loaded_sha256)
        and scope.population == POPULATION
        and scope.filters == ()
        and scope.identity_namespace is None
        and evidence.measure == "output_tokens"
        and evidence.unit == "tokens"
        and window.half_open is True
        and window.grain_seconds == width
        and window.start == datetime.fromtimestamp(row["start"], UTC)
        and window.end == datetime.fromtimestamp(row["end"], UTC)
        and coverage.state == "complete"
        and not coverage.gaps
        and not coverage.reasons
        and any(
            ref.loaded_sha256 == coverage_sha256
            and ref.generation == generation
            and ref.kind != source.kind
            for ref in coverage.refs
        )
    )


def _matches_totals(record, n, k, value, total):
    return all(
        type(record[key]) is type(expected) and record[key] == expected
        for key, expected in (
            ("observed_rows", n),
            ("known_rows", k),
            ("unknown_rows", n - k),
            ("observed_sum", value),
            ("complete_total", total),
        )
    )


def _w19_side(row: _Interval, grain: str):
    a, b = row["start"], row["end"]
    return dict(
        window=[a, b],
        grain=grain,
        coverage=[[a, b]],
        buckets=[(a, b, row["observed_rows"], row["known_rows"], row["observed_sum"])],
    )


def _checked_period(row: _Interval, ev: "Evidence", cutoff: int, summary) -> Period:
    period = Period(
        ev,
        datetime.fromtimestamp(cutoff, UTC),
        summary["observed_rows"],
        summary["known_rows"],
        summary["known_sum"],
        summary["total"],
    )
    if not _matches_totals(
        row, period.observed_rows, period.known_rows, period.observed_sum, period.total
    ):
        raise ValueError("The interval totals do not reconcile.")
    return period


def _union_matches(union: _Totals, periods: tuple[Period, Period]) -> bool:
    n = sum(p.observed_rows for p in periods)
    k = sum(p.known_rows for p in periods)
    value = sum(p.observed_sum for p in periods if p.observed_sum is not None) if k else None
    return bool(n) and _matches_totals(union, n, k, value, value if n == k else None)


def build_w07_comparison(
    reconciliation: W07Reconciliation | None,
    *,
    baseline: "Evidence | None" = None,
    current: "Evidence | None" = None,
    source_cutoff_epoch: int | None = None,
    verified_cutoffs_epoch: tuple[int, int] | None = None,
    aggregate_sha256: str | None = None,
    coverage_sha256: str | None = None,
    member_sha256: str | None = None,
) -> Comparison:
    """Render only the exact trusted P3 result and time-of-use N14 evidence.

    The authenticated caller must retain the P3 adapter's exact input pins, actual
    owner age and independent cutoff evidence. Never pass request JSON or synthesize
    coverage here. This projection rejects inconsistent bundles; it does not admit them.
    """
    if reconciliation is None or baseline is None or current is None:
        return Comparison()
    try:
        if (
            reconciliation["status"] != "PASS_AGGREGATE_RECONCILIATION_ONLY"
            or not _count(reconciliation["source_age_seconds"])
            or not _count(source_cutoff_epoch)
            or verified_cutoffs_epoch is None
            or not _digest(aggregate_sha256)
            or not _digest(coverage_sha256)
            or not _digest(member_sha256)
        ):
            return Comparison()
        cutoff_a, cutoff_b = verified_cutoffs_epoch
        if not all(_count(c) and c >= source_cutoff_epoch for c in (cutoff_a, cutoff_b)):
            return Comparison(reason="The independently verified cutoffs do not cover the pair.")
        source_cutoff = datetime.fromtimestamp(source_cutoff_epoch, UTC)
        grain = reconciliation["unit"]
        width = _WIDTH[grain]
        earlier, later = reconciliation["intervals"]
        generation = baseline.source.generation
        if (
            not all(_count(row[key]) for row in (earlier, later) for key in ("start", "end"))
            or earlier["start"] % width != 0
            or earlier["end"] - earlier["start"] != width
            or later["start"] != earlier["end"]
            or later["end"] - later["start"] != width
            or later["end"] > source_cutoff_epoch
            or generation.id != reconciliation["generation"]
            or generation.manifest_sha256 != reconciliation["manifest_sha256"]
            or not _digest(generation.manifest_sha256)
            or baseline.source != current.source
            or baseline.source.loaded_sha256 != aggregate_sha256
            or len(
                {
                    generation.manifest_sha256,
                    baseline.source.loaded_sha256,
                    coverage_sha256,
                    member_sha256,
                }
            )
            != 4
            or not _evidence_matches(baseline, earlier, width, generation, coverage_sha256)
            or not _evidence_matches(current, later, width, generation, coverage_sha256)
        ):
            return Comparison(reason="The interval, population or source evidence does not match.")

        meta = dict(
            population=POPULATION,
            generation=generation,
            filters=(),
            measure="output_tokens",
            unit="tokens",
            cutoff=source_cutoff_epoch,
        )
        w19 = compare_windows(meta, meta, _w19_side(earlier, grain), _w19_side(later, grain))
        periods = (
            _checked_period(earlier, baseline, cutoff_a, w19["left"]),
            _checked_period(later, current, cutoff_b, w19["right"]),
        )
        if not _union_matches(reconciliation["union"], periods):
            return Comparison(reason="The combined source total does not reconcile.")
    except (AttributeError, KeyError, OverflowError, OSError, TypeError, ValueError):
        return Comparison(reason="Invalid interval or source evidence.")

    delta = w19["delta"]
    return Comparison(
        status="comparable" if delta is not None else "partial",
        reason=""
        if delta is not None
        else "Both periods need a fully measured, nonempty output total.",
        periods=periods,
        delta=delta,
        grain=grain,
        source_cutoff=source_cutoff,
        coverage_sha256=coverage_sha256,
        member_sha256=member_sha256,
    )
