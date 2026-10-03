# T003 route identity — plan and trust limitations

## Hypothesis (confirmed in `fa6e4e03`)

`_claim_route` discarded `_account_label`, `_reroute_from_base` discarded the
routing label (`bid, _, claimed = …`), and `_retry_direct_once` discarded
`pick_target`'s via (`retry_url, retry_timeout, _ = …`). Selected credential
source and per-attempt target identity never reached request-local dispatch
metadata, so strict scope could not be trustworthy.

## What this slice does (route/identity only)

- `_selected_dispatch(via, credential_source=…)` — pure builder for the
  published `prospective_runtime.SelectedDispatch` (no state, no accessor
  authority): endpoint is the exact configured base for the selected target
  (`config.CENTRAL_URL` central / `config.UPSTREAM` direct, untouched strings),
  topology is `"central"` for central relays (never pretending direct
  authority), `internal_probe=False` (a real user's half-open recovery is not
  an internal probe). An unrecognized target records `endpoint=None` and the
  verbatim topology — explicit unknown, strict then refuses.
- `_claim_route` records the routing decision's selected operator account
  label (missing → `None`), this attempt's exact target.
- `_reroute_from_base` REPLACES the record with the new routing label — stale
  authority is cleared; a label-less replacement records `None`. Never derived
  from a caller header, bearer hash, token contents or a label map.
- `_retry_direct_once` refreshes endpoint/topology for the target it actually
  selects (branch `direct_target` → direct; branch `pick_target` re-pick →
  whatever topology that pick returned; same-URL retry → same via) and
  preserves `credential_source` ONLY from the existing request-local record,
  because this fallback never replaces the credential (same headers). No
  record → `None`.
- Accessors used exactly as published: `get_selected_dispatch(request)` /
  `set_selected_dispatch(request, SelectedDispatch(...))`; no alternate key or
  helper authority, no config/metrics edits, default-off behavior preserved
  (recording is inert metadata the bridge consumes only in observe/strict).

## Trust limitations (remaining)

- The source label is trusted only as returned by the routing decision; any
  path that replaces credentials WITHOUT authoritative selected metadata clears
  the source to `None` (unknown), never guesses.
- Endpoint truth is the configured base string only; base-path variants are
  whatever `config.UPSTREAM`/`CENTRAL_URL` literally contain (no normalization
  by design).
- Topology is identified for `central`/`direct` targets; anything else is
  recorded verbatim as unknown to the bridge and must refuse under strict.
- Probe carve-out remains unwired; `internal_probe` is always `False` here.
- Strict/observe/off enforcement, reservation and refusal policy belong to the
  #297 bridge and T004+; this slice only records metadata.
- Integration dependency: `prospective_runtime` (#297) must land alongside
  this slice; focused tests fake the published accessor contract.

## Files owned here

`src/anthropic_throttle_proxy/proxy.py` (route/identity sites only),
`tests/test_route_identity.py`, this note. SSE/probe/response functions and
every other owner's WIP untouched.
