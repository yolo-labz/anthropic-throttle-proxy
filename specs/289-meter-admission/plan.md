# Credential-bound meter admission — 03/10/2026

## Reproduction and scope

The live MiMo admission endpoint reported `allow=true` and `2/2 bearers serving`
while its fresh report showed exhausted quota. A Mac execution of the real
endpoint fixture reproduced that exact false-open result before integration.
The protected account verifier established existing credential label A maps to
`mimo:team-owner` and B to `mimo:team-b`; production values/keys stay private.

Reuse the existing credential-label metadata and cached meter reader. An
explicit label→row mapping limits each credential to its own fresh meter.
Required mode refuses unknown/unmapped credentials and missing configuration;
malformed mapping never silently disables the feature. Default-off instances
retain their existing behavior. No credential parser, model or account changes.

## Implementation and falsifiers

- [x] Preserve pK owner worktree; transport its frozen four-file draft.
- [x] Shared predicate controls admission claims, account election and the
  pre-dispatch gate. Stale, missing, exhausted or unknown mapped rows refuse.
- [x] Required empty/unknown/unmapped and malformed configuration fail closed.
- [x] Local refusal is503 + Retry-After + `x-throttle-meter-refusal: 1`.
  It is returned before upstream dispatch and does not alter provider AIMD.
- [x] Fake upstream proves exhausted A sends nothing while fresh B serves;
  refused configurations send no request and preserve both concurrency caps.
- [x] Full suite: 1571 passing; focused endpoint/dispatch/config slice: 57 passing.
  Ruff lint/format and diff whitespace pass. Affected-test clone density is
  0.143% (one existing two-line queue setup clone), below the existing 1% gate.
- [ ] Exact-head CI and cross-family integration review.
- [ ] Coherent source/Nix package, protected configuration and fresh runtime
  report/admission/traffic/rollback acceptance.

This is a meter policy at request admission and route selection, not a token
reservation system or guarantee against external spending/vendor429. Already
committed streams retain their session. The read-only dispatch audit found a real recheck gap after queue waits and
on retries. The same predicate now runs for each ordinary/direct retry, each
held-stream retry and every synthetic credential probe. Local provenance
prevents refusal from entering provider retry/AIMD handling. A committed stream
cancels its keepalive emitter and emits one valid terminal SSE error and EOF. Prospective accounting remains Plane THRTL-18/spec245.

pK authored the initial predicate, wiring and endpoint/fake-upstream tests.
Independent Mac review fixed required-mode fail-open and local429 classification
before publication. Runtime has not been activated by this worktree.

## Final dispatch regression — 03/10/2026

Five new local-upstream fixtures cover meter loss while queued, pushback retry,
central-to-direct retry, an already-committed SSE hold, and a synthetic probe.
All pass; full suite1,576 passes, Ruff lint/format passes. The held request
finishes200 while its now-refused queued sibling returns503 without spending
or leaking a lease. Retry fixtures spend exactly once; the stream emits one
error event and drains its hold count. Initial test assertion used a string
against bytes and was corrected; this was a test type error, not a hidden green.

These checks occur at proxy dispatch boundaries, not an atomic vendor quota
transaction. External consumption and usage between cached meter samples
remain limitations; no promise of zero vendor429 is made.
