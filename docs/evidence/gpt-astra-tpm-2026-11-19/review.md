# Astra API org TPM source review, 19/11/2026

The Spec and Standards reviewers independently reproduced response loss after a
successful synthetic HTTP 200 when another SQLite writer held the database.
The fetch wrapper now retains the response and debit while retrying observation
contention. Delivery and cancellation cases each assert exactly one send.

A later six-process run also found constructor lock contention. The production
adapter and worker fixture now use cancellable asynchronous initialization;
each SQLite busy wait is bounded to 25 ms before yielding the event loop.
Startup lock-release and cancellation have separate regression cases.

Review-time pressure exposed a 10-second fixture deadline and 30-second runner
limit. Individual worker tests now allow 60 seconds and the source runner allows
180 seconds. No token limit, rolling-window assertion or retry hold was relaxed.
Workers rendezvous before the 4M/3.95M capacity observation, so slow startup does
not invalidate the unguarded control by spending the observation window first.

Runtime activation, Nix pins and fleet coverage remain unverified. The user owns
live subscription moves; this work does not change models, auth or admission
policy on the running gateway.

Prior revision: Spec and Standards reported no concrete residual source blocker.
Spec independently checked startup lock release and cancellation. That source
suite passed 26 tests, installed-core qualification passed 29 with zero skipped,
and the frozen Python suite passed all 3211 tests in 95.30 seconds.

CI rejected head 2f9ff863 with three 240-token arrivals in one 300 ms fake
provider window, exceeding capacity600. A grant could age out while its worker
or IPC delivery stalled. Reservations now remain pending through headers/error;
settlement and dead-owner discovery each start a full fresh retention window.
A child-crash regression and delayed-header regression exercise the fix. Spec
review independently confirmed that an unsettled reservation survives five
windows and expires only after settlement plus one full window. Cancellation
after dispatch can wait for SQLite settlement; the debt must stay durable.
The earlier 3211-test result predates this fix and is not final CI proof.

The corrected revision passes 28 source checks and 31 installed-native
qualification checks with zero skipped. The unguarded control still violates
the same capacity oracle. The Python entry point now explicitly requests TAP
so CI does not depend on the Node default test reporter.

Standards reviewed the final pending/settlement/dead-owner revision read-only
and found no concrete blocker. Both reviews preserve the distinction between
source qualification and live activation.

Final frozen-tree verification after the in-flight fix: 3211 Python tests pass
in 77.51 seconds; Ruff check and format pass across src/tests (219 files).
Alignment and code-slop path gates pass. Refreshed PR CI is still required.
