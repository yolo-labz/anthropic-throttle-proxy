# Combined acceptance — 01/10/2026

## Verified source, not a deployment claim

Integrated onto `9fc1eed` (landed persistent admission wait fix), preserving
original worker worktrees. No concurrency ceiling, privacy rule, reserved lane,
credential assignment or provider purchase changed.

- Full Python suite: **1478 passed**, 122 existing aiohttp warnings; `pytest.txt`.
- Native Pi 0.85.1 adapter: **233 passed, 0 failed**, `client.tap`.
- Ruff check and format check: clean for `src`, `tests`, Team sampler and render
  check (81 files formatted). `git diff --check`: clean.
- Synthetic fairness: **24/24 assertions**, `fairness.txt`; not a provider benchmark.
- Headless isolated Chromium render: **1440/1440** and **390/390** viewport/content
  widths; screenshots `capacity-1440.png`, `capacity-390.png`. Synthetic inputs,
  no operator profile, credentials, provider requests or external CDN.
- Optional different-family advisory review of OpenAI corrections: Z.AI refused
  before spawn, no available slots. **Unavailable, not approval.** The recovered
  telemetry/fairness work has its prior review receipts under 279/282; no new
  independent exact-head review is claimed.

## Reproduced defects and corrections

1. Four recovered routing regressions failed: retired static caller with idle
   or busy replacement, quarantined incoming credential, unequal live limits.
   Explicit credential kind confines the healthy-unconfigured exception to
   OAuth pools. Known credential death overrides cached quota. Queue/inflight
   pressure is normalized by live per-account capacity, retaining scale and
   existing quota/Retry-After gates.
2. An additional **real-handler** falsifier exposed fallback to the retired key
   while the replacement was mid-recovery-probe. Keep an unconfigured static
   caller on a configured non-dead account's existing admission gate, including
   when no candidate may dispatch yet. No second recovery probe is dispatched.
   Retirement coverage exercises both messages and chat/completions endpoints.
3. The recovered acceptance queue test assumed load-aware routing would queue
   behind a full seat despite free space on another. It now first proves that
   spare capacity is used, fills both seats, then verifies the hard-cap queue.
   The fake limiter now exposes its real `max_concurrent` interface.
4. Mixed independent seats no longer use worst-seat-wins: exhausted individual
   plus usable Team is **limited**, not universally exhausted. A binding spent
   window still overrides another window's headroom on the same seat. Invalid
   numbers and unmeasured throughput are unknown/absent. Explicit nonbinding
   premium exhaustion cannot condemn unrelated products. Unlike units/windows
   are never summed, and model eligibility stays separately unverified.
5. Nine sampler falsifiers proved historical display counters vetoed valid
   current counters and invalid project paths reached browser attachment.
   History no longer determines current quota; path validation precedes attach,
   response capture is same-origin, mandatory identity/detail/usage responses
   are checked by name (not raw count), and the explicit Team fetch requires
   identity verification. Missing/invalid Team evidence remains unknown.
6. `docs/examples/fleet-ui-mimo.yaml` supplies **display-only** inventory for the
   individual plan, assigned owner and unassigned seat. It supplies no third
   credential or invented usable allowance. Merge by id; preserve other rows.

## Reproduce

```sh
uv run pytest -q
uv run ruff check src tests scripts/mimo-token-plan-probe.py
uv run ruff format --check src tests scripts/mimo-token-plan-probe.py
uv run python scripts/check-seat-fairness.py
PI_CODING_AGENT_ROOT=/path/to/pi-coding-agent node --test clients/pi-queue-wait/*.test.mjs
uv run python tests/render_preview.py specs/288-capacity-delivery/capacity-preview.html
PLAYWRIGHT_NODEJS_PATH="$(command -v node)" BROWSER_EXECUTABLE=/path/to/installed/chromium \
  uv run --group delivery python specs/288-capacity-delivery/render_check.py \
  specs/288-capacity-delivery/capacity-preview.html
```

## Delivery boundaries

Source PR/CI, Nix pin/hash, private project configuration, client pin and runtime
activation are distinct. These source checks do **not** prove increased live
throughput, reduced provider disconnects, fresh subscription balances, or a
safe higher concurrency ceiling. Never restart an occupied lane merely to make
its dashboard look current. Deployment must compare persisted unit, effective
ExecStart, health import path and GC roots; only then remove a restore override.
