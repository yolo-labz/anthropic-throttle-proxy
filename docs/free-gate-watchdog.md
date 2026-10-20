# Free-gate per-bearer watchdog (`:8770` go, `:8774` zen)

**Slice:** observability (opencode free-models fleet, 19/10/2026).
**Files:** `src/anthropic_throttle_proxy/free_gate_watchdog.py`,
`scripts/free-gate-watchdog.py`, `src/anthropic_throttle_proxy/metrics.py`
(`anthropic_aimd_floor`), `tests/test_free_gate_watchdog.py`.
**Evidence:** `docs/evidence/free-gate-watchdog-2026-10-19/`.

## The incident it exists to catch

Live go-gate (`:8770`) journal, 19/10/2026 18:46-19:09 BRT, shared bearer
`e574f38a`:

```
18:49:02  aimd-shrink max_concurrent=8    budget pushback (429)
18:50:24  aimd-shrink max_concurrent=5
18:51:44  aimd-shrink max_concurrent=3
18:53:15  aimd-shrink max_concurrent=2
18:53:48  aimd-shrink max_concurrent=1    <- pinned at the floor
19:02:24  queue-wait-timeout est_wait_s=342.90 max_wait_s=179.98
19:02:26  queue-wait-timeout reason=pre-queue-depth ... (10 total, 8 pre-queue)
19:09:03  aimd-grow   max_concurrent=2    <- first recovery
```

The ceiling rested at 1 for **15.2 min** while generations ran 171-303 s. Every
other request was refused 503 (drain estimate 210-355 s > 180 s client wait
budget) and pi panes died in retry loops. Nothing alerted, because
`anthropic_aimd_max_concurrent{...}=1` is indistinguishable from a healthy
idle lane.

## Rules

| rule | condition (default) | severity | fires on the incident at |
|---|---|---|---|
| `pushback` | ≥3 budget-class AIMD shrinks / 5 min per bearer | warning | **18:50:24** (10.8 min before the first 503) |
| `qtimeout` | ≥2 queue-wait-timeout 503s / min per bearer | critical | **19:02:30** (clients already dying) |
| `pinned` | ceiling ≤ floor sustained ≥10 min | critical | **19:03:48** (confirms the 15.2 min pin) |
| `unreachable` | every live scrape of a gate failed (partial → `Pending`) | critical | live-only — the watchdog is blind to that gate |

Each rule is paired with a falsifier test (near-miss must stay silent):

- a 4-min pin → `PENDING`, not an alert (idle lanes rest at the floor);
- a healthy ceiling never alerts at any duration;
- concurrency-class 429 bursts don't move `anthropic_aimd_shrinks_total`
  (budget-class only, per `proxy._aimd_apply_pushback`) → must not alert;
- one isolated timeout is not a storm; a counter reset (gate restart, the
  18:48:59 one is in this very incident) contributes no fake spike.

`pushback` is the actionable early signal. `pinned`'s 10-min dwell is
deliberate — it is a confirmation, not the alarm — and any open run shorter
than the dwell is still reported as `PENDING` so the near-miss channel stays
visible. Alert firing is rate-limited per bearer (300 s cooldown) to avoid
paging loops.

## New metric: `anthropic_aimd_floor{bearer}`

A scrape-time collector in `metrics.py` publishes the configured
`THROTTLE_AIMD_MIN` per bearer (and `_anon` on a cold gate). Read at scrape
time, not import time, because `AIMD_MIN` is hot-tunable via the config-knob
surface (`config.py` `_set_module_attr`). With it, `max_concurrent <= floor`
becomes machine-checkable in Grafana:
`anthropic_aimd_max_concurrent <= anthropic_aimd_floor` for 10 min = pinned.

## Usage

```bash
# live: scrape :8770 and :8774, 4 samples @15 s
python scripts/free-gate-watchdog.py --live

# live with a fallback floor while the deployed gate predates the metric patch
python scripts/free-gate-watchdog.py --live --floor 2

# replay a journalctl capture (the incident demo)
journalctl --user -u opencode-go-throttle-proxy.service \
  --since "2026-10-19 18:45" --until "2026-10-19 19:12" -o short-iso > /tmp/go.log
python scripts/free-gate-watchdog.py --replay /tmp/go.log --floor 1 --show-series

# machine-readable (timeline reconstructed from the journal)
python scripts/free-gate-watchdog.py --replay /tmp/go.log --floor 1 --json
```

Exit status: `1` when a **critical** alert fires, else `0` — so
`systemctl --user` timers with `OnFailure=` and shell `||` both work. Tuning
flags: `--pin-dwell`, `--pushback-count`, `--pushback-window`, `--timeout-rate`,
`--timeout-window`.

## Replay fidelity

`--replay` folds a `journalctl -o short-iso` capture into the *same* cumulative
metric series the live gate would expose (`aimd-shrink` →
`anthropic_aimd_shrinks_total`, `queue-wait-timeout` →
`anthropic_queue_wait_timeouts_total`, `done status=` →
`anthropic_requests_total`, `bearer-new`/`aimd-*` → gauges), including
counter-zeroing on the `listening` restart line — then runs the identical rule
functions. One rule engine, two input paths; the replay cannot drift from
production.

Known limit: per-event scrapes mean the window-rate rules see an upper bound
on resolution (an event is visible from its log line, not a 15 s poll), so a
replay may fire slightly earlier than the live poller would. That direction is
safe (alert slightly early, never late).

## Wiring (left for the integrator)

No systemd unit ships in this slice (no src edits outside the slice's files,
no commits). A 1-min timer running `--live --json` with `OnFailure=` on the
timer unit would close the loop; the alert sink (ntfy/Gatus/Grafana) is an
integrator decision.
