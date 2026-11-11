# Guarded native Pi core recovery

The `pi` launcher version does not identify an already running seat. Inspect the
foreground native Pi process's `PI_FLEET_INSTALL` and qualify its actual artifact.
This operational tool currently supports Claude subscription seats only. It never
submits inference, changes a provider/model/effort, cancels a working stream, or
uses API billing.

Qualification checks:

```sh
node --experimental-strip-types --test test-core-upgrade-guard.mjs
python -m pytest --noconftest test_recover_core.py
node check-native-loader.mjs /path/to/installed/core /absolute/core-upgrade-guard.ts
node ../pi-stream-timeout-check.mjs /path/to/old/core reproduces
node ../pi-stream-timeout-check.mjs /path/to/accepted/core survives
```

The loader check uses the actual installed extension loader and runner with fake
native state. The stream check uses loopback mock SSE and placeholder credentials;
it makes no provider request. Run host commands through the bounded launcher.

Discovery is an explicit symlink in `~/.pi/agent/extensions/core-upgrade-guard.ts`
to the reviewed durable extension. It registers two nonce commands and has no
hooks, timers, model selection, or provider calls. Removing the link affects later
loads; it does not unload an already running extension. Never replace a different
owner's existing file.

For one settled seat, retain the exact transcript, cwd, native agent name, selected
provider/model/effort, original bootstrap role/environment and launch flags. If
the original launch recipe cannot be reconstructed, hold the upgrade. Native Pi
rewrites argv, so `/proc/PID/cmdline` alone is insufficient. In particular,
`pane run` does not preserve Herdr's agent-name registration or its role lookup.
The first Gama trial exposed this limitation; further fleet adoption remains held
until role/bootstrap preservation is qualified.

Protocol (all identity arguments must remain identical):

1. `recover_core.py prepare ... --accepted-install PATH --accepted-source IMMUTABLE_SOURCE`
   verifies the source pin, complete installed manifest, native branch, pane identity,
   and immutable launcher. It writes a private, four-minute nonce request.
2. Deliver `/reload` through the visible-editor guard. Core reload preserves queues
   and editor, but installed startup hooks must also be audited for model changes.
3. Deliver `/core-upgrade-state NONCE`. Require the private native receipt to prove
   exact session/model/effort, idle, no pending messages, and zero editor bytes.
4. Run `recover_core.py qualify-bootstrap ... --nonce NONCE` against that state
   receipt. Unknown flags and positional inference input refuse qualification.
   Then deliver `/core-upgrade-ready NONCE`. It repeats native guards synchronously,
   atomically writes a private receipt, then requests graceful shutdown. A successful
   quit can make the delivery helper fail to verify the vanished input box; the
   native receipt and actual process exit are the oracle.
5. `recover_core.py restart ... --nonce NONCE` waits for the original shell PID/cwd
   and old process exit, then launches the exact immutable wrapper against a pinned
   accepted install. It records a single-start marker and restores native name.
6. Prepare a **fresh** request for the new process and deliver only
   `/core-upgrade-state NEW_NONCE`. Confirm actual native selections, bootstrap
   role, same transcript, tab label, and cwd before permitting work.

Expired requests or an undelivered command can be prepared again after inspecting
current native state. A `.started.json` marker must never be blindly retried:
inspect the foreground process and recover verification without another launch.
No continuation prompt is injected; unfinished human work must remain untouched.

`recover_core.py --audit` reads fresh native `pi` processes and actual Anthropic
transport files. Its nonzero result means an affected or unknown cohort remains;
it does not claim that unrelated provider compatibility has been qualified.

The exceptional, manually reviewed Gama registration continuation is completed
incident evidence, not a reusable CLI phase. Expired shutdown admission never
authorizes a later launch. A blocked or uncertain started marker requires a fresh
operator investigation; the tool deliberately offers no automatic continuation.
A shell foreground snapshot alone does not prove an empty human shell draft;
use the supported guarded pane delivery and stop if draft safety is unknown.

The known Gama identity correction uses native Herdr preregistration and an
immutable launcher through a self-removing shell function. The canonical Zsh
`nocorrect pi` alias remains unchanged. This exception is scoped to that exact
seat; it does not qualify unknown original bootstrap flags for other seats.

Historical cross-family targets qualify only when the **latest target on the
active parent lineage** has a matching applied receipt on that same lineage.
Receipt target ID, provider, and actual normalized model must agree; Codex receipts
must name `gpt-6.1-sol`, matching the installed parity startup skip rule. This
permits Plane's already-consumed target without replaying its historical model.
Unapplied targets, another branch's receipt, context-recovery markers, duplicate
IDs and cycles remain blocked. Native selection must still match the operator's
current selection before shutdown and after restart.

The normal upgrade path can preserve any qualified Claude seat whose existing
native bootstrap name exactly matches its Herdr name, whose role/environment are
captured, and whose native launch flags pass qualification. The Gama-specific
identity reconstruction exception is unnecessary for such seats and remains
restricted. Working or compacting seats must reach the native idle boundary;
there is no forced cancellation.

`recover_idle.py` composes that protocol for **one explicitly identified seat**.
Pass the same required identity/accepted-source flags as `recover_core.py`, plus
`--timeout 870` (maximum870s). A nonblocking per-seat lock refuses duplicate
controllers. At least600s must remain before the graceful shutdown command;
otherwise it stops while the old process is still running. Native receipt waits
are capped at20s and restart orchestration at300s. It uses one bounded native Herdr agent wait for an idle/done boundary, then
rechecks exact native identity and requires a visibly empty editor. An idle pane
with a draft is held without spinning or changing its editor. Linux filesystem
notifications await native load and nonce receipts. It sends `/reload`
once and refuses extension commands until a fresh PID-bound private load receipt
proves both command registrations completed. Its random generation must differ
from the baseline and its guard version must match; rendered toasts are ignored. Native receipts, bootstrap qualification, graceful quit,
restart and a fresh native state receipt follow. No work continuation is sent.
A delivery helper exit or timeout is uncertain: only a fresh native load
generation or matching native nonce receipt permits progress. Each command
is sent once. Missing proof, a global timeout or changed identity stops the
controller; inspect nonce receipts and started markers before another attempt.
This is a reviewed single-seat operation, not broad fleet restart authorization.

A failed or timed-out `pane run` response is an uncertain launch result. The
helper never resends it: one bounded native detection must prove the original
session and cwd before registration and subsequent native state verification.
If detection cannot prove that identity, the started marker remains and recovery
stops for explicit diagnosis. The two 11/11/2026 infrastructure trials established
that a nonzero response can follow a successful core launch.
