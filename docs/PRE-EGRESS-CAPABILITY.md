# Pre-egress process capability — schema 1

`GET /__throttle/pre-egress-capability?nonce=<64 lowercase hex>` is a fixed
read-only control-plane route. It is reserved for every method before the
catch-all proxy: non-GET is405 and never forwarded. Malformed/missing/duplicate
nonce or extra query keys is400. Unsupported generation/configuration is503.
Every response has `Cache-Control: no-store` and `Pragma: no-cache`.

A successful bounded JSON object contains exactly:

- `schemaVersion: 1`, `kind: "gateway-pre-egress-capability"`;
- `nonce`: exact parsed challenge, not generated or cached by the producer;
- `pid`: actual process PID, captured at import and rechecked on GET;
- `invocationId`: captured systemd `INVOCATION_ID`, exactly32 lowercase hex,
  still equal to the current environment; missing/changed/forked is unsupported;
- `build`: the same immutable imported package directory as `health.build`;
- `modules`: `proxy.py`, `forwarding.py`, `capability.py` → lowercase SHA256;
- `configSha256`: lowercase SHA256 of the canonical effective-input object below;
- `centralUrl`: actual effective central URL, not a discovered URL to follow.
  A credential-bearing userinfo/query/fragment URL is unsupported, not exposed.

All three module fingerprints are captured DURING their imports from canonical
paths beneath the SAME `/nix/store/<32-character-store-hash>-…/` package directory.
Files are read at most1MiB each at import, never rehashed on GET. Mutable-source,
missing, mixed or changed re-registration cannot attest support. A disk file
changed after import does NOT replace its captured loaded fingerprint: the host's
independent live immutable-byte check must still detect that disagreement.

This endpoint makes no provider request, probe, credential/file-content read,
allow/capacity decision, admission mutation or configuration write. It works
during genuine closed admission. It is metadata, NOT authorization to retry a
model call or evidence of budget availability. It does not create an accepted
chain descriptor or enable a host feature.

## Exact configuration fingerprint inputs

The object has these keys:

1. `config`: EVERY public uppercase attribute of `config.py`, except the UI
   metadata maps `EDITABLE_KNOBS`, `ENV_DEFAULTS`, `RUNTIME_OVERRIDES`.
2. `proxy`: EVERY public uppercase proxy attribute/re-export, excluding `M_*`
   metrics, `REGISTRY`, `TYPE_CHECKING` and private names.
3. `editable`: actual value from EVERY registered editable-knob getter, including
   proxy-local and body-shrink settings. Hot overrides therefore change the hash
   even when their values do not live in config.py. No setter is called.
4. `startupRoutingEnvironment`: captured at capability-module import;
   `routingEnvironment`: freshly read on every GET. Both include every
   `THROTTLE_*`, `INGRESS_*`, `CLAUDE_API_*` environment key, plus exactly
   `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, their lowercase forms,
   `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`.

Canonicalize recursively: null/bool/int/string/finite float remain themselves;
Path becomes its string; tuple/list becomes an array; string-keyed dict becomes
an object; set/frozenset becomes an array sorted by each canonical element's
`json.dumps(sort_keys=True)` representation. Reject unsupported types, nonfinite
floats or depth>8. Serialize with Python `json.dumps(sort_keys=True,
separators=(",", ":"), ensure_ascii=True, allow_nan=False)`, UTF-8 encode, reject
more than65536 bytes, then SHA256. Never silently omit an unknown effective
configuration value. Only the digest is returned; input values are not logged.
Credential/account FILE PATHS and environment values participate, not secret
file contents. External credential/meter/quota/live-capacity state is not a
configuration authorization grant and is not read by this endpoint.

## Host join and initial supported route

The host feature stays OFF without p1's independently authorized owner-only
`accepted-chain.json`. On EVERY verification it joins a fresh challenge response
to independent before/after systemd PID/invocation/ExecStart identity, exact
immutable loaded-module bytes and the finite accepted chain/config binding.
`INVOCATION_ID` format alone is NOT independent proof of systemd custody;
synthetic test invocation IDs are never production attestations.

Initial host support is blank/direct central only. Nonblank or unknown actual
central route fails closed; never follow a discovered URL. All actual trusted
hops still require accepted provenance/fatal-auth behavior, not a PR-number
heuristic. Old22 has no endpoint/module; reject it from absent/old descriptor
and expected bytes rather than querying its catch-all as a fallback. Do not
require admission `allow=true`, model probes, quota headroom or perfect pacing.

No packaging, deployment, pA/native source or host-issuer implementation is part
of this producer change. The host owns the join; p1 alone inventories and deploys.

Runnable producer checks: `uv run --frozen pytest -q tests/test_pre_egress_capability.py`.
The tests explicitly separate fixture-only metadata from actual immutable-source
subprocess qualification. Neither is a production systemd/descriptor acceptance.
