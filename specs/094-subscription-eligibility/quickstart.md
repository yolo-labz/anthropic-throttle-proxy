# Quickstart: Verify Subscription Eligibility v1 (ADR-6a)

No command in this quickstart calls a live model or changes a service.

## Targeted contract tests

```sh
uv run pytest -q tests/test_ingress_adr6a.py
```

Expected: subscription fixtures pass; direct-key/unattested/malformed-request
fixtures are refused before their fake message endpoint is called; the
credential-mode stamp appears ONLY on constrained 2xx responses (never on
unconstrained traffic); 403 refusal distinguishes `no_eligible_lane` from
`eligible_lanes_exhausted` scoped to the role chain.

## Full local gate

```sh
specs/094-subscription-eligibility/verify.sh
```

The script runs target tests, ruff, the CI-equivalent full pytest suite,
changed-module mypy, wheel build, and a throwaway Docker build. It does not push
an image or mutate production.

## Manual health capability check against the test app

The pytest fixtures assert the `enforcement` object (`credential_mode:true`,
`contract:"adr6a-credential-mode/1"`, `subscription_upstreams_count` +
`subscription_upstreams_digest`) and per-lane
`credential_mode` (+ reason when `unknown`), with no bearer/client topology in
ingress health.

## Falsifiers

After the implementation is committed, temporarily neuter (1) pre-egress
enforcement, (2) the response stamp, and (3) spoof stripping; each corresponding
focused test must fail. Restore the committed files. The exact commands and
failing assertion tails are recorded in the K3 report.

## No-live-canary boundary

Do not send the new request header to the running `:8760` service during this
slice. No Nix pin or service activation is included, so the running process is
expected to remain on the pre-v1 build after merge.
