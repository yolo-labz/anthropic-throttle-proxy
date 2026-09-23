# MiMo subscription meter

## Hypothesis and falsifier

The subscriptions UI cannot display a MiMo console meter because the lane reader
has no MiMo window-meter mapping or independent report input. A fresh MiMo
report rendering a monthly allowance on the unmodified reader would falsify it.

## Scope

Dashboard telemetry only. Reuse the existing lane normalization, subscription
rows and stale-data policy. No inference endpoint, key, routing policy, payment,
plan upgrade or artificial inference probe is added.

## Tasks and acceptance

- [x] Add `mimo` provider metadata and reuse window-meter normalization.
- [x] Read optional `THROTTLE_MIMO_REPORT` separately, with its own timestamp and
  cadence. Missing/invalid reports produce an unknown row; old samples go stale.
- [x] Add an authenticated browser-host sampler using an existing supervised
  `xiaomi` profile. Require `MIMO_EXPECTED_ACCOUNT_ID` and verify `/userProfile`
  before publishing. Read-only console navigation; never export cookies, keys,
  email, card data or full API responses.
- [x] Derive usage from `month_total_token` counters rather than rounded percent.
  Preserve the allowance and UTC reset. Do not invent a rolling-window start or
  burn pace. Caption explicitly restricts use to public-class work.
- [x] Test counter validation, secret exclusion, freshness, exhaustion, missing
  reports and the existing subscription renderer. Targeted suite: 47 passed;
  full suite: 1,321 passed on 23/09/2026. Ruff lint and format pass.
- [ ] Delivery: land PR, install collector and dashboard-only package, verify live
  rendered row and scheduled refresh. Source tests do not establish delivery.

## Collection contract

Run `scripts/mimo-token-plan-probe.py` with the web-automation virtualenv and
`lib.interactive` available, on the supervised browser host. Set
`MIMO_EXPECTED_ACCOUNT_ID` privately; absent/mismatched identity fails closed.
Use the browser supervisor's existing environment paths. No provider key is used.

Every 15 minutes, write stdout to a mode-0600 temporary file adjacent to the
configured report and atomically rename it **only after exit zero**. Preserve the
last sample on failure; its age must increase until the dashboard marks it stale
(after two collection intervals, with the existing minimum freshness bound).
The collector must time out rather than overlap indefinitely. Runtime reports
belong under XDG state/runtime, not in git.

Set `THROTTLE_MIMO_REPORT` on the dashboard service to that report path. Optional
`fleet-ui.yaml` subscription metadata can bind `lane: mimo:plan`; the observed
plan is already in the report and must not be replaced with a fabricated meter.

## Boundaries and rollback

This console API is observed, not a documented public telemetry API. A changed
response shape or expired login causes failed sampling, not an invented healthy
state. The report says nothing about content filters or private-work eligibility.

Code rollback: one revert PR of this slice's squash commit. Runtime rollback:
stop the dedicated collector timer, remove its dashboard environment binding and
restore the prior dashboard package. Leave the general lane collector and all
inference services untouched.
