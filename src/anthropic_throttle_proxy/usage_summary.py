"""``usage-summary.v1`` — a small, content-free token-usage contract (spec 380).

An external producer (a session-log scanner, not this process) writes one JSON
document; the dashboard's Usage panel reads it from
``THROTTLE_USAGE_SUMMARY_PATH``. The gateway never computes these numbers, so
it never invents them: a ``null`` count is *unknown* and stays unknown — it is
not zero, and it is never folded into another class.

Content-free by construction: the validator accepts only the keys below, label
values must match ``_LABEL`` (no spaces, no ``@`` — so no prompt text, e-mail
address or other raw identity fits), and counts are non-negative integers. A
row with anything else is dropped and counted, never rendered.

Document shape::

    {
      "schema": "usage-summary.v1",
      "generated_at": "2026-11-08T23:40:00Z",
      "coverage": {"window_start": "...", "window_end": "...",
                   "files_scanned": 42, "files_skipped": 1},
      "rows": [{"provider": "anthropic", "model": "claude-opus-5-5",
                "pane": "p2V", "role": "turn", "requests": 12,
                "fresh_input": 1200, "cache_write": 30000, "cache_read": 900000,
                "output": 8000, "reasoning": null}]
    }

Times: ``coverage.window_end`` IS the source as-of time; ``generated_at`` is
only the file's write time. Freshness is measured from
``min(generated_at, window_end)``; a ``window_end`` later than ``generated_at``
or than now (beyond 60 s skew) is ``invalid-source-time``, never fresh.

``reasoning`` is a subset of ``output``: it is never summed with the other
classes, and a row whose known ``reasoning`` exceeds its known ``output`` is
rejected.

``role`` is ``turn`` (ordinary model calls) or ``compaction`` (the summariser
calls that rewrite a context). ``pane`` may be ``null`` when unattributed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCHEMA = "usage-summary.v1"
TOKEN_CLASSES = ("fresh_input", "cache_write", "cache_read", "output", "reasoning")
# ``reasoning`` is a SUBSET of ``output`` (an attribution, not an extra class):
# never add it to the others.
ADDITIVE_CLASSES = ("fresh_input", "cache_write", "cache_read", "output")
ROLES = ("turn", "compaction")
MAX_BYTES = 1 << 20  # ponytail: 1 MiB / 2000 rows; a producer this big should paginate (v2).
MAX_ROWS = 2000

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,95}$")
_ROW_KEYS = frozenset({"provider", "model", "pane", "role", "requests", *TOKEN_CLASSES})
_TOP_KEYS = frozenset({"schema", "generated_at", "coverage", "rows"})
_COVERAGE_KEYS = frozenset({"window_start", "window_end", "files_scanned", "files_skipped"})


class SummaryError(ValueError):
    """The document cannot be used at all. The message never quotes content."""


@dataclass(frozen=True)
class Row:
    provider: str
    model: str
    pane: str | None
    role: str
    requests: int | None
    counts: dict[str, int | None]  # every TOKEN_CLASSES key; None = unknown

    def value(self, key: str) -> int | None:
        """A token class count, or ``requests``."""
        return self.requests if key == "requests" else self.counts[key]


@dataclass(frozen=True)
class Summary:
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    files_scanned: int
    files_skipped: int
    rows: list[Row] = field(default_factory=list)
    rejected_rows: int = 0


def _ts(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise SummaryError(f"{name} is not a timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise SummaryError(f"{name} is not ISO-8601") from None
    if parsed.tzinfo is None:
        raise SummaryError(f"{name} has no UTC offset")
    return parsed


def _count(value: object) -> int | None:
    """A non-negative int, or None for unknown. Anything else raises."""
    if value is None:
        return None
    # bool is an int subclass; a float count (even 3.0) is a producer bug.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("count")
    return value


def _label(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _LABEL.match(value):
        raise ValueError("label")
    return value


def _row(raw: object) -> Row:
    if not isinstance(raw, dict) or set(raw) != _ROW_KEYS:
        raise ValueError("keys")
    if raw["role"] not in ROLES:
        raise ValueError("role")
    counts = {k: _count(raw[k]) for k in TOKEN_CLASSES}
    reasoning, output = counts["reasoning"], counts["output"]
    if reasoning is not None and output is not None and reasoning > output:
        raise ValueError("reasoning exceeds output")
    return Row(
        provider=_label(raw["provider"]) or "",
        model=_label(raw["model"]) or "",
        pane=_label(raw["pane"], nullable=True),
        role=raw["role"],
        requests=_count(raw["requests"]),
        counts=counts,
    )


def _files(value: object, name: str) -> int:
    try:
        n = _count(value)
    except ValueError:
        n = None
    if n is None:
        raise SummaryError(f"coverage.{name} is not a non-negative integer")
    return n


def parse(doc: object) -> Summary:
    """Validate one decoded document. Bad rows are dropped and counted."""
    if not isinstance(doc, dict):
        raise SummaryError("document is not an object")
    if doc.get("schema") != SCHEMA:
        raise SummaryError(f"schema is not {SCHEMA}")
    if set(doc) != _TOP_KEYS:
        raise SummaryError("unexpected or missing top-level keys")
    cov = doc["coverage"]
    if not isinstance(cov, dict) or set(cov) != _COVERAGE_KEYS:
        raise SummaryError("coverage keys do not match v1")
    rows_raw = doc["rows"]
    if not isinstance(rows_raw, list):
        raise SummaryError("rows is not a list")
    if len(rows_raw) > MAX_ROWS:
        raise SummaryError(f"more than {MAX_ROWS} rows")
    start = _ts(cov["window_start"], "coverage.window_start")
    end = _ts(cov["window_end"], "coverage.window_end")
    if end < start:
        raise SummaryError("coverage window ends before it starts")
    rows: list[Row] = []
    rejected = 0
    for raw in rows_raw:
        try:
            rows.append(_row(raw))
        except ValueError:
            rejected += 1
    return Summary(
        generated_at=_ts(doc["generated_at"], "generated_at"),
        window_start=start,
        window_end=end,
        files_scanned=_files(cov["files_scanned"], "files_scanned"),
        files_skipped=_files(cov["files_skipped"], "files_skipped"),
        rows=rows,
        rejected_rows=rejected,
    )


def _reject_constant(token: str) -> None:
    raise SummaryError(f"non-finite number {token}")


def load(path: Path) -> Summary:
    """Read + validate ``path``. Every failure is a ``SummaryError``."""
    try:
        with path.open("rb") as fh:
            blob = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        raise SummaryError(f"unreadable ({exc.__class__.__name__})") from None
    if len(blob) > MAX_BYTES:
        raise SummaryError(f"larger than {MAX_BYTES} bytes")
    try:
        doc = json.loads(blob, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SummaryError("not valid JSON") from None
    return parse(doc)


def total(rows: list[Row], key: str) -> tuple[int | None, int]:
    """Sum of the known values for ``key`` and how many rows were unknown.

    ``(None, n)`` when no row knows the value — unknown, not zero.
    """
    known = [r.value(key) for r in rows]
    values = [v for v in known if v is not None]
    return (sum(values) if values else None), len(known) - len(values)
