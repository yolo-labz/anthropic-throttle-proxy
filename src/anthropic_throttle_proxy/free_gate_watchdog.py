"""Per-bearer watchdog for the OpenCode free gates (``:8770`` go, ``:8774`` zen).

Why this exists
---------------
On 19/10/2026 18:46-19:06 the shared go-gate bearer ``e574f38a`` was pushed
back by OpenCode's free-tier budget limiter, AIMD shrank its ceiling
``12 -> 8 -> 5 -> 3 -> 2 -> 1`` in seven minutes, and then the ceiling *rested*
at 1. With generations taking 171-303 s, the drain estimate (210-355 s) blew
past the clients' 180 s wait budget, so every other request was refused with a
pre-queue-depth 503. Ten ``queue-wait-timeout`` events and eight pre-queue
refusals later, pi panes were dying in retry loops.

Nothing alerted. ``anthropic_aimd_max_concurrent`` read ``1`` — a value that is
*equally correct* for a cold, idle bearer, so it is not by itself an alertable
signal. This module supplies the three missing signals:

===============  ============================================================  ==========
rule             condition                                                     severity
===============  ============================================================  ==========
``pinned``       ``aimd_max_concurrent <= aimd_floor`` sustained >= dwell       critical
``pushback``     budget-pushback events within the window >= threshold         warning
``qtimeout``     queue-wait-timeout events/min >= threshold                    critical
===============  ============================================================  ==========

``pushback`` is the *early* rule: on the replay it fires at 18:51:44, 10.7 min
before the first client-visible 503. ``pinned`` with a 10-minute dwell fires at
19:03:48, ~84 s AFTER the 503 storm began — the dwell is deliberate (a ceiling
at the floor is normal on an idle lane) and the honest consequence is that the
pin rule is a *confirmation*, not the alarm. ``qtimeout`` is the "users are
already burning" rule.

Two input paths, one rule engine
--------------------------------
``--live`` scrapes ``/metrics`` from each gate. ``--replay`` folds a
``journalctl`` capture back into the *same* metric series the gate would have
exposed (counters rebuilt from ``aimd-shrink`` / ``queue-wait-timeout`` /
``done status=`` log lines, gauges from ``bearer-new`` / ``aimd-*``), then runs
the identical rule functions. The replay is therefore a real replay, not a
parallel code path that could drift from production.

Counter semantics worth knowing before you tune a threshold
-----------------------------------------------------------
``anthropic_aimd_shrinks_total`` counts *budget-class* pushback only: a
headerless 429 with the unified windows still ``allowed`` is classified
``concurrency-429`` and deliberately does NOT shrink (17/07/2026 regression —
see ``proxy._aimd_apply_pushback``). So a sustained shrink rate is a genuine
budget signal, and a burst of concurrency-429s will not trip this rule.

Usage
-----
    python scripts/free-gate-watchdog.py --live
    python scripts/free-gate-watchdog.py --replay /tmp/go-gate-incident.log --floor 1
    python scripts/free-gate-watchdog.py --live --json --interval 15 --samples 8
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "Alert",
    "Reconstructor",
    "Sample",
    "Scrape",
    "UnreachableGate",
    "WatchdogConfig",
    "evaluate",
    "fetch_metrics",
    "parse_journal_line",
    "parse_prometheus",
    "replay_journal",
    "scrapes_from_journal",
]

# ---------------------------------------------------------------------------
# Metric names (must match metrics.py)
# ---------------------------------------------------------------------------
M_AIMD_MAX = "anthropic_aimd_max_concurrent"
M_AIMD_FLOOR = "anthropic_aimd_floor"
M_AIMD_SHRINKS = "anthropic_aimd_shrinks_total"
M_QUEUE_WAIT_TIMEOUTS = "anthropic_queue_wait_timeouts_total"
M_REQUESTS = "anthropic_requests_total"
M_INFLIGHT = "anthropic_inflight"
M_QUEUED = "anthropic_queued"

_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?"
    r"\s+(?P<value>[^\s]+)\s*$"
)
_LABEL_RE = re.compile(r'([a-zA-Z_]\w*)="((?:[^"\\]|\\.)*)"')
_JOURNAL_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z))"
)
_FIELD_RE = re.compile(r"([a-zA-Z_]\w*)=(\S+)")
#: `[anthropic-throttle] <kind> ...` — the proxy's structured log envelope.
#:
#: ReDoS-safety (Sonar hotspot): every quantifier over a repeated group is
#: POSSESSIVE (``\s++``, ``[a-z0-9-]*+``, ``\s*+``), so the engine consumes
#: them once and can never backtrack into them — the search for ``]`` advances
#: monotonically and each attempt runs in linear time in the line length. The
#: only non-possessive quantifier is the terminal ``.*`` before the ``$``
#: anchor, which cannot backtrack productively. The possessive rewrite is
#: semantics-preserving here: ``\s`` and ``[a-z]`` are disjoint (giving back
#: whitespace can never let ``[a-z]`` match), and ``[a-z0-9-]`` and ``\s``
#: are disjoint the same way. Requires Python >=3.11; the repo pins >=3.13.
_ENVELOPE_RE = re.compile(r"\]\s++(?P<kind>[a-z][a-z0-9-]*+)\s*+(?P<rest>.*)$")


# ---------------------------------------------------------------------------
# Scrape model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Sample:
    """One metric sample: name + label set + value."""

    name: str
    labels: tuple[tuple[str, str], ...]
    value: float

    def label(self, key: str) -> str | None:
        for k, v in self.labels:
            if k == key:
                return v
        return None

    @property
    def key(self) -> tuple[str, tuple[tuple[str, str], ...]]:
        return (self.name, self.labels)


@dataclass
class Scrape:
    """All samples observed at one instant, plus where they came from."""

    ts: float
    samples: list[Sample] = field(default_factory=list)
    source: str = ""

    def series(self, name: str) -> list[Sample]:
        return [s for s in self.samples if s.name == name]

    def value(self, name: str, **labels: str) -> float | None:
        """Sum ``name`` over samples whose labels match every kwarg.

        Summing (rather than requiring one sample) lets a caller ask for
        ``value(M_REQUESTS, status="429")`` without naming the model label.
        Returns ``None`` when nothing matches — absent is NOT zero, and the
        rules treat the two differently.
        """
        total = 0.0
        found = False
        for s in self.samples:
            if s.name != name:
                continue
            if any(s.label(k) != v for k, v in labels.items()):
                continue
            total += s.value
            found = True
        return total if found else None

    def bearers(self, name: str) -> dict[str, float]:
        """``{bearer: value}`` for a per-bearer series (later samples win)."""
        out: dict[str, float] = {}
        for s in self.samples:
            if s.name != name:
                continue
            bid = s.label("bearer")
            if bid is not None:
                out[bid] = s.value
        return out


def _parse_labels(raw_labels: str | None) -> tuple[tuple[str, str], ...]:
    """Unescape one ``{...}`` label block into a sorted tuple."""
    if not raw_labels:
        return ()
    return tuple(
        sorted(
            (lk, lv.replace('\\"', '"').replace("\\\\", "\\"))
            for lk, lv in _LABEL_RE.findall(raw_labels)
        )
    )


def _parse_sample_line(line: str) -> Sample | None:
    """Parse one exposition line, or ``None`` when it carries no signal.

    Comments, blanks, ``_created`` bookkeeping series and non-finite values
    are dropped here: a ``NaN`` ceiling would silently satisfy ``<= floor``
    in the wrong direction, and ``_created`` is an artefact of the client
    library, not a signal.
    """
    m = _SAMPLE_RE.match(line)
    if not m:
        return None
    name = m.group("name")
    if name.endswith("_created"):
        return None
    try:
        value = float(m.group("value"))
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return Sample(name=name, labels=_parse_labels(m.group("labels")), value=value)


def parse_prometheus(text: str, *, ts: float | None = None, source: str = "") -> Scrape:
    """Parse Prometheus text exposition into a :class:`Scrape`.

    ``#`` comments and blank lines never reach :func:`_parse_sample_line`;
    everything else that carries no signal is dropped there.
    """
    scrape = Scrape(ts=time.time() if ts is None else ts, source=source)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sample = _parse_sample_line(line)
        if sample is not None:
            scrape.samples.append(sample)
    return scrape


def fetch_metrics(endpoint: str, *, timeout: float = 5.0, source: str = "") -> Scrape:
    """Scrape ``<endpoint>/metrics``.

    ``endpoint`` is an operator-supplied local URL (``http://127.0.0.1:8770``);
    no user input reaches this call, hence the S310 suppression.
    """
    url = endpoint.rstrip("/") + "/metrics"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return parse_prometheus(body, source=source or endpoint)


# ---------------------------------------------------------------------------
# Journal parsing / replay
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class JournalEvent:
    """One structured proxy log line."""

    ts: float
    kind: str
    fields: dict[str, str]
    raw: str


def _parse_ts(stamp: str) -> float | None:
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def parse_journal_line(line: str) -> JournalEvent | None:
    """Parse a ``journalctl -o short-iso`` line into a :class:`JournalEvent`.

    Returns ``None`` for anything that is not a structured throttle line, so a
    capture can be fed in whole without pre-filtering.
    """
    tsm = _JOURNAL_TS_RE.match(line)
    if not tsm:
        return None
    ts = _parse_ts(tsm.group(1))
    if ts is None:
        return None
    env = _ENVELOPE_RE.search(line)
    if not env:
        return None
    kind = env.group("kind")
    fields = dict(_FIELD_RE.findall(env.group("rest")))
    return JournalEvent(ts=ts, kind=kind, fields=fields, raw=line.rstrip("\n"))


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


class Reconstructor:
    """Fold a journal capture into the metric series the gate would expose.

    Only the series the rules actually read are rebuilt. Everything is
    cumulative-state + emit-a-scrape-per-event, so the resulting list is a
    faithful, monotonically-timestamped time series rather than a re-log.
    """

    def __init__(self, *, floor: float = 1.0, source: str = "replay") -> None:
        self.floor = floor
        self.source = source
        self._shrinks: dict[tuple[str, str], float] = {}
        self._timeouts: dict[str, float] = {}
        self._requests: dict[str, float] = {}
        self._ceiling: dict[str, float] = {}
        self._inflight = 0.0
        self._queued = 0.0
        self._scrapes: list[Scrape] = []
        #: replay-only counters that have no production metric (reporting only)
        self.extra: dict[str, float] = {"queue-depth-reject": 0.0, "rate-pushback-retry": 0.0}

    # -- state -> samples ---------------------------------------------------
    def _emit(self, ts: float) -> None:
        samples: list[Sample] = []
        for (bid, status), value in sorted(self._shrinks.items()):
            samples.append(Sample(M_AIMD_SHRINKS, (("bearer", bid), ("status", status)), value))
        for bid, value in sorted(self._timeouts.items()):
            samples.append(Sample(M_QUEUE_WAIT_TIMEOUTS, (("bearer", bid),), value))
        for status, value in sorted(self._requests.items()):
            samples.append(Sample(M_REQUESTS, (("status", status),), value))
        for bid, value in sorted(self._ceiling.items()):
            samples.append(Sample(M_AIMD_MAX, (("bearer", bid),), value))
            samples.append(Sample(M_AIMD_FLOOR, (("bearer", bid),), self.floor))
        samples.append(Sample(M_INFLIGHT, (), self._inflight))
        samples.append(Sample(M_QUEUED, (), self._queued))
        self._scrapes.append(Scrape(ts=ts, samples=samples, source=self.source))

    # -- journal -> state ---------------------------------------------------
    def feed(self, line: str) -> None:
        event = parse_journal_line(line)
        if event is None:
            return
        self.feed_event(event)

    #: ``bearer-new`` and ``aimd-grow`` (and the ``max_concurrent`` carried by
    #: ``aimd-shrink``) all mean the same thing to the replay: the lane's
    #: ceiling is now this value. One recorder for all three.
    def _record_ceiling(self, f: dict[str, str], bid: str | None) -> bool:
        ceiling = _as_int(f.get("max_concurrent"))
        if not bid or ceiling is None:
            return False
        self._ceiling[bid] = float(ceiling)
        return True

    def _record_shrink(self, f: dict[str, str], bid: str | None) -> bool:
        if not bid:
            return False
        # The ceiling rides along on the shrink line, same as the others.
        self._record_ceiling(f, bid)
        status = f.get("status", "unknown")
        key = (bid, status)
        self._shrinks[key] = self._shrinks.get(key, 0.0) + 1.0
        return True

    def _record_timeout(self, f: dict[str, str], bid: str | None) -> bool:
        if not bid:
            return False
        self._timeouts[bid] = self._timeouts.get(bid, 0.0) + 1.0
        return True

    def _record_pushback_retry(self, f: dict[str, str], bid: str | None) -> bool:
        del f, bid
        self.extra["rate-pushback-retry"] += 1.0
        return False

    def _record_restart(self, f: dict[str, str], bid: str | None) -> bool:
        """A fresh process starts every counter at zero.

        The 18:48:59 restart is inside the incident; without this the
        pre-restart shrinks would be counted as if the new process had seen
        them, inflating the replay's pushback rate.
        """
        del f, bid
        self._shrinks.clear()
        self._timeouts.clear()
        self._requests.clear()
        return True

    def _record_depth_reject(self, f: dict[str, str], bid: str | None) -> bool:
        del f, bid
        self.extra["queue-depth-reject"] += 1.0
        return False

    def _record_done(self, f: dict[str, str], bid: str | None) -> bool:
        del bid
        status = f.get("status")
        if status is not None:
            self._requests[status] = self._requests.get(status, 0.0) + 1.0
        return True

    #: kind -> state recorder. Kinds absent here are pure noise for the replay
    #: (e.g. ``queue-first-waiter`` is the accept twin of ``queue-depth-reject``
    #: and moves no counter).
    _RECORDERS: dict[str, Callable[[dict[str, str], str | None], bool]] = {
        "bearer-new": _record_ceiling,
        "aimd-grow": _record_ceiling,
        "aimd-shrink": _record_shrink,
        "queue-wait-timeout": _record_timeout,
        "rate-pushback-retry": _record_pushback_retry,
        "listening": _record_restart,
        "queue-depth-reject": _record_depth_reject,
        "done": _record_done,
    }

    def feed_event(self, event: JournalEvent) -> None:
        kind, f = event.kind, event.fields
        bid = f.get("bid")
        recorder = self._RECORDERS.get(kind)
        changed = recorder(self, f, bid) if recorder is not None else False
        # inflight/queued gauges ride along on every line that carries them.
        for attr, name in (("_inflight", "inflight"), ("_queued", "queued")):
            value = _as_int(f.get(name))
            if value is not None:
                setattr(self, attr, float(value))
                changed = True
        if changed:
            self._emit(event.ts)

    def finish(self, ts: float | None = None) -> None:
        """Emit a closing scrape so an open run's duration is measurable."""
        if not self._scrapes:
            return
        end = ts if ts is not None else self._scrapes[-1].ts
        if end <= self._scrapes[-1].ts:
            return
        self._emit(end)

    @property
    def scrapes(self) -> list[Scrape]:
        return list(self._scrapes)


def scrapes_from_journal(
    lines: Iterable[str], *, floor: float = 1.0, source: str = "replay"
) -> tuple[list[Scrape], Reconstructor]:
    """Rebuild a scrape stream from raw journal lines."""
    recon = Reconstructor(floor=floor, source=source)
    for line in lines:
        recon.feed(line)
    recon.finish()
    return recon.scrapes, recon


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
@dataclass
class UnreachableGate:
    """One gate's scrape failures over a live run — per-endpoint failure state.

    This is the counter the stderr warning used to be the only trace of. A gate
    the watchdog cannot scrape is invisible to every OTHER rule (no scrapes, no
    series), so the blindness itself must be carried into :func:`evaluate` and
    surfaced as a finding: a gate that failed EVERY scrape becomes a critical
    ``unreachable`` Alert (the watchdog is blind for the whole run), while a
    partially-failing gate becomes a ``Pending`` near-miss so one transient
    timeout does not page anyone.
    """

    source: str
    endpoint: str
    attempts: int = 0
    failures: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0
    last_error: str = ""

    @property
    def blind(self) -> bool:
        """True when NO scrape of this gate landed (at least one was attempted)."""
        return self.attempts > 0 and self.failures >= self.attempts

    def note_failure(self, exc: BaseException) -> None:
        """Record one failed scrape: count, when it happened, why it failed."""
        now = time.time()
        if self.failures == 0:
            self.first_ts = now
        self.failures += 1
        self.last_ts = now
        self.last_error = f"{type(exc).__name__}: {exc}"


@dataclass(frozen=True)
class Alert:
    rule: str
    severity: str
    bearer: str
    ts: float
    detail: str
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "bearer": self.bearer,
            "at": _iso(self.ts),
            "source": self.source,
            "detail": self.detail,
        }

    def __str__(self) -> str:
        return (
            f"{_iso(self.ts)}  {self.severity.upper():8s} {self.rule:9s} "
            f"bearer={self.bearer}  {self.detail}"
        )


