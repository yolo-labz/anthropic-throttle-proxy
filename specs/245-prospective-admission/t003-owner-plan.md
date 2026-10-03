# T003 foundation — standalone persistence owner

## Scope and hypothesis

Native metadata verifies worktree `anthropic-throttle-proxy-294-prospective-owner`,
branch `294-prospective-owner`, base `6ac87f11a470d6c130b000a2afb5f931890d9a51`.
No command was executed to obtain this receipt.

Hypothesis: one bounded, serialized worker can persist reservations off the event
loop and roll back only identity-bound, proven-unsent attempts without making a
failed provisional refund available to later admissions.

This is a foundation, NOT completed T003 dispatch wiring or T004 HTTP/SSE policy.
Only five files are owned: `src/anthropic_throttle_proxy/ledger.py`, new
`src/anthropic_throttle_proxy/prospective_admission.py`, `tests/test_ledger.py`,
new `tests/test_prospective_admission.py`, and this note. No other worktree,
production import, setting, budget, provider, scope resolver, scheduler,
dependency, session, tracker or model pin changes.

## Bounded implementation tasks

1. Add provisional identity-checked `LaneLedger.cancel_unsent`: remove both
   request and token debt, reject settled/foreign leases, preserve sequence
   high-water. The caller supplies proof of no transport handoff; the standalone
   owner maintains that proof, not the low-level ledger.
2. Add explicit per-scope configuration and one stdlib worker owning pool load,
   debit/save, cancellation/save and snapshots. Require caller-owned absolute
   state paths, explicit cold-start permission, budgets and canonical scope keys.
3. Bound pending work and each public wait, track abandoned reservation cleanup,
   fault subsequent operations after persistence failure, retain after handoff,
   and drain without blocking the event loop.
4. Author focused deterministic falsifiers, read back all owned files, and freeze
   for Mac. Executable acceptance remains UNRUN in this seat.

## Design contract

- The module is standalone/default-off: importing it creates no owner, thread,
  file, environment setting, network call or production integration.
- One owner is bound to one running event loop and owns one worker thread. Each
  configured canonical `(upstream, account, model)` gets a separate `LedgerPool`
  with its explicit budgets and file. No raw credentials enter this API.
- The caller guarantees exclusive custody of the state files and their parent
  directories across processes/owners. No distributed/file-lock service is
  introduced. Duplicate scope keys and file aliases within an owner are rejected.
  Missing state is allowed only by explicit cold-start permission; corrupt state
  never becomes empty headroom. Parents must already exist.
- `start()` loads on the worker; `reserve(...)` is an async context manager.
  Its ticket is yielded only after successful debit persistence. Call its
  synchronous `handoff()` immediately before entering/awaiting transport, with
  no intervening await. The owner does not send anything or prove physical wire
  delivery. Every outcome after handoff retains debt; no settlement API is added.
- The work bound includes running/queued operations and yielded UNSENT tickets.
  An unsent ticket retains its permit for rollback, so cleanup cannot overflow
  the executor queue. Handoff releases the permit; network stream duration does
  not consume persistence capacity. Saturation refuses immediately, never queues
  a new admission waiter. This is a persistence bound, not an inference scheduler.
- Public waits time out without cancelling worker futures. Cancelled/timed-out
  reservation acquisition schedules tracked cleanup: await its persisted result,
  then cancel/save only if a lease was actually acknowledged. Cancellation in the
  context before handoff does the same; cancellation after handoff retains debt.
- Worker exceptions latch a fault BEFORE the next queued action executes. A
  failed rollback may leave provisional memory refunded, but the fault makes it
  unspendable. There is no reset/retry on this owner. A fresh owner must restore
  persisted state only after the prior owner drains and custody is transferred.
- `aclose()` closes new admission immediately, tracks drain/teardown, bounds the
  caller's wait and surfaces faults. A timeout/cancellation does NOT mean closed:
  the tracked close continues and may be awaited again. The caller must exit all
  unsent contexts and keep the loop alive until cleanup/drain finishes.
- ponytail: Python cannot kill a thread stuck in filesystem I/O. The queue and
  caller waits are bounded; physical fsync completion and process-exit latency
  are not. Do not start another owner against its files on a close timeout.
- Existing 60-second debit-time expiry and wall-clock limitations remain. An
  expired/backward-clock ticket cannot hand off. No vendor charging-epoch,
  calibrated-token, host-restart or physical power-loss guarantee is introduced.

## Acceptance and custody

