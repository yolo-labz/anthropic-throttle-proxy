# T003 — forwarding final-body reservation seam (plan + freeze)

Slice: the `_forward_once` **actual final-body reservation seam** and focused
synthetic tests, consuming the #297 bridge API
(`prospective_runtime`, per `specs/245-prospective-admission/t003-runtime-interface.md`
in worktree 297 — read once; absent in this 299 tree by design).

Frozen files (this slice, nothing else):

1. `src/anthropic_throttle_proxy/forwarding.py` — the seam (the only existing-file edit).
2. `tests/test_prospective_forwarding.py` — focused synthetic tests (no network).
3. `specs/245-prospective-admission/t003-forwarding-reservation.md` — this plan.

Not touched: `proxy.py`, `config.py`, metrics, `ingress.py`, the bridge itself, pK/pM
work, any other worktree.

## Seam contract (exact placement)

In `_forward_once`, per actual attempt:

1. normalize → fit → rebind (existing, unchanged) produces the **final body**;
2. `await _pace_dispatch()` (existing) — pacing first;
3. `async with _prospective_reservation(request, body) as permit` — **one reserve per
   attempt** with the final bytes and `get_selected_dispatch(request)` (the routing
   decision's `SelectedDispatch`, passed through untouched — never constructed here);
4. `permit.handoff()` — **synchronous, last statement before `session.request`, no
   intervening await**;
5. the existing transport block (unchanged) runs inside the reservation context.

`_prospective_reservation` is plumbing only: it returns
`prospective_runtime.get_runtime(request).reserve(selected, body)` for POST attempts
carrying a body, else a no-op stand-in. When the bridge module is absent (pre-297
trees) the seam is inert. **No parallel owner/runtime/ledger is implemented here.**

## Semantics enforced by placement (bridge-owned, seam-preserving)

- **Unsent cancellation rolls back; sent/unknown outcomes retain debt:** handoff is
  the commitment point; everything after it (success, TimeoutError, client reset,
  unknown) exits the context with handoff completed → debt retained. Cancellation
  before handoff (including during reserve acquisition) is the bridge's tracked
  rollback. `LocalProspectiveRefusal` from reserve-entry or handoff propagates with
  zero upstream requests (handoff-failure is the unsent case).
- **No double debit on retries:** `_forward_once` = one attempt = one fresh
  reservation context; the caller's retry loop re-enters and reserves afresh.
- **Typed refusal stays distinguishable:** `LocalProspectiveRefusal` (with
  `.refusal` payload) is raised/propagated, never converted here; the caller must
  catch it OUTSIDE the reservation context, before provider retry/AIMD handling —
  so no provider feedback is triggered by local policy. Transport exceptions
  (`TimeoutError`/`aiohttp.ClientError`/`ClientConnectionResetError`) keep their
  existing consumer contract and are never translated into local admission policy.
- **Default off:** wire bytes, headers and status are untouched; no ledger is
  created/read/written; control GETs and body-less calls never enter the generation
  API.

## Tests (synthetic; zero network)

`tests/test_prospective_forwarding.py` — fake bridge (accessor pair only), fake
session/transport, event-order recording:

- strict denial (reserve-entry) → typed refusal propagates, **zero upstream
  requests**; handoff failure → typed refusal, zero upstream, unsent rollback exit;
- ordering: `pace → reserve-construct → reserve-enter → handoff → request → retained`,
  handoff immediately before transport;
- reserve receives `SelectedDispatch` + the **final** body (post-fit);
- one reserve per attempt across caller retries (two attempts → two reserves);
- TimeoutError returned unchanged + sent/unknown retains debt; client-reset
  propagates unchanged;
- default-off (no bridge) → identical wire bytes, `pace → request` only;
- control GET → `get_runtime` never called.

## Exact remaining wiring (other slices)

1. `proxy.handler` catch of `LocalProspectiveRefusal` OUTSIDE the reservation
   context, before retry/AIMD (map `exc.refusal` to `local_refusal_response` before
   HTTP commitment; after SSE commitment adapt `message` + `ERROR_TYPE` to
   `_emit_sse_error_terminal(sse_resp, message, error_type)` — the adapter from the
   T004 plan; stop the keepalive emitter first).
2. Routing populates `set_selected_dispatch(request, SelectedDispatch(...))` on the
   decision (trusted routing metadata only — never caller headers).
3. App lifecycle: `runtime.start()` before serving / `runtime.aclose()` after
   request+probe contexts exit (#297's app-owner duties).
4. Mode selection + scope custody + ledger files: operator configuration only
   (#297); **nothing here enables strict/observe**.

Sonar-style complexity of the seam remains trivial (one guard + one async-with).
No measured result is claimed by this seat; Mac validates and publishes.
