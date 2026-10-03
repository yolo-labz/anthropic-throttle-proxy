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
committed streams retain their session. A separate read-only audit is tracing
retry/probe/queued-dispatch seams; any further protection must preserve terminal
stream semantics. Prospective accounting remains Plane THRTL-18/spec245.

pK authored the initial predicate, wiring and endpoint/fake-upstream tests.
Independent Mac review fixed required-mode fail-open and local429 classification
before publication. Runtime has not been activated by this worktree.