Authored tests cover identity, RPM/token rollback, high-water/restart, serialized
scope ownership, heartbeat during blocked fsync, bounded pending work (including
unsent cleanup), cancellation/timeout before handoff, retention afterward,
failed debit/rollback persistence, faulted queued admission and bounded close.
These are test definitions, NOT passing results.

Mac owns execution in the admitted development environment, then lint, full
suite, Sonar cognitive complexity <=15, CI and publication. No provider review,
Bash, parsing/eval, test execution, live probe or activation is authorized here.
No date stamp is added because `date` execution is prohibited.

## Freeze receipt — executable acceptance UNRUN

The four source/test files have been read back completely with native reads.
This plan records the final bounded artifact and is the fifth frozen file:

1. `src/anthropic_throttle_proxy/ledger.py`
2. `src/anthropic_throttle_proxy/prospective_admission.py`
3. `tests/test_ledger.py`
4. `tests/test_prospective_admission.py`
5. `specs/245-prospective-admission/t003-owner-plan.md`

The pre-tool gate blocked a proposed test edit before it applied. Its supported
`glm_plan(mode="direct", reason="inseparable")` route subsequently accepted the
existing bounded implementation and already-assigned independent review/scopes
as the reason not to duplicate delegation. No bypass, provider call or new agent
was used. On the coordinator's final instruction to finish only the plan/freeze,
source/test expansion stopped. In particular, the additional concurrent-budget
fixture was NOT added; neither were the proposed startup/submission tests. The
existing bounded-pending and queued-admission-after-failed-rollback fixtures ARE
present and form part of Mac's first acceptance target. Treat the missing extra
fixture as an explicit coverage gap, not a green result or a blocker to transfer.

Mac reports pG has the public owner snapshot `68304499`; that identifier and
review custody are coordinator-supplied, not locally hashed or independently
verified. Review is advisory and did not delay this freeze. All subsequent source
changes should follow Mac's executable findings, not further speculative growth.

Suggested focused acceptance, for Mac only (NOT executed here):

```sh
uv run --offline --no-sync pytest -q tests/test_ledger.py tests/test_prospective_admission.py
uv run --offline --no-sync ruff check src/anthropic_throttle_proxy/ledger.py src/anthropic_throttle_proxy/prospective_admission.py tests/test_ledger.py tests/test_prospective_admission.py
uv run --offline --no-sync ruff format --check src/anthropic_throttle_proxy/ledger.py src/anthropic_throttle_proxy/prospective_admission.py tests/test_ledger.py tests/test_prospective_admission.py
```

Then full suite, diff/CI and actual Sonar <=15 checks. No green, complexity,
publication or runtime claim is made. The spec244 all-dispatch oracle is NOT
expected to turn green from this disconnected foundation. No tasks.md completion
marks or live mode changes are made. All deliverables remain in this feature
worktree; nothing was left in disposable scratch. Older worktrees are untouched.

## Mac execution and review disposition

First focused run:104 cases passed. Mac added the explicitly missing concurrent
request-window test (three contenders, two durable grants, one budget refusal),
and expired/pruned unsent rollback without poisoning subsequent admission:106
focused cases pass. Formatting repaired; full-tree Ruff passes.

The separate-family snapshot review lacked ledger.py. Its missing durable-save
and expired-lease claims are falsified by actual LedgerPool.check_and_debit
saving before acknowledgement and cancel_unsent treating expired legitimate
leases as no-ops, exercised by these tests. Bounded close deliberately reports
TimeoutError while owned work remains, exposes pending, and forbids custody
transfer; it does not interrupt fsync or pretend that an incomplete rollback
succeeded. Public caller cancellation is shielded and covered before/after
handoff and during rollback/close. The review also identifies a limitation:
arbitrarily cancelling private owner tasks/futures (including indiscriminate
loop shutdown) is outside this API contract. Runtime integration must await
aclose before cancelling owned background tasks or closing the loop; do not
claim safety for direct cancellation of internal _cleanups or executor futures.
No runtime wiring or shutdown-order guarantee is added by this foundation.

Final Mac production-source acceptance:1,758 full tests pass;106 focused cases
pass; owner branch-aware coverage92.31% (statements94.52%,branches83.33%);
changed-Python clone scan0%; full-tree Ruff clean. The final fixture cleanup
asserts the fault state instead of silently swallowing expected OwnerFaulted;
focused acceptance was rerun. Native/remote CI is still an exact-head gate.
