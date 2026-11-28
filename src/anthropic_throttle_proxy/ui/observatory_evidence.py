"""Evidence for new Observatory views, not a loader or an admission authority.

Pairing validates metadata, never joins records or produces a number. Keep the
measurement and its envelope together; don't display evidence as a link.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .. import usage_summary

SCHEMA = "thrviz.observatory-evidence.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CODE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,95}")
_LABEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/+-]{0,95}")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_IDENTITY_FILTERS = frozenset({"account", "pane", "replica", "target", "workspace"})
_SUMMARY_FILTERS = frozenset({"provider", "model", "role"})
_ABSENT = frozenset({"missing", "rejected"})


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("evidence window must have explicit time zones")
    return value.astimezone(UTC)


def _check_code(value: str) -> None:
    if not _CODE.fullmatch(value):
        raise ValueError("expected a content-free evidence code")


def _check_digest(value: str) -> None:
    if not _SHA256.fullmatch(value):
        raise ValueError("expected an exact SHA-256 digest")


@dataclass(frozen=True)
class Generation:
    authority: str
    id: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        _check_code(self.authority)
        _check_code(self.id)
        _check_digest(self.manifest_sha256)


@dataclass(frozen=True)
class SourceRef:
    kind: str
    loaded_sha256: str
    generation: Generation | None = None

    def __post_init__(self) -> None:
        _check_code(self.kind)
        _check_digest(self.loaded_sha256)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    grain_seconds: int | None = None
    half_open: bool = False

    def __post_init__(self) -> None:
        start, end = _utc(self.start), _utc(self.end)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        if end < start or (self.half_open and end == start):
            raise ValueError("invalid evidence window order")
        if self.grain_seconds is not None:
            if not self.half_open or type(self.grain_seconds) is not int or self.grain_seconds <= 0:
                raise ValueError("a grain requires a positive integer and half-open edges")
            grain = timedelta(seconds=self.grain_seconds)
            if (start - _EPOCH) % grain or (end - _EPOCH) % grain:
                raise ValueError("evidence window is not aligned to its UTC grain")


@dataclass(frozen=True)
class Scope:
    """Actual population and narrowing. No filters does not mean fleet-wide."""

    population: str
    filters: tuple[tuple[str, str], ...] = ()
    identity_namespace: str | None = None

    def __post_init__(self) -> None:
        _check_code(self.population)
        filters = tuple(sorted(self.filters))
        keys = {key for key, _ in filters}
        if len(keys) != len(filters) or keys - (_IDENTITY_FILTERS | _SUMMARY_FILTERS):
            raise ValueError("duplicate or unsupported evidence filters")
        if any(not _LABEL.fullmatch(value) for _, value in filters):
            raise ValueError("unsafe or empty evidence filter value")
        if self.identity_namespace is not None:
            _check_code(self.identity_namespace)
        elif keys & _IDENTITY_FILTERS:
            raise ValueError("identity filters require an admitted namespace")
        object.__setattr__(self, "filters", filters)


@dataclass(frozen=True)
class Coverage:
    state: str
    reasons: tuple[str, ...] = ()
    refs: tuple[SourceRef, ...] = ()
    gaps: tuple[Window, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in {"complete", "partial", "sampled", "unknown", *_ABSENT}:
            raise ValueError("invalid coverage state")
        for reason in self.reasons:
            _check_code(reason)
        if self.state == "complete":
            if not self.refs or self.gaps or self.reasons:
                raise ValueError("complete coverage requires independent proof without omissions")
        elif not self.reasons:
            raise ValueError("non-complete coverage requires an explicit reason")
        if self.gaps and not self.refs:
            raise ValueError("localized gaps require evidence")


@dataclass(frozen=True)
class Evidence:
    source: SourceRef | None
    window: Window | None
    scope: Scope | None
    measure: str | None
    unit: str | None
    coverage: Coverage

    def __post_init__(self) -> None:
        for code in (self.measure, self.unit):
            if code is not None:
                _check_code(code)
        if self.coverage.state == "complete" and (
            self.window is None
            or not self.window.half_open
            or self.scope is None
            or self.measure is None
            or self.unit is None
        ):
            raise ValueError("complete coverage needs explicit edges, scope, measure and unit")
        for ref in self.coverage.refs:
            if (
                self.source is None
                or self.source.generation is None
                or ref.generation != self.source.generation
                or ref.kind == self.source.kind
                or ref.loaded_sha256 == self.source.loaded_sha256
            ):
                raise ValueError("coverage proof must be independent and in the same generation")
        previous_end = None
        for gap in self.coverage.gaps:
            if (
                self.window is None
                or not self.window.half_open
                or not gap.half_open
                or not self.window.start <= gap.start < gap.end <= self.window.end
                or (previous_end is not None and gap.start < previous_end)
            ):
                raise ValueError("coverage gaps must be ordered non-overlapping UTC subranges")
            previous_end = gap.end


class SelectionRelation(StrEnum):
    MATCHED = "matched"
    INDEPENDENT_GLOBAL = "independent_global"
    UNSUPPORTED = "unsupported"


def selection_relation(evidence: Evidence, requested: Scope) -> SelectionRelation:
    """Keep global values as independent results, never as filtered totals."""
    actual = evidence.scope
    if evidence.source is None or actual is None or evidence.coverage.state in _ABSENT:
        return SelectionRelation.UNSUPPORTED
    if actual == requested:
        return SelectionRelation.MATCHED
    if not actual.filters:
        return SelectionRelation.INDEPENDENT_GLOBAL
    return SelectionRelation.UNSUPPORTED


@dataclass(frozen=True)
class EvidencePair:
    expected: Evidence
    observed: Evidence
    mismatches: tuple[str, ...]

    @property
    def compatible(self) -> bool:
        return not self.mismatches


def _source_mismatch(left: SourceRef | None, right: SourceRef | None) -> str | None:
    if left is None or right is None:
        return "missing_source"
    if left == right:
        return None
    if left.generation is None or left.generation != right.generation:
        return "generation_mismatch"
    if left.kind == right.kind:
        return "loaded_bytes_mismatch"
    if left.loaded_sha256 == right.loaded_sha256:
        return "source_kind_mismatch"
    return None


def pair_evidence(expected: Evidence, observed: Evidence) -> EvidencePair:
    """Only reconcile exact same-window measurements; retain both inputs on refusal."""
    mismatch = _source_mismatch(expected.source, observed.source)
    reasons = [mismatch] if mismatch is not None else []
    if expected.coverage.state in _ABSENT or observed.coverage.state in _ABSENT:
        reasons.append("source_unavailable")
    window = expected.window
    if window is None or window != observed.window:
        reasons.append("window_mismatch")
    elif not window.half_open and expected.source != observed.source:
        reasons.append("unknown_edge_semantics")
    if expected.scope is None or expected.scope != observed.scope:
        reasons.append("scope_mismatch")
    if expected.measure is None or expected.measure != observed.measure:
        reasons.append("measure_mismatch")
    if expected.unit is None or expected.unit != observed.unit:
        reasons.append("unit_mismatch")
    return EvidencePair(expected, observed, tuple(reasons))


def summary_evidence(
    summary: usage_summary.Summary,
    *,
    measure: str,
    filters: tuple[tuple[str, str], ...],
    unknown_values: int,
) -> Evidence:
    """Wrap the already-loaded summary and the actual validated projection.

    Omission counts are source-wide; do not attribute them to a selected group.
    A structurally accepted summary never proves independent interval coverage.
    """
    if measure not in {"tokens", "requests", *usage_summary.TOKEN_CLASSES}:
        raise ValueError("unsupported usage-summary measure")
    if any(key not in _SUMMARY_FILTERS for key, _ in filters):
        raise ValueError("unsupported usage-summary filter")
    if type(unknown_values) is not int or unknown_values < 0:
        raise ValueError("invalid usage-summary unknown count")
    omissions = []
    if summary.files_skipped:
        omissions.append("source_files_skipped")
    if summary.rejected_rows:
        omissions.append("source_rows_rejected")
    if unknown_values:
        omissions.append("selected_values_unknown")
    state = "partial" if omissions else "unknown"
    source = None
    if summary.loaded_sha256 is not None:
        source = SourceRef(usage_summary.SCHEMA, summary.loaded_sha256)
    else:
        omissions.append("missing_loaded_bytes")
    return Evidence(
        source,
        Window(summary.window_start, summary.window_end),
        Scope("summary_recorded_calls", filters),
        measure,
        "recorded_calls" if measure == "requests" else "tokens",
        Coverage(state, (*omissions, "independent_continuity_unverified")),
    )
