# T003/T004 runtime bridge — consumer interface

## Custody and scope

First artifact in `anthropic-throttle-proxy-297-prospective-dispatch`, verified by
native worktree metadata at `fa6e4e03edfeaf878594f274019d24527d01dec8`.
Only this document, `src/anthropic_throttle_proxy/prospective_runtime.py`, and
`tests/test_prospective_runtime.py` belong to this slice. Consumers in proxy,
forwarding, ingress, config and metrics belong to parallel owners and stay
untouched here. No registry expansion, provider call, execution or activation.

Hypothesis: a thin bridge can resolve trusted final dispatch metadata, reuse the
merged estimator/owner/refusal types, and enforce a persisted handoff only in
strict mode without misclassifying transport exceptions as local policy.

## Concrete API for consumer owners

Import from `anthropic_throttle_proxy.prospective_runtime`:

```python
SelectedDispatch(
    credential_source: str | None,
    endpoint: str | None,
    topology: str,
    internal_probe: bool,
)

ProspectiveRuntime(
    *,
    mode="off",                         # exactly off / observe / strict
    resolver=None,                      # merged prospective_scope.ScopeResolver
    scopes=(),                          # merged prospective_admission.Scope objects
    max_pending=None,                   # explicit positive bound when enabled
    wait_timeout=None,                  # explicit positive finite seconds when enabled
    budget_label=None,                  # explicit operator-owned PUBLIC label
    retry_after_s=None,                 # explicit bounded LOCAL retry hint
)

await runtime.start()
async with runtime.reserve(selected, final_body) as permit:
    permit.handoff()                    # synchronous; immediately before transport
    # enter/await session.request/post HERE, with no intervening await
await runtime.aclose()

runtime.observations()                  # bounded count snapshot, observe mode only

LocalProspectiveRefusal.refusal         # merged ProspectiveRefusal payload

RUNTIME_KEY                            # single aiohttp AppKey, exported here
get_runtime(request)                   # request.app[RUNTIME_KEY], else shared OFF runtime
get_selected_dispatch(request)         # SelectedDispatch | None, request-local only
set_selected_dispatch(request, value)  # SelectedDispatch | None; None clears stale metadata
```

### Shared accessor convention — published for pK/pJ/pG

The coordinator alone attaches `app[RUNTIME_KEY] = runtime` and owns final
configuration/lifecycle integration after consumers freeze. Consumers call
`get_runtime(request)` and never construct their own owners or invent app keys.
An absent app key returns one immutable shared OFF runtime with the same
start/reserve/aclose/observations interface. It allocates no owner, task or
persistence state. A present invalid app value raises TypeError rather than
silently disabling configured admission.

The producer calls `set_selected_dispatch(request, selected)` after each routing
choice and clears it with None when no trusted source is available. The getter
returns None for absent/malformed stored metadata; strict reserve then refuses
UNBOUND. The sole private request key lives in this module; consumers must not
recreate it. The setter rejects values other than SelectedDispatch or None.
Neither accessor reads caller headers, bearer hashes or request payloads. Selected
metadata is immutable; it must be refreshed on reroute/direct fallback, not cached
as connection authority. Off consumers can use these accessors safely without
starting any accounting resource.

`LocalProspectiveRefusal` is an exception. Catch it OUTSIDE the reservation
context, before provider retry/AIMD handling. Before HTTP commitment, consumers
use merged `local_refusal_response(exc.refusal)`. After commitment they adapt
`exc.refusal.message` plus merged `ERROR_TYPE` to the existing terminal-SSE
helper, stop the keepalive emitter first, and do not manufacture upstream usage.
This module does not emit HTTP/SSE, headers, metrics or transport calls.

### Trust and placement

- `credential_source` is the selected operator-config credential-source label
  carried by the routing decision, never a caller header, token, bearer hash,
  quota meter or registry membership. Missing metadata remains unknown.
- `endpoint` is the EXACT configured direct upstream identity used by the scope
  resolver, including any configured base path; no hostname/prefix normalization.
  It must describe the selected target for THIS attempt, not a stale initial route.
- `topology="direct"` is the only supported topology in this slice. Any other
  topology (central, ingress relay, unsupported fallback) explicitly refuses in
  strict mode. `internal_probe=True` also explicitly refuses: no probe carve-out
  is invented. A real user's half-open recovery request is not an internal probe.
- Generation callsites alone enter this context, AFTER their last body/model/auth
  transformation and AFTER pacing. Pass the exact immutable bytes about to be
  sent. No serialization or rewriting of those bytes is performed by the bridge.
  The model is parsed from those bytes, never an earlier request hint.
- Every retry gets a fresh context and metadata. Control GETs and local synthetic
  responses must not enter this generation API. Unsupported body shapes refuse
  under strict rather than borrowing another endpoint's accounting.

### Explicit configuration and authority

Enabled construction requires the merged resolver, nonempty explicit owner
scopes, pending/wait bounds, public budget label and local retry hint. The caller
owns exclusive per-scope file custody and cold-start permission through each
owner Scope. No env/file loader, daemon, watcher, defaults or discovery is added.
Matching resolver budgets must equal the configured owner's canonical-scope
budgets; absent/mismatched custody refuses, never creates another allowance.
Output defaults come ONLY from the resolver match. A wire alias uses its canonical
configured output default without rewriting the wire model or inventing a bound.
The public label names this configured authority; it is never derived from source,
account, endpoint, model or payload. Retry-After is a configured local hint, NOT a
claim about a vendor reset. Plan/credit meters remain completely separate from
these 60-second request/token ledger windows.

### Modes