@dataclass(frozen=True)
class Pending:
    """A run that has not yet crossed its dwell — reported, never alerted.

    This is the near-miss channel that keeps the pin rule honest: an idle lane
    resting at its floor for 4 minutes must NOT page anyone.
    """

    rule: str
    bearer: str
    started: float
    ended: float
    detail: str

    @property
    def seconds(self) -> float:
        return self.ended - self.started


@dataclass
class WatchdogConfig:
    #: R1 — how long a ceiling may rest at/below the floor before it is a pin.
    pin_dwell_s: float = 600.0
    #: R2 — budget-pushback events inside ``pushback_window_s`` that mean "spike".
    pushback_count: int = 3
    pushback_window_s: float = 300.0
    #: R2 — minimum gap between two pushback alerts for the same bearer.
    pushback_cooldown_s: float = 300.0
    #: R3 — queue-wait-timeouts per minute that mean "clients are already dying".
    timeout_rate_per_min: float = 2.0
    timeout_window_s: float = 60.0
    timeout_cooldown_s: float = 300.0
    #: Fallback floor when a scrape carries no ``anthropic_aimd_floor`` series.
    floor: float = 1.0
    #: Endpoint labels for live mode; empty for replay.
    endpoints: dict[str, str] = field(default_factory=dict)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S%z")


