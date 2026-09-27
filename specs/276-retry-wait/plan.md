# Short Retry-After waits must park outside slots

## Hypothesis and falsifier

An already-serving bearer receiving a short cooldown does not arm its half-open
probe gate, so `_pre_dispatch_gate` falls through without waiting and
`_post_slot_recheck` returns its slot repeatedly until cooldown/deadline expiry.
Falsifier: a warmed bearer with a short headerless 429 can wait without repeated
slot acquisitions on the unchanged source.

## Evidence (27/09/2026)

Live MiMo lane `:8773`, no central tier, package `myxmj4j74qs7rqg0sd0zrdzvbh49vr1i`:
10,000 alternating queue/start journal entries in 7.882921 s; five waiting
clients, live cap 2, no forwarded request counted inflight at the sample.
Drain history was polluted with 64 near-zero holds (service time 0.001 s).

The isolated local-fake-upstream regression warms a bearer, returns the exact
headerless 429 envelope, then issues another request during the short cooldown.
Unchanged source: six failures, 180–747 slot acquisitions where 0–1 were expected.
It exercises messages, chat completions and responses, with both an expiring
queue budget and a budget long enough to dispatch after the cooldown.

## Smallest correction

Reuse the existing deadline-bounded `wait_reval(limiter.wait_retry_after)` in the
non-probe branch, then recheck the same route/lease. Re-running account routing
while retaining a claimed probe loses its ownership and can wait on itself;
the existing `test_probe_lease_holder_never_parks_on_another_probe` caught that
in the first implementation and remains green without weakening its assertions. Keep long-window fast-fail, account
rerouting, half-open leases, upstream retry limits and service configuration
unchanged. No live restart or provider traffic is required for acceptance.

## Tasks

- [x] Capture live symptom and find the actual wait/requeue path.
- [x] Add red regression against the real HTTP handler (six cases).
- [x] Add missing pre-slot wait and recheck the gate after revalidation.
- [x] Run targeted/full pytest (162 / 1,382 passed), ruff lint/format and diff checks.
- [ ] Land by normal PR subject to actual repository gates.
- [ ] Verify activated runtime separately; never claim source tests fix the live incident.

## Separate findings, not silently included

MiMo's service config names `THROTTLE_PLAN_METER_LANE=mimo:plan` but does not
supply `THROTTLE_MIMO_REPORT`; the independent sidecar is wired to the dashboard
only. This causes the conservative missing-meter fallback. That is a separate
Nix wiring correction. Neither correction proves an upstream rate limit gone,
or establishes a token-per-minute ceiling.
