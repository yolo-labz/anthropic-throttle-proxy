# Weekly MiMo first waiter

08/10/2026. Base: `10e790a7ea409872b082e145f0d7ca3a2a09062e`.

Hypothesis: an evidenced service estimate longer than the finite wait budget
rejects even the first waiter on the one-slot weekly lane, preventing a sibling
tab from holding a place while a successful request finishes.

Falsifier: the same saturated one-slot limiter with no parked request and the
observed service samples does not raise a pre-queue timeout at 180 seconds.

Root observed service 236.319s, residual 234.54s, budget 180s, inflight 1,
queued 0, served 50. A fresh local journal read also showed one slot, queued 0,
measured service 216.017s and residuals 188.857–215.307s rejected against
179.989–179.999s. Requests then completed successfully. This is queue admission,
not a quota or content refusal. Running queue source at observation:
`/nix/store/939kr6a14jdl692yn675lih7n2b12d42-anthropic-throttle-proxy-0.1.0`.

## Smallest design

No suitable native option exists: disabling the wait bound or changing the cold
service estimate does not solve this measured-service case safely. Add an
environment-only `THROTTLE_QUEUE_ALLOW_FIRST_WAITER`, default false. Enable it
only for the weekly MiMo queue in the separate consumer/pin integration.

On an enabled one-slot normal fair lane, allow one first waiting request past
the predictive refusal only with a finite positive actual wait budget and no
already-queued request. Keep the existing `asyncio.wait_for`, per-client
round-robin dispatcher, leases and cancellation cleanup. No extra upstream slot,
wait-cap change, quota/key/privacy change or reroute. Priority reserve behavior
and other instances' defaults stay unchanged.

Move the prediction check into the existing acquire/enqueue critical section.
The current check precedes `asyncio.wait_for` task scheduling, which can yield
before enqueue; locking the decision with enqueue prevents a burst from claiming
several first-waiter exceptions. Bare acquire callers retain their current API
behavior. Forecast values remain honest estimates; log the explicit first-waiter
exception and publish the enabled option in the limiter snapshot.

## Tasks and executable acceptance

- [x] Reproduce the first-waiter refusal with no network.
- [x] Implement opt-in admission inside the existing enqueue lock.
- [x] Prove sibling gets the next freed slot and the holder cannot jump it.
- [x] Prove a simultaneous burst parks only one predictive exception.
- [x] Prove actual timeout and cancellation remove the waiter without a slot or
  service-sample leak, and a later first waiter can park again.
- [x] Prove default-off, multi-slot and priority paths retain predictive refusal.
- [ ] Run admitted focused checks, normal full-suite CI and hooks.
- [ ] Squash merge proxy PR and export exact source/pin handoff to root.

This is source delivery only. No live activation, Nix edits, shared CI restart,
main worktree edits, stash, hooks bypass, rewritten history or force push.

## Local evidence

The new first-waiter regression failed on the unchanged base with
`QueueWaitTimeout(pre_queue=True)`, one held slot and zero queued. After the fix,
the observed service/residual math is checked at 236.319/234.54 seconds against
180 seconds, while actual timer expiry runs at 0.03 seconds to avoid long waits.
All 53 first-waiter, existing wait and existing drain-admission tests passed
(2.62s pytest, 3.384s admitted job, 50.5 MiB peak, zero swap). Full-source Ruff
lint and format passed for 128 files. Normal PR full-suite CI remains required.
