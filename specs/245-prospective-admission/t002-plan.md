# T002 — repaired standalone reservation ledger

Task (spec 245 tasks.md, exact):

> T002 Reservation ledger: one process-local ledger per `(upstream, account,
> model)`, 60 s rolling request + token windows, atomic check/debit with no
> await between, debt retention on 429/timeout/missing usage, restart debt, no
> refund for unknown discounts.

## Scope and custody

Only these three transported files are edited in:
`/home/notroot/Documents/Code/yolo-labz/anthropic-throttle-proxy-290-reservation-ledger`

Native reads of `.git`, its worktree `HEAD`/`gitdir`, and the branch ref verified
branch `290-reservation-ledger` at base
`7236d96c1a3ab9c30281829184d415a7ffab5c31`. No Git command was run.

- Default-off standalone module, no hot-path import/wiring, strict activation,
  `prospective.py` edits, provider adapter, or invented vendor budget/scope.
  `Budgets` contains only caller-supplied positive integers.
- One process, ONE calling thread, ONE pool owner per state file. Synchronous
  operations serialize on that owner; absence of `await` is not a thread lock
  or permission to share the file among workers. No concurrency framework added.
- Existing pG worktree, the registry slice, live config, models, sessions and
  tracker are untouched. No private account data or network calls.
- Native reads/edits only. Tests, parsing/eval, lint, complexity measurement,
  publication and deployment belong to Mac. No executable result is claimed.

## Repair contract

### Identity and settlement

- Leases bind to their exact lane key plus a random ledger identity persisted
  with the lane. Sequence high-water is persisted even when all entries expire;
  callbacks cannot alias a new lease after pruning/restart. Active lookup also
  checks the debit timestamp. Identity is an internal correctness guard, not
  authentication against a malicious in-process caller.
- `LaneLedger.check_and_debit` is explicitly PROVISIONAL/in-memory. It checks
  both 60 s debit-time windows and returns `None` on refusal.
- Settlement accepts only `type(spent_tokens) is int` and values >= 0. Validation
  and refund computation precede mutation. Boolean/float/complex zero cannot
  refund or poison a reservation. Repeated completion refunds zero.
- Full known spend replaces the reservation, including overspend: the refund
  is clamped, NOT the liability. Unknown discounts are never credited.
- At age >= 60 s, legitimate late `settle` returns zero and `retain` is a no-op,
  regardless of whether snapshot/another debit already pruned the entry.
  Neither touches a newer lease nor resurrects debt into another window.
  Foreign identity is still rejected. `retain` gives no refund within the window.
- This preserves the specified DEBIT-TIME window policy. It does NOT establish
  a provider's charging epoch for a long stream; that evidence remains a
  prerequisite for future strict activation. Wall-clock jumps are not bounded
  by 60 s: backward jumps can retain debt longer; forward jumps can expire it.

### Transactional restore

State schema is now version 1, with root `version`/`lanes`; each lane has
`key`, `id`, `next_seq`, `entries`; each entry has `seq`, `at`, `tokens`, `settled`.
Validate exact fields/types, finite non-negative timestamps, non-negative
integer amounts, positive unique sequences below the high-water mark, unique
lane keys/identities, and a real boolean settlement flag. Unsettled zero-token
entries are invalid. Duplicate JSON fields are rejected too.

`_parse_entry` and `_parse_lane` validate complete local candidates. `_load`
builds a temporary lane map and publishes it only after ALL rows pass. A valid
first row plus malformed later row must leave the existing map/debt untouched.
Missing/malformed schema is NOT empty headroom. Unversioned prototype files
are rejected, with no silent migration or fallback. A missing file is an explicit
cold-start assumption, not proof of no prior debt: retaining/restoring the file
is a deployment obligation before using this module for real admission.

### Persist before acknowledgment

Use `LedgerPool.check_and_debit(key, input_tokens, output_bound)` for the
persisted boundary. It requires a state path, performs the provisional debit,
and returns its lease ONLY after `save()` succeeds. `None` or ANY exception
means no acknowledgment and MUST NOT result in upstream dispatch. Save failure
retains conservative in-memory debt; there is no automatic refund/retry.