- **off (default):** a shared no-op async context/marker; no owner, worker,
  accounting task, body parsing, resolver call or file read/write. `start`,
  `aclose` and the marker are no-ops. No policy headers/status/bytes change.
- **strict:** resolve/account final bytes; await the merged owner's persisted
  reservation; yield a synchronous handoff wrapper. Missing authority maps to
  `UNBOUND`, unsupported body/probe/topology or unavailable persistence to
  `UNKNOWN`, and configured ledger denial to `EXHAUSTED`. The wrapper also
  translates owner handoff failure into the typed LOCAL exception.
- **observe:** never refuses or waits for persistence in the REQUEST path.
  Resolve/account using the same rules; only at handoff enqueue a bounded shadow
  owner operation. Record `admitted`, `exhausted`, `unbound`, or `unknown` counts.
  Unknown/dropped/late/faulted measurements are explicitly `unknown`, never
  evidence of headroom. Full pending capacity drops the measurement, not the
  request. No synchronous callback or new scheduler is required for metrics.
  Shadow persistence runs at worker execution time, not physical send time;
  denied and dropped samples do not debit actual spend. Thus observe is a bounded
  would-admit/would-refuse sample, NOT a complete spend ledger or calibration
  guarantee. Drain finishes the retained samples at lifecycle shutdown.

Observe and strict need DISTINCT state-file custody; do not reuse partial shadow
history as strict restart debt. Mode is fixed for this runtime's lifetime.

### Exception, cancellation and lifecycle boundaries

Only reservation entry, synchronous handoff, and unsent cleanup failures are
translated to LocalProspectiveRefusal. An OSError/TimeoutError from the consumer's
transport/body MUST propagate unchanged, not turn into local admission policy.
Cancelled acquisition/unsent exits use PersistenceOwner's tracked rollback.
After handoff, all outcomes retain debt; no settlement/refund from response usage
is attempted by this bridge. Cancellation propagates, never becomes UNKNOWN.

The app owner calls `start` before serving/enabling probe tasks and `aclose` after
request/probe contexts exit, in a finally path that also runs when startup fails.
Strict startup failure propagates; observe startup failure records unknown and
leaves requests unblocked. Close stops new shadow
submissions and drains them, then calls PersistenceOwner.aclose. Waiting is
bounded; cancellation/timeout is NOT proof that fsync stopped. Keep the loop alive
and re-await close before transferring files. No automatic restart/recovery.

## Bounded implementation and acceptance

1. Publish this interface first for parallel consumer owners.
2. Implement only the new bridge using merged ScopeResolver, account_request,
   PersistenceOwner and ProspectiveRefusal; do not edit any merged module.
3. Author focused tests for off/no-touch parity, exact metadata/alias defaults,
   strict refusal and handoff, transport-exception provenance, cancellation,
   observe/nonblocking bounded samples, and lifecycle drain.
4. Native readback and freeze these three files for Mac's focused/full/lint/CI
   validation. Sonar cognitive complexity must remain <=15; no measured result
   is claimed by this seat. Tests/eval/Bash/network/provider calls are prohibited.

The inherited bytes/4 estimator remains uncalibrated. This is code for the
bounded dispatch boundary, NOT authorization to activate strict or observe and
NOT a vendor TPM guarantee. Unsupported probes/topologies remain explicitly
refused in strict until their own authorized integration exists.

## Frozen handoff — executable acceptance UNRUN

Implemented and read back completely with native tools:

1. `src/anthropic_throttle_proxy/prospective_runtime.py`
2. `tests/test_prospective_runtime.py`
3. `specs/245-prospective-admission/t003-runtime-interface.md`

The exported app key/accessors match the contract above. An invalid configured
app value fails loudly; absent runtime uses the immutable shared no-op. Request
metadata setters/getters never inspect headers and None clears stale routing
metadata. Enabled runtime configuration and mode are fixed at construction; no
watcher or runtime provider registry was added.

Authored focused fixtures cover off/no-touch behavior, accessor custody, explicit
configuration validation, canonical alias/default accounting with exact body
preservation, unknown metadata/topology/probes/body refusal, mismatched custody,
exhaustion, unchanged transport exception identity, cancellation before/after
handoff, failed entry/rollback persistence, handoff while closing, observe
exhaustion/nonblocking bounded work, and observe startup failure. These are
unexecuted test definitions, not passing evidence. Shared owner fsync/drain and
ledger invariants are reused, not copied or changed.

Mac owns focused/full suites, Ruff, actual Sonar <=15 measurement, CI and
publication. Suggested first acceptance (NOT executed here):

```sh
uv run --offline --no-sync pytest -q tests/test_prospective_runtime.py
uv run --offline --no-sync ruff check src/anthropic_throttle_proxy/prospective_runtime.py tests/test_prospective_runtime.py
uv run --offline --no-sync ruff format --check src/anthropic_throttle_proxy/prospective_runtime.py tests/test_prospective_runtime.py
```

The coordinator owns final app attachment/lifecycle/configuration after parallel
consumers freeze. This bridge does not prove dispatch-site coverage, full response
byte parity, SSE refusal handling or activation; those require their consumer
integration tests. The synchronous inherited body parser/estimator still incurs
CPU cost when enabled; observe's nonblocking claim is specifically no admission
refusal or persistence wait in the request path, not zero parsing cost.

No modifications to proxy.py, forwarding.py, ingress.py, config.py, metrics.py,
merged prospective modules, original294, sessions or model pins. No Bash, eval,
network, provider calls, builds, live changes, scratch deliverables or fresh date
stamps. Native metadata anchors the base; no local hash or executable result is
claimed. Source/test expansion stops at this bounded freeze pending measured
validator findings.