def _window_counts(
    scrapes: Sequence[Scrape], name: str, window_s: float
) -> list[tuple[float, str, int, float]]:
    """Per-scrape ``(ts, bearer, events_inside_window, minutes_span)``.

    Counts *increments*, not counter deltas. Three reasons this is the right
    oracle for a rate rule:

    * a cumulative counter's first observation already includes whatever
      happened at that instant, so a delta-based window undercounts by one and
      a "3 events in 5 min" rule silently becomes "4 events";
    * a gate restart resets the counter to zero, and a decrease must contribute
      nothing rather than a negative burst (the 18:48 restart is in the replay);
    * the honest question is "how many pushbacks happened in this window",
      which is exactly the number of observed rises.
    """
    out: list[tuple[float, str, int, float]] = []
    history: dict[str, list[tuple[float, float]]] = {}
    for scrape in scrapes:
        for bid, value in scrape.bearers(name).items():
            series = history.setdefault(bid, [])
            series.append((scrape.ts, value))
            cutoff = scrape.ts - window_s
            inside = [
                ts
                for (prev_ts, prev_value), (ts, cur_value) in zip(series, series[1:], strict=False)
                if ts >= cutoff and cur_value > prev_value
            ]
            span = (scrape.ts - min(inside)) / 60.0 if inside else window_s / 60.0
            out.append((scrape.ts, bid, len(inside), span))
    return out


