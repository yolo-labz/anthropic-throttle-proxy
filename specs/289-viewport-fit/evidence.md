# Viewport-fit acceptance — 01/10/2026

## Executed browser checks

The unchanged `bd18131` templates and CSS, rendered with the existing nine-seat
fixture, fail the new viewport check: **1765px content in a 1366×768 viewport**
(`red.txt`). The candidate passes:

| Viewport | Document | Horizontal panel overflow |
|---|---|---|
| 1366×768 | 1366×768 | none |
| 1440×900 | 1440×900 | none |
| 1920×1080 | 1920×1080 | none |
| 2210×1240 | 2210×1240 | none |
| 390×844 | 390×3076 | normal narrow-table scrolling; no document overflow |

`tests/check_viewport.py` asserts all nine subscription rows, telemetry and footer
fit on desktop, checks panel scroll widths, preserves 14.25px row identities, and
operates the details checkbox by keyboard. Detailed provenance and absolute reset
times reappear and stay visible after replacing the polled stats fragment. The
compact view keeps meter values/allowances, relative resets, status, billing and
plan-conflict warnings, and transport/capacity/model-eligibility distinctions.
No document clipping, zoom, transforms, added JavaScript or dependencies.

Receipts: `viewport.json`, `render.txt`, `viewport-*.png`. These are synthetic
fixture screenshots, not evidence that the installed UI has changed.

The desktop renderer stalled starting its Node driver; a separate bounded probe
also stalled at `utilsBundle.js` (`driver-diagnostic.txt`). Used the existing
Nix.Server isolated headless Chromium via installed Patchright, **not** an
operator profile. The same checker/assertions ran; only its browser-client import
was changed to the installed compatible API and fixture generation was skipped
because the locally rendered HTML/CSS were transferred unchanged. The temporary
remote directory is only execution state; all results were copied into this spec.

Reproduce with installed Chromium and the declared delivery dependencies:

```sh
PYTHONPATH=src:tests PLAYWRIGHT_NODEJS_PATH="$(command -v node)" \
  BROWSER_EXECUTABLE="$(command -v google-chrome-stable)" \
  uv run --group delivery python tests/check_viewport.py
```

## Other gates

- 156 targeted UI/layout/capacity/status/icon/contrast tests passed.
- Ruff lint and format checks passed; `git diff --check` passed.
- Local full pytest run hit its 240-second bound after 124 cases; this is **not**
  a full-suite pass. The exact staged source was then run in an isolated server
  directory: **1478 passed, 122 warnings in 89.38s**, exit 0 (`server-pytest.txt`).
  Required remote CI remains necessary before merge.
- Local normal commit hooks stalled/terminated (`commit.exit` = 1). A server
  worktree has the byte-identical pre-commit hook, but transferring the exact
  engine stalled too. A locked dependency reconstruction failed with npm's
  `Invalid Version`; it was not used as a substitute passing gate. Subsequent
  server SSH timed out. No `--no-verify`, skipped gate, or approval is claimed.

## Workload truth fix (this branch)

The screenshoted gauge read the idle, deliberately offline local Anthropic
proxy (upstream `127.0.0.1:1`) while MiMo carried 12–20 in-flight requests, so
"0 tokens/s" and "CRIT — bearer refused" described the wrong process. Red
capable test first: `tests/test_workload_view.py::test_selected_workload_replaces_
local_zero_and_refusal` failed `assert 0 == 12` before the change
(`workload-red.txt`). After the change the same suite is green.

- `defaults.workload: <fleet label>` in `fleet-ui.yaml` selects one measured
  sibling as the dashboard's workload source. Local counters, tps and the
  disabled-primary refusal verdict are replaced by the sibling's measured
  counters; local golden signals/bearers stay off the board (they belong to
  the idle Anthropic sidecar).
- Sibling health carries opt-in `?telemetry=1` bounded token buckets (no
  identity/prompts/external I/O); default health schema is unchanged
  (`test_health_keeps_its_own_schema` still exact-matches). Bad/absent/old
  telemetry is **unknown, never zero**: the panel says "throughput unavailable
  … Not zero tokens/s" and the dial prints `—` until real buckets arrive.
  Inputs are validated (int, non-negative, bounded, ≤360 buckets) before any
  arithmetic; a malformed sibling cannot break the render.
- The MiMo meter probe (`mimo-dashboard-probe`) failed 17:51–18:23 on SSH
  timeouts and systemd start timeouts, leaving `mimo-plan.json` stale — that
  was the "stale" badge, not quota. It succeeded again at 18:53 once the link
  recovered; readings are fresh again.
- Red before fix / green after: **1494 pytest passed** on the isolated server
  suite (`workload-full-pytest.txt`), Ruff clean, `git diff --check` clean.
  Local hooks stall under the host's ~900 load average; the exact
  `code-slop-gate` package (242 MB) was rsynced to the server and verified
  (`aislop 0.12.0`, gate-upload.log) so normal pre-commit gates run there
  byte-identically. No `--no-verify`, no `CODE_SLOP_GATE_SKIP`.
- The earlier COC "Failed to resolve API key" was retested in that exact
  process environment: the helper returned the expected key in 0.05–0.16 s,
  three times, no privacy flag. The session resumed assistant/tool exchanges.
  The historical failure cause is **not established** — only that it does not
  currently reproduce and no credential/model was changed.

The live runtime still runs the old package: MiMo's own build predates token
accounting, so even with workload selection its gauge must read "unavailable"
until the MiMo service restarts at a verified idle boundary with the new build.
That restart is a separate, gated step — busy inference is never interrupted
just to update a dashboard.

## Delivery

Recovery check at 21:39 BRT: neither host committed or pushed this branch;
the earlier retry driver exited successfully after failed SSH attempts. It
is not a delivery receipt and must not be rerun. A fresh local run passed
**1494 tests, 122 warnings in 79.63s** (`recovery-pytest.txt`), plus Ruff lint,
format and whitespace checks. Normal local hooks and protected PR CI remain
required; no gate bypass or runtime activation is implied.

Activation must set BOTH `defaults.workload: mimo` in the display inventory
and the configured MiMo fleet health URL to
`http://127.0.0.1:8773/__throttle/health?telemetry=1`. The default health endpoint
deliberately omits token buckets. Verify `/ui` AND its actual polling endpoint
`/ui/stats`; `/ui/partials/stats` is not a registered UI route.

Pending protected PR CI/merge and idle-dashboard-only activation. The busy MiMo
lane, active agent turns, provider quotas and routing are outside this change.
More rows, expanded details, phone-sized or zoomed views may scroll naturally;
this is a tested nine-seat desktop overview, not a claim to fit arbitrary data
into a finite screen.
