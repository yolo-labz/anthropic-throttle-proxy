# T004 — prospective admission refusal plan (local typed payload + 503 factory)

Scope: **smallest immutable typed LOCAL refusal payload + HTTP 503 factory** for the
prospective-admission boundary (spec 245). Default-off foundation only.

Frozen files (this change, nothing else):

1. `src/anthropic_throttle_proxy/prospective_refusal.py` — payload + factory + future
   provenance-stripping helper.
2. `tests/test_prospective_refusal.py` — offline tests (exact response, bounds/types,
   CRLF/spoof stripping, typed payload).
3. `specs/245-prospective-admission/t004-refusal-plan.md` — this plan.

## Contract

- **Envelope** (existing Anthropic-protocol error convention, payload only):
  `{"type": "error", "error": {"type": "prospective_admission_refused", "message":
  "local policy: … (<budget>)", "reason": "<allowlisted>", "budget": "<public label>",
  "retry_after_s": <int>}}`. The actual SSE terminal helper is
  `proxy._emit_sse_error_terminal(sse_resp, message, error_type)` — it takes
  `(sse_resp, message, error_type)`, **not an envelope dict**. An **adapter is required**
  at wiring time (map `message=refusal.message`, `error_type=ERROR_TYPE` into that call);
  no drop-in compatibility is claimed. Structured extras (reason/budget/retry_after_s)
  travel on the JSON 503 path unless a future boundary extends the helper. This module
  writes no streams and invents no writer.
- **Reasons** — finite allowlist `RefusalReason {exhausted, stale, unknown, unbound}`
  (meter-state taxonomy minus `ok`); unknown strings raise.
- **Retry-After** — bounded positive integer `1..86400` (`RETRY_AFTER_MIN_S/MAX_S`);
  bools, floats, strings and out-of-range values raise. Emitted on the 503 as
  `retry-after`.
- **Public budget label** — `^[a-z0-9][a-z0-9._-]{0,62}$`: the validator guarantees only
  **charset and length** (no CRLF/control/quote/`/`-path injection possible by
  construction). It CANNOT guarantee absence of secrets or account metadata, and it
  does **not** reject `..`. The caller must supply an **operator-configured public
  label** (never derived from request data); semantic purity is the caller's
  responsibility. Messages embed only the allowlisted reason + this validated label.
- **Provenance** — module constants stamped by the factory (`x-throttle-refusal-source:
  local`, `x-throttle-prospective-refusal: 1`), distinct headers so a future boundary can
  distinguish a locally minted refusal from upstream statements. **Local authority is
  never inferred from incoming headers or markers** — the factory accepts no request.
- **Immutability** — frozen dataclass; construction validates once; payload is plain
  data afterwards.

## Explicitly NOT here (and NOT claimed as wired)

- **No hot-path import/wiring.** Nothing in `proxy.py`/`ingress.py`/UI imports this
  module; behavior is unchanged (default-off).
- **`strip_incoming_provenance()` is for FUTURE trust boundaries only** (forwarded/
  proxied header ingestion). No boundary currently calls it; the negative spoof tests
  pin the helper, not a wired defense.
- **No metrics, no retry/AIMD, no routing changes.**

## Exact remaining wiring (future steps, in order)

1. At the prospective-admission call site (spec 245 boundary), construct
   `ProspectiveRefusal(reason=<meter state>, retry_after_s=<bounded int from the local
   wait budget>, budget_label=<public label mapped from the lane id, e.g.
   `mimo:team-b` → `mimo-team-b`)`.
2. For HTTP paths: `local_refusal_response(refusal)` (503 + provenance + retry-after +
   JSON envelope). For SSE paths: write the ADAPTER that calls the existing
   `proxy._emit_sse_error_terminal(sse_resp, refusal.message, ERROR_TYPE)` (do not add a
   writer; the helper's signature is `(sse_resp, message, error_type)`).
3. At any future trust boundary that accepts forwarded headers: run
   `strip_incoming_provenance(headers)` before reading/forwarding, then stamp
   `PROVENANCE_HEADERS` locally on anything this proxy mints.
4. Wire a regression at the boundary that asserts provenance cannot be spoofed through
   it (the helper tests here are the fixture for that).
5. Only after the above: consider metrics/AIMD interactions (separate change).

## Verification (Mac)

`pytest tests/test_prospective_refusal.py` — offline; covers envelope shape, frozen
payload (real `dataclasses.FrozenInstanceError` on normal assignment), reason
allowlist, Retry-After bounds/types, budget-label charset/length rejection of CRLF/
slash-paths/quotes/case/length (semantic purity documented as caller duty), exact 503
response, constant (non-request-derived) provenance, and case-insensitive spoof
stripping with CRLF-laden rows.

## Executable acceptance and independent review follow-up

The first Mac run caught an invalid frozen-dataclass test: object.__setattr__
bypasses ordinary frozen assignment. The owner corrected it to normal assignment
and FrozenInstanceError; all38 focused cases then passed. Independent Astra
review found that the factory omitted required budget/reason headers and reused
the plan-meter marker. The integration copy now emits prospective-specific
classification, public budget and reason, with case-insensitive incoming stripping
and exact response/negative-spoof fixtures. Static values never authenticate a
peer; runtime trust must use the configured relay boundary. Privacy and SSE
adapter overclaims were removed. No runtime caller was introduced.

Final Mac acceptance:1,755 full tests pass;38 focused refusal cases pass;
module statement and branch coverage both100%; full-tree Ruff passes;
changed-Python clone scan reports0%. CI remains a separate exact-head gate.