def _rule_unreachable(
    gates: Sequence[UnreachableGate],
    scrapes: Sequence[Scrape],
    pending: list[Pending],
) -> list[Alert]:
    """R4: a gate the watchdog could not scrape is itself a finding.

    Runs BEFORE the other rules and even on an empty scrape stream: a run where
    every scrape failed has no series to evaluate, and reporting nothing would
    make blindness indistinguishable from health. A fully-blind gate is a
    critical ``unreachable`` Alert; a gate that failed only SOME scrapes is a
    ``Pending`` near-miss (one transient timeout must not page anyone, exactly
    like the pin dwell).
    """
    alerts: list[Alert] = []
    run_end = scrapes[-1].ts if scrapes else time.time()
    for gate in gates:
        detail = (
            f"{gate.source} ({gate.endpoint}) failed {gate.failures}/{gate.attempts} "
            f"scrape(s), last error: {gate.last_error}"
        )
        if gate.blind:
            alerts.append(
                Alert(
                    rule="unreachable",
                    severity="critical",
                    bearer=gate.source,
                    ts=gate.last_ts or run_end,
                    source=gate.source,
                    detail=(
                        f"{detail} — watchdog is BLIND to this gate for the whole run; "
                        "no rule below saw its metrics"
                    ),
                )
            )
        else:
            pending.append(
                Pending(
                    rule="unreachable",
                    bearer=gate.source,
                    started=gate.first_ts,
                    ended=gate.last_ts,
                    detail=f"{detail} — partial outage, other scrapes landed",
                )
            )
    return alerts


