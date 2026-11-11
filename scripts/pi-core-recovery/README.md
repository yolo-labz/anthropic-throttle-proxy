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
