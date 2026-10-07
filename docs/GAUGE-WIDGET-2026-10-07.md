# Gauge widget — 07/10/2026

Generator family: **openai** (GPT-6.1 Sol). Branch: `313-gauge-widget`.
Base: `3c64a535` (#312). No model review/approval claimed.

## Plan → tasks → acceptance

1. Reproduce the actual live widget, trace its callers/history/templates.
2. Repair only UI rendering, source selection, freshness and accessible semantics.
3. Run red/green route regression, targeted/full tests, Ruff/types, wire/browser
   receipt and current-head PR CI. Coordinator owns activation.

Frozen contract: Throttler `PLAN-2026-10-07-mimo-gauge-quality.md`.
Scope: `ui/`, UI tests and these evidence artifacts only. No limiter/admission,
producer, token, shared settings, systemd or fleet provider changes.

## Evidence and hypothesis

Date verified: desktop `date`, **07/10/2026 16:05 BRT**.
Read-only `/ui` + `/ui/stats` on `:8765` and `:8773` identified the arc as
**local accounted output tokens/s**, not the subscription quota tracks. At the
initial capture, `:8765` pointed to disabled loopback upstream `:1`, had served
zero requests, and displayed MiMo only in the routing rail. `:8773` had served
2,081 requests since start; its trailing ring held no accounted output. Those
facts do not establish an instantaneous MiMo stream speed or available quota.

Hypothesis: the local-only arc cannot answer the operator's MiMo workload
question; explicit per-tab sibling selection, cache-only rendering and
sample-based freshness repair this path without changing inference routing.

Falsifiers: real route still invokes a network collector; `source=mimo` still
uses the local ring; failed/stale sibling substitutes local zero; frozen local
sample remains current through HTML polls. These are tested at real aiohttp
handler/Jinja seams, not only helper arithmetic.

An initial verbal hypothesis overstated accessibility: the captured live SVG
already says **“Output throughput not yet measured”**, not numeric zero. The
actual unknown-state defect is narrower: the rounded zero-length sweep,
zero-position peak and sparkline still depict/announce zero-like measurements.
No claim that every quota meter is broken.

## Repair

- `/ui?source=mimo` selects a configured sibling **for this tab only**; a
  keyboard-accessible server-rendered link picker exposes local/sibling choices.
  HTMX carries the same source into every `/ui/stats` poll. Default YAML display
  policy and revision-triggered `HX-Refresh` remain intact. Unknown source never
  falls back to local telemetry. This is not an inference routing control.
- Rendering reads account/fleet/Copilot caches and the existing out-of-process
  lane report only. Existing account collection keeps its 300-second background
  cadence. Fleet/Copilot polling moves to a cancellable background task with
  existing collector timeouts, single-flight/TTL/backoff and a five-second tick.
  Collector exceptions are isolated; a cold/stale reading is unknown.
- Cached sibling health ages out after three five-second ticks. Local gauge
  samples age out after three ten-second sampler ticks. Render time is never
  sample time; backward-clock local samples are also stale. Sibling age labels
  describe collector freshness, not an upstream sampler timestamp.
- Unknown/stale arcs do not draw a value sweep, peak marker or claimed numeric
  sparkline. Accessible labels say unknown/stale. Measured cold-start windows
  label their actual sampled duration (10–60s), not a fictional full minute.
  Peak is labelled a **10-second bucket**, matching the actual arithmetic.
- Output tokens/s, prompt-side in+cache tokens/s and optional measured fresh
  tokens/s remain separate. Quota percentages are explicitly **used quota**,
  not tokens/s. Throughput labels say **not a subscription quota**.
- Asset revision now covers signals/presentation as well as routes, templates
  and CSS, so an open tab reloads when rendering logic changes.

## Red-before / green-after

On base code with the new regression file, after correcting two test-fixture
symbol errors, `uv run pytest -q tests/test_gauge_routes.py` produced **8 failed
in 0.80s**. Failures included three collector invocations per route, ignored
per-tab source, absent stale handling, and unknown-state geometry/semantics.
Fixture setup errors are not counted as reproduction evidence.

Acceptance fixtures use synthetic telemetry: six ten-second buckets each
containing 1,200 output + 3,000 input/cache tokens. Expected rendered rates:
**120 output tokens/s, 300 input/cache tokens/s**, not a quota percentage.
Cases include missing/malformed/failed/stale sibling data, frozen local data,
revision refresh, unchanged shared defaults, partial collector failure/retry
and task cancellation. Existing caller tests now seed cache snapshots rather
than rely on render-time collector calls; their capacity assertions are kept.

Validation on the implementation tree:

| Command | Result |
|---|---|
| `uv run pytest -q tests/test_gauge_routes.py tests/test_workload_view.py tests/test_tps_gauge.py tests/test_capacity_view.py` | 59 passed, 1 warning, 1.40s |
| `uv run pytest -q` (admitted heavy lane, serial) | 2,059 passed, 155 existing-style warnings, 88.41s; peak memory 136.6 MB |
| `uv run ruff check src tests` | All checks passed |
| `uv run ruff format --check src tests` | 115 files already formatted |
| `node tests/dashboard-refresh-check.mjs` | PASS single-script invariant + server-side freshness stamp |
| Types / isolated browser / PR CI | Pending receipts. Desktop heavy admission refused `pressure-high`, then `reopen-pending`; no unadmitted fallback |

No coverage/security score or Sonar gate improvement is inferred from pytest.

## Meter contract and cross-owner inputs

Desktop **weekly remaining quota** and Token Plan **monthly credit usage** are
separate subscriptions, denominators and reset schedules. The route regression
renders distinct synthetic IDs and windows: an unknown Desktop weekly row
stays “no reading”, while the independently measured Token Plan monthly row
shows 75% used, 250 credits left of 1,000 and its own reset. No Desktop reading
or percentage is invented, and a Token Plan reading never fills it.

The new Desktop producer is not this branch's responsibility. The current lane
normalizer consumes per-meter `limitId`, `usedPercent`, `resetsAt`, `windowMins`,
`remaining` and `allowance`; it does **not** consume `remainingPercent`, source
semantics or a date-only `resetDate`. The integration owner/coordinator must
supply a separate identity and explicit semantics/reset timezone, then arrange
a reviewed compatible producer/normalizer contract. A raw Desktop SGP
`percent=100.0` is not silently presented as 100% used or as credits.

Live deployed `:8773/__throttle/health` currently does **not** publish
`throughput`, including with `?telemetry=1`. Thus the new selector correctly
shows unmeasured throughput on that old runtime until coordinator activation
ships the existing token export; it does not invent it from served counts.

Unknowns: the user's exact intended MiMo quota widget may also need that
producer; no fresh Desktop report exists in this branch. Sibling `/health`
publishes token buckets without an upstream sampler timestamp, so a fresh
health response alone cannot detect a dead sampler on that sibling. Throughput
is accounted at completion, not streaming instantaneous generation speed.

## Delivery boundary and reversal

No deployed UI acceptance is claimed. Coordinator must pin/activate the merged
proxy and verify `/ui?source=mimo`, HTMX polling, current runtime build and the
new separate Desktop meter after its producer exists. Preserve `:8773` and
legacy Token Plan while doing so. Source merge does not activate any unit here.

Reversal: one normal `git revert <squash-commit>` PR; no runtime changes to undo
from this worker. PR/head/CI and browser/type receipt are recorded below.
