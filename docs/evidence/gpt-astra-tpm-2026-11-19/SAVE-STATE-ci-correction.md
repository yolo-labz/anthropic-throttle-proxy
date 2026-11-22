
### 19/11/2026: Astra API CI regression and correction

CI rejected PR #94 head 2f9ff863632ee6e6ab1681244240a7399f08050e. The synthetic
six-worker oracle observed 720 tokens against capacity 600: a reservation could
expire while its worker or IPC delivery was delayed. No merge or activation took
place; the first bounded merge waiter ended on that failed gate.

The corrected adapter keeps reservations pending until response headers or a
transport error, then retains them for one full window. Dead-worker discovery
also retains uncertain debt for a full fresh window and checks Linux process
start times for PID reuse. Delayed-header and crashed-sender regressions are
included. Source checks pass 28/28; installed Pi native qualification passes
31/31, zero skipped. Removing admission still fails the same capacity oracle.
These are synthetic checks with real network disabled, not production load.

The final frozen Python suite and refreshed CI are being run. The earlier
3211-test result predates this correction. Live API worker activation remains
pending verified organization/rate-group configuration; provider/model/effort,
authentication, live subscriptions and gateway admission remain unchanged.