`save()` writes a unique same-directory temporary file, flushes and fsyncs it,
atomically replaces the state file, then fsyncs the directory. Errors propagate;
owned temporary files are cleaned up. Failure after replace can leave the new
debt on disk without an acknowledged request; this is conservative, not success.
POSIX file/directory fsync support is required; physical power-loss durability
is not claimed verified by the fault-injection tests.

Low-level settlement remains provisional: settle, then explicitly `pool.save()`
before further admission decisions. Do not mix unsaved low-level mutations with
the persisted API or treat a returned refund as a durable commit. `save()` with
no state path raises rather than pretending to persist. There is NO request
handler/dispatch integration in this slice; the tests model acknowledgment only.
Inline synchronous persistence is deliberately not activated on a serving loop.

## Owner-authored acceptance

Existing budget/window/refund/restart tests remain, with the old overspend test
corrected to require the full liability. Added deterministic falsifiers cover:

- foreign leases with identical sequence/time, including another ledger with
  the SAME key;
- invalid settlement types without partial mutation, valid integer zero, and
  repeated completion;
- late completion before/after pruning, and pruning + restart + new sequence;
- known overspend persisted/reloaded and subsequent reservation refused;
- missing/malformed root, duplicate fields, bad entry amounts/timestamps/types,
  bad keys/identities/high-water, duplicate lanes/identities/sequences;
- transactional failure after a valid first lane;
- failed file fsync, replace, and directory fsync: zero acknowledged leases,
  conservative memory debt, appropriate old/new on-disk snapshot, no owned
  temporary-file residue;
- missing-path refusal and successful persisted acknowledgment/settlement
  round trips.

The old `islice`/non-coroutine introspection check was removed: it did not prove
atomicity. The contract is a single serialized owner, not general concurrency.

## Owner freeze / Mac integration

Frozen files:
1. `src/anthropic_throttle_proxy/ledger.py`
2. `tests/test_ledger.py`
3. `specs/245-prospective-admission/t002-plan.md` (this file)

All three files have been read back with native reads after the repairs and
falsifiers were authored. They are now frozen; executable acceptance is UNRUN.
No commit, push, PR, tracker update or deploy was performed by this seat. Source
provenance: MiMo-authored original plus OpenAI repair. Date execution was not
permitted, so this repair adds no new timestamp.

Mac, in an admitted prepared development environment, owns:

```sh
uv run --offline --no-sync pytest -q tests/test_ledger.py
uv run --offline --no-sync ruff check src/anthropic_throttle_proxy/ledger.py tests/test_ledger.py
uv run --offline --no-sync ruff format --check src/anthropic_throttle_proxy/ledger.py tests/test_ledger.py
git diff --check
```

Then run repository acceptance/CI. Sonar's actual cognitive-complexity gate is
**<=15**; `_parse_entry`/`_parse_lane` keep nested validation out of `_load`, but
NO measured complexity or green gate is claimed here. Keep checks intact and
repair concrete failures before publication. T003 wiring, calibration, live
restart/long-stream proofs and deployment are not completed by this slice.

## Mac acceptance — 03/10/2026

The frozen owner files were transported to the matching isolated Mac worktree.
Ruff formatting was applied and repeated test setup was shared without changing
production semantics. Exact acceptance: **1,687 full tests passed**; **65 ledger
cases passed**, with **95% branch-aware ledger coverage**. Full-tree Ruff lint
and format checks passed (85 files). Cached jscpd over the two new Python files,
`--min-lines 2 --min-tokens 25 --threshold 1`, reported one two-line test-setup
clone, **0.28%**, under the existing 1% gate. Initial duplication was 3.48%; it
was repaired, not excluded. No live integration or strict activation occurred.

Source provenance is MiMo original plus OpenAI repair and Mac validation.
Repository CI and any concrete review findings remain publication checks;
these local results do not claim an independent review approval. Rollback is
one revert PR because this standalone module has no production import.

CI initially rejected a silent missing-file cold start. It now emits a warning
that no debt was restored; permission and corruption failures still propagate.
The existing cold-start and persistence acceptance cases remain green.
