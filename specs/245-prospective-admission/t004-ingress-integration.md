# T004 ingress integration — seam, tests and honest topology report

Slice: `anthropic-throttle-proxy-301-prospective-ingress`, base `fa6e4e03`
(merged main). Companion to pM's 297 bridge (`prospective_runtime.py`,
published interface in that worktree). **One coherent 297 integration PR** —
this slice is frozen source for it, not a separate publication. Validated by
the Mac coordinator (no desktop tests/builds/paid calls). No
registry/config/metrics/proxy.py edits; merged modules reused
(`prospective_refusal.local_refusal_response`,
`prospective_refusal.strip_incoming_provenance`).

## Files owned here

1. `src/anthropic_throttle_proxy/ingress.py` — the whole delta:
   - `_forward_headers`: hop-boundary strip of incoming provenance claims via
     the merged helper (a caller can never make its own refusal look local;
     well-formed callers are byte-identical).
   - `_prospective_bridge()`: lazy import of the published 297 accessor
     surface (module stays importable while the bridge lands in the same PR).
   - `_send_upstream`: per-attempt seam —
     `set_selected_dispatch(request, SelectedDispatch(credential_source=None,
     endpoint=None, topology="ingress_relay", internal_probe=False))` after
     each routing choice (reroutes refresh it), then
     `async with runtime.reserve(selected, body_data) as permit: permit.handoff();
     return await _send()` — handoff is synchronous and immediately precedes
     transport. `LocalProspectiveRefusal` is caught OUTSIDE the reservation
     context and BEFORE any retry/AIMD/spill handling → merged
     `local_refusal_response(exc.refusal)` (typed local 503). Transport
     exceptions keep their existing shape (`ingress-upstream-unreachable/-timeout`).
2. `tests/test_ingress_prospective.py` — 7 focused synthetic-stub tests.

## Honest topology report

| topology / case | strict | observe | off |
|---|---|---|---|
| **`ingress_relay` (every generation forward)** — ingress relays to a sibling proxy that selects the final credential | **UNSUPPORTED → typed LOCAL refusal before transport** (honest first support boundary) | allowed; bridge records `unknown` and sends | shared no-op; forwards unchanged |
| **streaming / non-bytes generation body** | typed LOCAL denial before transport (bridge classifies UNKNOWN; never silently forwarded as accounted) | allowed; UNKNOWN-classified, sends | forwards unchanged |
| **trusted sibling verdict (relayed refusal from downstream)** | **UNSUPPORTED — documented, not implemented** | unchanged (ordinary response) | unchanged |
| **non-generation control requests** (lane health probes, `/`, `/_health`, `/_metrics`, local synthetic responses) | bypass structurally — never reach `_send_upstream`/the generation API | same | same |

**Authority (prerequisite, not assumption):** the sibling proxy selects the
final credential, so no account authority exists at the ingress hop. Nothing
here derives identity from downstream tokens, bearer hashes, plan percentages,
registries or stamps; `credential_source`/`endpoint` stay `None` and the
topology string is the only claim made. Exact downstream-selected account
authority must arrive as trusted metadata in a future authorized slice before
any ingress hop can be supported in strict. **Trusted-sibling verdict
classification** (marker + configured route identity, never marker alone) is
deliberately out of scope until that authority exists; it must not be guessed.

**No double debit:** with `credential_source=None` and unsupported topology,
observe classifies `unknown` (not admitted/debited) and strict refuses before
transport — neither hop can charge a guessed identity.

## Test inventory (7, synthetic stubs of the published 297 API)

1. `test_off_mode_forwards_exact_final_bytes` — off parity: reserve entered as
   no-op, transport sees the same bytes.
2. `test_strict_ingress_relay_refuses_before_transport` — typed local 503
   (`ERROR_TYPE` envelope), zero upstream calls, no handoff.
3. `test_observe_sends_after_reserve` — observe sends; handoff precedes
   transport.
4. `test_selected_dispatch_metadata_per_attempt` — per-attempt metadata,
   refreshed on reroute; never a guessed identity.
5. `test_inbound_provenance_claims_stripped_and_wellformed_unchanged` —
   forged claims dropped; well-formed headers byte-identical.
6. `test_strict_streaming_body_is_typed_denial_before_transport` — (corrected
   shape) unsupported accounting = explicit typed LOCAL refusal before
   `_send_upstream` transport, zero transport; not silent forwarding.
7. `test_streaming_body_forwarded_with_reserve_in_off_observe` — positive
   control: off/observe forward the body OBJECT with reserve entered
   (classified UNKNOWN by the bridge).
8. `test_transport_error_is_not_a_local_refusal` — transport failures keep the
   existing unreachable/timeout shape (never local admission policy).

(7th/8th listed as separate cases: 8 assertions-level tests total in file.)

## Integration dependencies (for the one PR)

- pM's `anthropic_throttle_proxy.prospective_runtime` (297 slice) — the
  published accessor surface used here (`get_runtime`,
  `set_selected_dispatch`, `SelectedDispatch`, `LocalProspectiveRefusal`).
  Coordination note for pM: the bridge must classify any non-`"direct"`
  topology (incl. `ingress_relay`) as unsupported in strict, and non-bytes
  body objects as UNKNOWN (strict refuses; observe records unknown).
- Coordinator: lifecycle/config attachment (`app[RUNTIME_KEY]`) so the same
  mode reaches ingress; this slice never constructs owners or app keys.
- Mac coordinator: focused/full/lint/CI validation (this seat ran nothing).

## Non-claims

No measured result, no activation, no strict/observe authorization. The
bytes/4 estimator remains uncalibrated. This is source for the bounded
dispatch boundary only.
