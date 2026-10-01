# Tasks — seat-capacity acceptance (w1P)

1. **Harness** — `tests/test_seat_capacity_acceptance.py`: recording upstream
   (per-seat attempts/inflight/max-inflight, per-tag arrival + hold events),
   static-key pool fixture (`tp-<uuid>` files in `tmp_path`,
   `ACCOUNT_ROUTING_MODE=least_loaded`, `QUEUE_MODE=fair`), seat seeding that
   clears cold-start probation and pins the live cap. Reuses
   `test_proxy_app._reset_proxy_state` / `_wait_for_limiter_queued` shapes.
2. **T1 retirement** — pool A+B serves on both; B busy (held); A's key file is
   retired; the next request must land on B and complete while B is busy; no
   dispatch to retired A; A's books clean.
3. **T2 spanning** — two eligible seats, concurrent held requests must be in
   flight on BOTH seats simultaneously; both complete.
4. **T3 exhausted seat** — A's unified window `rejected`/`util 1.0`; all useful
   completions land on B; A consumes zero slots/leases.
5. **T4 caps + durations** — A cap 2 / B cap 1, long + short requests; short
   completes while longs hold; B queues instead of over-dispatching; upstream
   max concurrency per seat never exceeds its cap.
6. **T5 queued cancellation** — one busy seat, second request queued then
   cancelled; the queue entry/lease returns, the books balance, the next
   request is served.
7. **T6 per-client fairness** — chatty client X (3) + quiet Y (1) share one
   seat; measured `queued_per_client` state; Y dispatches after at most one
   sibling turn (not behind X's whole backlog); cap never exceeded.
8. **Gates** — run the new module, then the full suite; `ruff check` +
   `ruff format --check` on owned files; record exact outcomes in
   `specs/284-capacity-acceptance/result.md`; commit only owned files.
