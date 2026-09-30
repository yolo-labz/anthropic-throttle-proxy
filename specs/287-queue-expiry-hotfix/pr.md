## Problem
A proven local queue rejection still killed an unstarted Pi turn after its default 30-minute sleep budget or 32 retries. Native replay reproduces the reported 8-refusal failure; the provider never streamed.

## Change
- Default cumulative wait/rejection ceilings are unbounded; cancellation and explicit finite caller limits remain.
- Only the existing exact loopback/provider/path/status/stamps/body + no-output/zero-usage gate may replay. Auth/quota errors, partial streams and arbitrary 503s are unchanged.
- Reject non-finite numeric Retry-After and chunk waits at Node's timer ceiling to prevent overflow becoming a 1 ms hot loop.
- No server timeouts/caps, credentials, model selection, UI framework or dependencies changed.

## Evidence
- Six native regression cases RED before the defaults change, covering MiMo and ZAI; two timer-safety cases RED before hardening.
- **233/233** native/unit Pi 0.85.1 client tests pass, including >30-minute waits, >32 refusals, identical request body, native retry accounting, cancellation and existing negative/provenance/usage cases.
- **1,404 pytest tests pass**; ruff lint and format pass.
- Logs and execution details: `specs/287-queue-expiry-hotfix/`.

## Rollout / reversal
Client source only. The separate Nix client pin and idle-boundary reload are required; this PR alone does not fix already-running tabs. No Python proxy service restart is needed. Revert this squash commit through a normal PR to restore the previous defaults.

Generator family: OpenAI. Automated review is not claimed; executable acceptance and required repository checks remain mandatory.