def evaluate(
    scrapes: Sequence[Scrape],
    config: WatchdogConfig,
    unreachable: Sequence[UnreachableGate] = (),
) -> tuple[list[Alert], list[Pending]]:
    """Run every rule over a chronological scrape stream.

    ``unreachable`` carries per-endpoint scrape failures from live ``poll``;
    it is consulted first because an all-failed run yields no scrapes at all
    and the early return below would otherwise hide the blindness.
    """
    alerts: list[Alert] = []
    pending: list[Pending] = []
    alerts.extend(_rule_unreachable(unreachable, scrapes, pending))
    if not scrapes:
        return alerts, pending
    scrapes = sorted(scrapes, key=lambda s: s.ts)
    alerts.extend(_rule_pinned(scrapes, config, pending))
    alerts.extend(
        _rule_rate(
            scrapes,
            name=M_AIMD_SHRINKS,
            rule="pushback",
            severity="warning",
            count_threshold=float(config.pushback_count),
            window_s=config.pushback_window_s,
            cooldown_s=config.pushback_cooldown_s,
            describe=lambda delta, span, thr: (
                f"{int(delta)} budget-pushback AIMD shrink(s) in {span:.1f} min "
                f"(threshold {int(thr)} / {config.pushback_window_s / 60:.0f} min) — "
                "upstream free-tier budget pressure, ceiling about to collapse"
            ),
        )
    )
    alerts.extend(
        _rule_rate(
            scrapes,
            name=M_QUEUE_WAIT_TIMEOUTS,
            rule="qtimeout",
            severity="critical",
            count_threshold=config.timeout_rate_per_min * config.timeout_window_s / 60.0,
            window_s=config.timeout_window_s,
            cooldown_s=config.timeout_cooldown_s,
            describe=lambda delta, span, thr: (
                f"{int(delta)} queue-wait-timeout 503(s) in {span:.2f} min "
                f"(threshold {config.timeout_rate_per_min:g}/min) — clients are "
                "already being refused; pi panes will enter retry loops"
            ),
        )
    )
    return alerts, pending


