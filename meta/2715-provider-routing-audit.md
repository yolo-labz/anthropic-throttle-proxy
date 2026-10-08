# Provider routing and health — 08/10/2026

Scope: fresh `2715-provider-routing-audit`, based on merged #331
`9b2db9529b514620ba15a1cb84d67164884663a0`. Generator family: OpenAI.
2714 owns UI/catalogue projection; root owns Nix consumption and activation.

Hypothesis: invalid upstream configuration can retain a healthy egress verdict,
the fleet collector discards a reachable proxy's JSON diagnosis on HTTP 503,
and expired quota snapshots retain an affirmative exhausted verdict.

Falsifier: synthetic fixtures must reject invalid URLs without DNS/network,
retain diagnostics on a proxy health 503 while preserving `ok=False`, and
replace expired or uncertified quota verdicts with stale/unknown. Fresh sibling
readings and actual refusal/error diagnoses must remain distinct.

## Specification and plan

Preserve existing config and health schemas, registry schemas, routing targets,
caps and grants. Reuse the current cached background DNS check, fleet parser,
shared lane/bearer eligibility helpers and lane clock normalization.
Respect a row's existing optional `sampledAt`;
an absent row timestamp uses the report clock for legacy producers.
The 2714-agreed optional row fields are `sample_age_s`, `sample_observed_at`
(DD/MM/YYYY HH:mm UTC), and `sample_interval_s`; unknown source clocks are null.
No new provider requests, canaries, secrets, billing or live controls.
Constitution II/IV/V apply: no credentials in evidence, cheap local health,
environment-derived routing. No SDK or dependency additions.

## Tasks

- [x] T001 Trace runtime configuration, admission, report clocks and source pins.
- [x] T002 Add failing checks in `tests/test_provider_health_truth.py`, `tests/test_fleet.py`
  and `tests/test_lanes.py` for the precise hypothesis.
- [x] T003 Fix existing backend seams in `proxy.py`, `fleet.py`, `lanes.py`,
  `routing.py`; tests reproduce `_anon` reopening a refused lane and quiesce
  being ignored by unconstrained routing.
- [x] T004 Run focused acceptance and lint in an admitted desktop small slot;
  run full pytest through repository CI. Never use the occupied server reserve.
- [ ] T005 Normal hooks, PR, exact-head CI, review findings and squash merge.
- [ ] T006 Export merged source identity and scoped Nix adapter to root;
  provide one receipt and short canonical Notes handoff text through root.

## Read-only findings

- Anthropic PID 2200060, :8765: `http://127.0.0.1:1`, central empty, served 0,
  configured max 5, admission 0/1 serving. Persistent
  `95-cancelled-offline-upstream.conf` explicitly says all subscriptions were
  cancelled and contains the loopback sink; this is intentional containment.
  `upstream_egress_ok=true` means DNS resolution only, not TCP/auth/inference.
  The primary provider row's `ok=True` describes the HTTP dashboard process.
  Do not re-enable the route or infer a funding recovery from these signals.
- Z.AI PID 3842786, :8766: `https://api.z.ai`, max 12, historical served 8020,
  no current serving bearer. Fresh report: 7d used 100%, remaining 0; 5h used 0%.
- Monthly MiMo PID 3873577, :8773: Token Plan upstream, max 20, historical
  served 2081, 0/2 serving. Fresh plan/owner readings both exceed 100%; Team B
  unknown. These independent allowances cannot borrow Desktop weekly quota.
- Weekly MiMo PID 3703360 bridge :8100 and PID 2808043 queue :8774 remain
  protected. Queue points at :8100, max 1, observed natural completions and
  `queue_allow_first_waiter=true`. Independent weekly report: remaining 70.5%.
- Legacy shim PID 1980 stays protected; no route or process changes here.
- Ingress PID 3843155, :8760, still runs `jmak9s5…`, configured Anthropic :8765,
  GLM :8766, retired Kimi/DeepSeek, default :8765 and a Codex lane. Its health
  advertises Anthropic/GLM open while their own admissions are closed. The
  shared `lane_usable` helper counted `_anon` and `bearer_usable` ignored a
  refused credential, reproducing both false-open decisions from captured
  health. Fix the shared predicate once; no ingress config change is needed.
