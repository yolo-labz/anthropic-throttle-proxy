# Reset metadata contract adapter

This isolated source candidate connects the existing TU-02R v2 observation
contract to the corrected v3 readiness function. It makes no network
calls, discovers no production inputs, starts no inference, and writes no data.

The concrete incompatibility is that direct `Bucket(**v2_record)` raises
`TypeError`: four field names differ between the exported contract and runtime.
The adapter maps `limit_scope_id` to `scope_id`, `source_attested` to `attested`,
`clock_skew_bound_seconds` to `clock_skew_bound_s`, and
`restoration_attestation` to `restoration_attested`.

Call `evaluate_observations` with one account's sanitized observation records,
explicit required scopes, frozen evaluation time and the three age/skew policy
thresholds. Every field declared required by v2 must be present; unknown values
remain explicit nulls. Additional fields, runtime aliases and malformed records
produce HOLD. Duplicate records remain visible to v3 instead of being collapsed.
The adapter does not coerce booleans, timestamps or missing values.

The original JSON contract remains byte-for-byte. This v4 integration formats
and sorts standard-library imports in `reset_readiness_v3.py` without changing
its readiness logic or `State(str, Enum)` behavior. The original v3 candidate
remains separate historical evidence; this version uses newly reviewed source
pins. Both dependency hashes are checked before loading the runtime.

A standard `importlib.abc.SourceLoader` reads only the already verified source
buffer for its fixed filename. It rejects other source paths, bytecode-cache
reads and cache writes. The loader cannot substitute changed disk bytes or an
unchecked Python bytecode cache after verification. The workflow caller verifies
all three adapter/runtime/contract pins on every opt-in use and immediately
before recording report provenance, including reuse of an imported adapter.

READY means metadata readiness only. The result always retains null capacity,
cumulative debit and pooled capacity, and `live_admission_authorized=false`.
The adapter does not authenticate a source's assertions. The canonical producer
must independently authenticate evidence before calling it; setting an attested
flag in an arbitrary JSON document does not establish trust.

Run the isolated contract-boundary checks:

```bash
python3 -B -m unittest -v test_reset_contract_adapter.py
```

This candidate leaves the original eighteen reset checks, the canonical
fifty-eight-check evaluator and the SQL repair untouched. Its checks cover schema
compatibility, missingness, wrong aliases, duplicate observations, mixed accounts,
source-clock age, reset restoration, display projections, strict types and a
changed dependency. Two additional checks reject unchecked bytecode substitution
and exercise source replacement after verification. All observations are fabricated.

This is not live integration or a measured savings result. Producer attempt-set
completeness, native/gateway identity, emitter token semantics, independent task
acceptance receipts and authenticated quota/reset observations remain separate
dependencies. Deployment remains with the existing integration/release owner.