def _close_pin_runs(
    open_runs: dict[str, dict[str, Any]], closed: list[dict[str, Any]], end_ts: float
) -> None:
    """Close every still-open run at ``end_ts`` (end of stream)."""
    for run in open_runs.values():
        run["ended"] = end_ts
        closed.append(run)


def _classify_pin_run(
    run: dict[str, Any], config: WatchdogConfig, pending: list[Pending]
) -> Alert | None:
    """Alert when the run crossed the dwell, else record the near-miss."""
    seconds = run["ended"] - run["started"]
    if seconds < config.pin_dwell_s:
        pending.append(
            Pending(
                rule="pinned",
                bearer=run["bearer"],
                started=run["started"],
                ended=run["ended"],
                detail=(
                    f"ceiling {run['ceiling']:g} <= floor {run['floor']:g} for "
                    f"{seconds / 60.0:.1f} min — below the "
                    f"{config.pin_dwell_s / 60:.0f} min dwell, not an alert"
                ),
            )
        )
        return None
    crossed = run["started"] + config.pin_dwell_s
    return Alert(
        rule="pinned",
        severity="critical",
        bearer=run["bearer"],
        ts=crossed,
        source=run["source"],
        detail=(
            f"ceiling {run['ceiling']:g} <= floor {run['floor']:g} for "
            f"{seconds / 60.0:.1f} min "
            f"({_iso(run['started'])} -> {_iso(run['ended'])}; dwell crossed "
            f"{_iso(crossed)}) — AIMD is pinned; a lane this slow cannot drain "
            "its own queue and every other request will 503"
        ),
    )