- Source drift: weekly queue's six backend files match #331. Anthropic's
  `proxy.py`, `forwarding.py`, `routing.py`, `lanes.py`, `metrics.py` match,
  `config.py` differs. Z.AI/monthly run older `jmak9s5…` backend, with only
  `routing.py` matching #331 among those six. Effective service stores match
  health import paths; base units and persistent override chains retain older
  stores. Root must scope consumption, rather than restart this fleet.
- Nix source pin is still `b5a08ee…` in `pkgs/anthropic-throttle-proxy/default.nix`;
  runtime overlays and the root-owned fr2l0 public parity artifact are separate
  evidence surfaces. Nix #2697 is MERGED as `3909c57f46bdb61ae8b1f2cea08d7e652e68c650`.

Review constraint: the operator forbids vendor requests in this task, so no
external model review call is authorized. Automated review is advisory under
the standing instructions; executable acceptance and real branch gates apply.
Host activation and fresh live verification remain root-owned work.

## Acceptance — 08/10/2026 14:55 BRT

The first regression run reproduced invalid-URL, lost-503 and stale-exhaustion
failures. A test fixture initially used the wrong Codex ID; corrected it before
accepting the per-row clock falsifier. Targeted checks then passed 116 tests.
The shared main-worktree venv was older than #329's security lock, so the first
full run failed four multidict checks and a subprocess importing that venv;
the obsolete no-host-success expectation was also updated. No shared venv edits.

`uv sync --offline --frozen --group dev` installed the lock in this worktree,
including multidict 6.9.1. Final focused acceptance: 242 passed. Final full
pytest: **2174 passed**, 290 warnings, 93.18 s. Ruff lint passed; format check
reported 129 files already formatted. Admitted desktop small-slot limits:
1 CPU, 512 MiB, no swap, 120 s; measured peak 161.8 MiB, CPU time 26.687 s.
No server work and no vendor requests.

Running the corrected pure helper on observed health closes Anthropic and
Z.AI (`no-usable-bearer`); weekly remains open. Monthly health alone still
cannot prove quota: its fresh authoritative admission refuses 0/2 serving,
and monthly is not an enrolled ingress lane. Root should keep those evidence
layers distinct rather than infer quota from the DNS/auth snapshot.

## Root adapter and Notes handoff

One result receipt: `meta/2715-provider-routing-audit.md`, with the allowlisted
read-only machine evidence in
`docs/evidence/2715-provider-routing-2026-10-08/receipt.json`.

Only these runtime files changed, under `anthropic_throttle_proxy/`:
`proxy.py`, `routing.py`, `fleet.py`, `lanes.py`. No Nix wiring change is needed
to select routes, caps, grants or endpoints. Consume these files from the
merged revision through root's existing scoped runtime adapter; preserve #331
`config.py`/`limiter.py` for the weekly queue and preserve the independent
fr2l0 parity closure. Do not replace shared/root worktrees or change the base
Nix pin as a side effect of this backend-only delivery.

Root's eventual activation must compare health.build, effective and persisted
units, file hashes and admission, while protecting the original fleet. No
activation or restart is authorized to this lane. Provider billing/quota and
Anthropic's intentionally cancelled configuration remain external conditions,
not repaired funding or recovered provider capacity.

Canonical Notes handoff text for root:

> 2715: backend source fixes reject invalid upstream URLs, preserve a native
> health-503 diagnosis, exclude `_anon`/quarantined credentials and quiesced
> lanes from shared route eligibility, and export independent per-row sample
> clocks (including Codex sampledAt). Fresh monthly/Z.AI exhaustion remains
> distinct from Anthropic cancellation and weekly 70.5% remaining. 2174 tests
> and ruff pass in admitted desktop limits. Four-module scoped consumption is
> root-owned; original seven PIDs unchanged, no live activation/provider calls.

Source merge/CI identity and exact exports accompany the final result; this
receipt does not claim activation or provider recovery.
