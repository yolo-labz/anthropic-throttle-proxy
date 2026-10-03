# T003/T004 slice — SSE seam + internal probes + local-refusal classification

Scope (owner: 300-prospective-sse-probes, base `fa6e4e03`): `proxy.py`
`_forward_once_into_sse`, `_probe_upstream_auth_once`, `_credential_recheck_one`
(+ their loop thread-through), and local-refusal retry/AIMD **classification** in
`_keepalive_one_attempt`. Focused tests + this plan. NOT mine: pK298 owns the
`SelectedDispatch` target refresh inside `_retry_direct_once` (identity line);
pJ299 owns `forwarding.py`; coordinator owns final lifecycle/config wiring and
explicitly merges adjacent imports. No whole-file copies across owners. No
account-route functions, config, metrics or ingress edits. No provider calls.

## Exact signatures required for probe runtime injection (coordinator request)

Request-less probe seams cannot use `get_runtime(request)`. They receive an
explicit injection carrier (defined in `proxy.py`, frozen):

```python
@dataclass(frozen=True)
class ProbeProspective:
    """Explicit runtime injection for request-less internal probe seams."""
    runtime: ProspectiveRuntime | None   # app/lifecycle-resolved; None ONLY when genuinely unconfigured
    selected: SelectedDispatch           # internal_probe MUST be True

    def reserve(self, final_body: bytes): ...   # runtime.reserve(selected, body) or transparent off-passthrough
```

Seam signatures (keyword-only, default `None` = genuinely-unconfigured ONLY):

```python
async def _probe_upstream_auth_once(*, prospective: ProbeProspective | None = None) -> None
async def _credential_recheck_one(bid: str, token: str, *, prospective: ProbeProspective | None = None) -> None
async def _credential_recheck_once(*, prospective: ProbeProspective | None = None) -> None   # thread-through
```

**Mandatory lifecycle rule:** any owner of an enabled (`observe`/`strict`)
`ProspectiveRuntime` MUST pass
`ProbeProspective(runtime=<that runtime>, selected=SelectedDispatch(
credential_source=<operator label>, endpoint=<exact configured probe target>,
topology="direct", internal_probe=True))`. Internal probes are NEVER defaulted
to off while the main runtime is enabled. Strict mode explicitly refuses
`internal_probe=True` at the bridge (no probe carve-out is invented): the seam
maps that `LocalProspectiveRefusal` to **LOCAL INCONCLUSIVE** — no
`note_upstream_auth` (never quarantine a valid credential), no `_clear_bearer_
credential`, no fabricated probe budget; `_credential_recheck_one` only touches
the recheck cadence (`_touch_credential_check`). Transport exceptions propagate
unchanged (only the local prospective exception skips provider feedback).

## SSE seam (`_forward_once_into_sse`)

- Trust boundary: `headers = strip_incoming_provenance(headers)` (merged
  T004 helper) before forwarding; client-minted provenance stamps never pass.
- Reservation/handoff context: `async with runtime.reserve(selected, body) as
  permit: permit.handoff()` then `session.request(...)` — post-pacer, exact
  final bytes (`body`), no intervening await. `LocalProspectiveRefusal` from
  reserve entry or handoff propagates OUT of the seam (it is raised outside the
  transport `try`, so it can never be misread as a network error).
- `json=payload` → `data=final_bytes` where `final_bytes = json.dumps(payload)
  .encode()` in probe seams: byte-identical to aiohttp's `json=` serialization,
  and the reserved bytes are exactly the sent bytes.

## Classification (`_keepalive_one_attempt`) — mirrors the meter-binding precedent

`except LocalProspectiveRefusal` (outside the reservation context, before any
retry/AIMD handling): `await cancel_keepalive()` (keeper off FIRST) → exactly
one existing terminal via `_emit_sse_error_terminal(sse_resp,
refused.refusal.message, PROSPECTIVE_ERROR_TYPE)` → `attempt.final_status=503,
response=sse_resp, meta=None, captured=None` → `return sse_resp` (stop: no
retry loop). No `_aimd_feedback`, no `_keepalive_apply_origin_aimd`, no hold,
no spill. Genuine provider 429/503 paths are untouched (feedback/retry
preserved). Before commit (an unprepared response) the typed 503 is the merged
`local_refusal_response(exc.refusal)`; no unprepared caller exists in these
seams today — the contract is pinned by test for future callers.

## Lifecycle note

Orderly `aclose()` before blanket task cancellation remains the owner contract
(297/294 docs); the keeper stop here is the targeted `cancel_keepalive`, never
task-group cancellation.

## Verification (Mac)

Focused: `tests/test_prospective_sse_probes.py` — probe refusal maps to LOCAL
INCONCLUSIVE (no verdict/quarantine calls), `ProbeProspective` validation,
keeper-stops-before-exactly-one-terminal ordering, provenance stripping at the
SSE seam, typed 503 pre-commit contract, signature keyword-only injection.
Tests importorskip the 297 bridge when absent on a standalone branch; on the
combined tree they run for real (exact source limitation reported at freeze).

## Known source limitations at freeze

- `prospective_runtime` (297, pM) is being authored in parallel; imports of
  `LocalProspectiveRefusal`/`ProspectiveRuntime`/`SelectedDispatch`/
  `get_runtime`/`get/set_selected_dispatch` follow the published interface and
  require the combined tree.
- pK298's `_retry_direct_once` identity refresh is intentionally untouched.
- No metrics changes; local-refusal outcomes reuse the existing
  `M_KEEPALIVE_HOLDS(outcome="errored")` precedent (separate counting needs a
  metrics-owned change).