def _collect_pin_runs(scrapes: Sequence[Scrape], config: WatchdogConfig) -> list[dict[str, Any]]:
    """Collect each bearer's contiguous at-floor runs over the stream."""
    runs: dict[str, dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []
    for scrape in scrapes:
        for bid, ceiling in scrape.bearers(M_AIMD_MAX).items():
            floor = scrape.bearers(M_AIMD_FLOOR).get(bid, config.floor)
            run = runs.get(bid)
            if ceiling <= floor:
                if run is None:
                    runs[bid] = {
                        "bearer": bid,
                        "started": scrape.ts,
                        "ceiling": ceiling,
                        "floor": floor,
                        "source": scrape.source,
                    }
                else:
                    run["ceiling"] = ceiling
            elif run is not None:
                run["ended"] = scrape.ts
                closed.append(run)
                runs.pop(bid, None)
    _close_pin_runs(runs, closed, scrapes[-1].ts)
    return closed


def _rule_pinned(
    scrapes: Sequence[Scrape], config: WatchdogConfig, pending: list[Pending]
) -> list[Alert]:
    """R1: ceiling at/below the configured floor for >= ``pin_dwell_s``.

    Two passes: first collect each bearer's contiguous at-floor runs, then emit
    one alert per run whose TOTAL length crossed the dwell. Emitting after the
    run closes means the alert carries the real recovery instant instead of the
    moment the dwell happened to be crossed — on the 19/10 replay that is the
    difference between "pinned 10.8 min" and "pinned 15.3 min, recovered
    19:09:03".

    The floor comes from the scraped ``anthropic_aimd_floor`` series (falling
    back to ``config.floor``) so the rule keeps working when
    ``THROTTLE_AIMD_MIN`` is hot-tuned mid-run.
    """
    alerts: list[Alert] = []
    for run in _collect_pin_runs(scrapes, config):
        alert = _classify_pin_run(run, config, pending)
        if alert is not None:
            alerts.append(alert)
    return alerts


def _rule_rate(
    scrapes: Sequence[Scrape],
    *,
    name: str,
    rule: str,
    severity: str,
    count_threshold: float,
    window_s: float,
    cooldown_s: float,
    describe: Callable[[float, float, float], str],
) -> list[Alert]:
    """Generic sliding-window counter-rate rule (R2 and R3 share it)."""
    alerts: list[Alert] = []
    last_fired: dict[str, float] = {}
    for ts, bid, count, span in _window_counts(scrapes, name, window_s):
        if count < count_threshold:
            continue
        previous = last_fired.get(bid)
        if previous is not None and ts - previous < cooldown_s:
            continue
        last_fired[bid] = ts
        alerts.append(
            Alert(
                rule=rule,
                severity=severity,
                bearer=bid,
                ts=ts,
                source=scrapes[0].source,
                detail=describe(float(count), span, float(count_threshold)),
            )
        )
    return alerts


def replay_journal(
    lines: Iterable[str],
    *,
    floor: float = 1.0,
    config: WatchdogConfig | None = None,
    source: str = "replay",
) -> tuple[list[Alert], list[Pending], Reconstructor]:
    """Rebuild the metric stream from a capture and evaluate every rule."""
    cfg = config or WatchdogConfig()
    scrapes, recon = scrapes_from_journal(lines, floor=floor, source=source)
    alerts, pending = evaluate(scrapes, cfg)
    return alerts, pending, recon


# ---------------------------------------------------------------------------
# Live mode + CLI
# ---------------------------------------------------------------------------
def poll(
    endpoints: dict[str, str],
    *,
    interval: float,
    samples: int,
    config: WatchdogConfig,
    timeout: float = 5.0,
    on_scrape: Any = None,
) -> tuple[list[Alert], list[Pending], list[Scrape]]:
    """Scrape every gate ``samples`` times, ``interval`` seconds apart.

    A failed scrape is NOT discarded: it is recorded on the endpoint's
    :class:`UnreachableGate` (count + timestamps + last error) and handed to
    :func:`evaluate`, which raises it to an ``unreachable`` Alert or Pending.
    The stderr line stays for the operator watching the run live.
    """
    scrapes: list[Scrape] = []
    gates: dict[str, UnreachableGate] = {
        name: UnreachableGate(source=name, endpoint=endpoint)
        for name, endpoint in endpoints.items()
    }
    for index in range(samples):
        for name, endpoint in endpoints.items():
            gate = gates[name]
            gate.attempts += 1
            try:
                scrape = fetch_metrics(endpoint, timeout=timeout, source=name)
            # URLError and TimeoutError are both OSError subclasses in py3; the
            # base class alone covers refused connections, DNS failures and
            # read timeouts, and any new OSError descendant stays covered.
            except OSError as exc:
                # Handling, not swallowing: the failure is counted on the gate
                # below and re-surfaced by evaluate() as an unreachable finding.
                gate.note_failure(exc)
                print(f"warn: {name} {endpoint} unreachable: {exc}", file=sys.stderr)
                continue
            scrapes.append(scrape)
            if on_scrape is not None:
                on_scrape(scrape)
        if index + 1 < samples:
            time.sleep(interval)
    alerts, pending = evaluate(
        scrapes, config, unreachable=[g for g in gates.values() if g.failures]
    )
    return alerts, pending, scrapes


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="free-gate-watchdog",
        description="Per-bearer watchdog for the OpenCode free gates (:8770 go, :8774 zen).",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--live", action="store_true", help="scrape /metrics from the live gates")
    source.add_argument("--replay", metavar="JOURNAL", help="replay a journalctl capture")
    parser.add_argument(
        "--endpoint",
        action="append",
        default=[],
        metavar="NAME=URL",
        help="live endpoint (repeatable); default go=http://127.0.0.1:8770 zen=http://127.0.0.1:8774",
    )
    parser.add_argument(
        "--interval", type=float, default=15.0, help="live: seconds between scrapes"
    )
    parser.add_argument("--samples", type=int, default=4, help="live: number of scrapes")
    parser.add_argument("--timeout", type=float, default=5.0, help="live: per-scrape HTTP timeout")
    parser.add_argument(
        "--floor",
        type=float,
        default=1.0,
        help="replay: THROTTLE_AIMD_MIN in force during the capture (default 1)",
    )
    parser.add_argument("--pin-dwell", type=float, default=600.0, help="R1 dwell seconds")
    parser.add_argument("--pushback-count", type=int, default=3, help="R2 events per window")
    parser.add_argument("--pushback-window", type=float, default=300.0, help="R2 window seconds")
    parser.add_argument("--timeout-rate", type=float, default=2.0, help="R3 503s per minute")
    parser.add_argument("--timeout-window", type=float, default=60.0, help="R3 window seconds")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--show-series",
        action="store_true",
        help="replay: print the reconstructed per-bearer ceiling timeline",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> WatchdogConfig:
    endpoints: dict[str, str] = {}
    for item in args.endpoint:
        name, _, url = item.partition("=")
        endpoints[name] = url
    if args.live and not endpoints:
        endpoints = {
            "go:8770": "http://127.0.0.1:8770",
            "zen:8774": "http://127.0.0.1:8774",
        }
    return WatchdogConfig(
        pin_dwell_s=args.pin_dwell,
        pushback_count=args.pushback_count,
        pushback_window_s=args.pushback_window,
        timeout_rate_per_min=args.timeout_rate,
        timeout_window_s=args.timeout_window,
        floor=args.floor,
        endpoints=endpoints,
    )


def _timeline(scrapes: Sequence[Scrape]) -> list[dict[str, Any]]:
    """Reconstructed per-bearer ceiling timeline (replay evidence)."""
    out: list[dict[str, Any]] = []
    last: dict[str, float] = {}
    for scrape in sorted(scrapes, key=lambda s: s.ts):
        for bid, ceiling in scrape.bearers(M_AIMD_MAX).items():
            if last.get(bid) != ceiling:
                last[bid] = ceiling
                out.append({"at": _iso(scrape.ts), "bearer": bid, "ceiling": ceiling})
    return out


def _json_payload(
    args: argparse.Namespace,
    config: WatchdogConfig,
    scrapes: Sequence[Scrape],
    alerts: Sequence[Alert],
    pending: Sequence[Pending],
    recon: Reconstructor | None,
) -> dict[str, Any]:
    """Build the ``--json`` document (also the evidence artifact)."""
    payload: dict[str, Any] = {
        "mode": "replay" if args.replay else "live",
        "source": args.replay or ",".join(config.endpoints),
        "scrapes": len(scrapes),
        "window": {
            "start": _iso(scrapes[0].ts) if scrapes else None,
            "end": _iso(scrapes[-1].ts) if scrapes else None,
        },
        "alerts": [a.to_dict() for a in alerts],
        "pending": [
            {
                "rule": p.rule,
                "bearer": p.bearer,
                "started": _iso(p.started),
                "ended": _iso(p.ended),
                "seconds": round(p.seconds, 1),
                "detail": p.detail,
            }
            for p in pending
        ],
    }
    if recon is not None:
        payload["timeline"] = _timeline(scrapes)
        payload["extra"] = recon.extra
    return payload


def _print_findings(alerts: Sequence[Alert], pending: Sequence[Pending], *, indent: str) -> None:
    """Print the ALERTS and PENDING sections."""
    if alerts:
        print(f"ALERTS ({len(alerts)}):")
        for alert in sorted(alerts, key=lambda a: a.ts):
            print(f"{indent}{alert}")
    else:
        print("ALERTS: none")
    if pending:
        print()
        print(f"PENDING / near-miss ({len(pending)}):")
        for item in pending:
            print(
                f"{indent}{_iso(item.started)}  {item.rule:9s} bearer={item.bearer}  {item.detail}"
            )


def _print_report(
    args: argparse.Namespace,
    config: WatchdogConfig,
    scrapes: Sequence[Scrape],
    alerts: Sequence[Alert],
    pending: Sequence[Pending],
    recon: Reconstructor | None,
) -> None:
    """Print the human-readable report (the default, non-``--json`` output)."""
    print(f"free-gate-watchdog  mode={'replay' if args.replay else 'live'}")
    if scrapes:
        print(
            f"  window   {_iso(scrapes[0].ts)} -> {_iso(scrapes[-1].ts)}  ({len(scrapes)} scrapes)"
        )
    if recon is not None and args.show_series:
        print("  ceiling timeline:")
        for row in _timeline(scrapes):
            print(f"    {row['at']}  bearer={row['bearer']}  ceiling={row['ceiling']:g}")
    if recon is not None:
        print(f"  journal extras: {recon.extra}")
    print(
        f"  thresholds  pin>= {config.pin_dwell_s / 60:.0f} min at/below floor; "
        f"pushback>= {config.pushback_count} / {config.pushback_window_s / 60:.0f} min; "
        f"503>= {config.timeout_rate_per_min:g} / min"
    )
    print()
    _print_findings(alerts, pending, indent="  ")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = _config_from_args(args)
    recon: Reconstructor | None = None

    if args.replay:
        with open(args.replay, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
        alerts, pending, recon = replay_journal(
            lines, floor=args.floor, config=config, source=args.replay
        )
        scrapes = recon.scrapes
    else:
        scrapes = []
        alerts, pending, scrapes = poll(
            config.endpoints,
            interval=args.interval,
            samples=args.samples,
            config=config,
            timeout=args.timeout,
        )

    if args.json:
        print(json.dumps(_json_payload(args, config, scrapes, alerts, pending, recon), indent=2))
    else:
        _print_report(args, config, scrapes, alerts, pending, recon)
    return 1 if any(a.severity == "critical" for a in alerts) else 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
