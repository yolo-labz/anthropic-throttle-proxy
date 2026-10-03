# Coherent default-off integration evidence

Observed on03/10/2026, Mac aarch64-darwin, Python3.14.7. One integration combines
297 runtime,298 route identity,299 forwarding,300 SSE/probes,301 ingress and the
coordinator's startup custody. Original worker sessions/worktrees were preserved.

- Full suite: **1,956 passed**,147 warnings,78.21s with branch coverage.
- Focused actual-handler4cases, ingress4cases, oracle11cases and forwarding14cases
  pass. The off diagnostic still reproduces exactly six named budget failures with
  its original assertions, budgets and three positive controls intact. Its
  counting loopback provider/configuration now serve both off and strict tests.
- Branch-aware combined coverage: runtime93.42%, config96.92%, lifecycle86.67%.
- Lifetime-lock acceptance uses a real second process, proves exclusion and
  subsequent release, and preserves the lock/ledger paths. Replacing its explicit
  subprocess lint suppressions with the existing async process API retained all22
  config/custody tests, including bounded owned-child cleanup.
- The duplicate-code ratchet passes with zero new/expanded clone regressions,
  12 inherited relevant clones and 0.238% repository duplication. The valid
  local execution uses Bash 5 and Nix coreutils; the earlier Bash 3 result
  lacked `mapfile` and is not evidence. Shared probe transport and test setup
  replaced the seven genuine clone regressions without a threshold change.
- Ruff lint/format and git whitespace checks pass. Two final formatting-only
  changes preserved their complete Python ASTs after the full suite.
- Alignment passes for all25 changed code files with zero errors/warnings. Required hosted checks, including Sonar,
  remain publication gates; local test results do not substitute for them.

Concrete review fixes are executable: Retry-After replacement updates/clears the
selected credential source; both probe context chains receive the same runtime;
startup failure stays primary if cleanup also fails; an undrained close retains
custody; prepared SSE stops its keeper before one terminal event; normal local
refusal skips served/retry/AIMD accounting; raw provider provenance is stripped
only when enabled. Default-off headers/body/status remain unchanged, including
untrusted provenance positive controls. Real provider429 feedback remains active.

Strict supports direct configured text/function-message shapes only. Responses,
relay topologies and internal probes refuse explicitly under strict. Observe
preserves those requests and reports unknown. Unknown/dropped shadow samples
cannot establish complete spend or estimator accuracy.

No prospective mode was activated. T005 requires one full day of complete demand
and estimator-error calibration; T006 is a later MiMo-only strict canary. The
independent final TeamB6ac source was already deployed through NixOS2609 with233
native checks. This source integration does not modify that live pin.

Hosted acceptance on the first exact head passed eight checks; Sonar measured
93.3% new-code coverage and zero new duplication, but reported six issues. The
repair documents the intentional immutable off no-ops, keeps ignored accounting
arguments compatible with positional and keyword callers, and uses the standard
attribute default when no prior dispatch exists. The off-path regression now
exercises keyword calls with invalid accounting objects as well. No gate changed.
