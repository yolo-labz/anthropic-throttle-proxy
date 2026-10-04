# Owner receipt — portfolio-throttler slice (04/10/2026 03:1x BRT)

Mission Control 03:11 BRT. Coordinator: this seat. pJ stays solely on
published proxy PR298 (red CI does not block independent source delivery).
pM/pF current Astra work preserved. No new seat/Astra review launched.

## Frozen owners and allowed paths (nonoverlapping, frozen BEFORE dispatch)

| Owner | Worktree / branch | Base | Allowed paths | Contract |
|---|---|---|---|---|
| **pH** | `anthropic-throttle-proxy-304-openai-conservation` / `304-openai-conservation` | `b5a08ee0` | `ratelimit.py`; `tests/test_ratelimit_usage.py`; `tests/test_tps_gauge.py`; gauge-label part of `ui/templates/partials/stats.html` if needed | `specs/304-openai-conservation/contract.md` |
| **pK** | `anthropic-throttle-proxy-305-admission-display` / `305-admission-display` | `b5a08ee0` | `ui/routes.py`; ONE new `tests/test_admission_display_trusted.py`; `specs/305-admission-display/route-matrix.md` | `specs/305-admission-display/contract.md` |

Both forbidden: `proxy.py`, `config.py`, `forwarding.py`,
`prospective_runtime.py`, pJ300/pM303 WIP. No runtime/registry activation.

## Frozen contracts (summary)

- **pH:** normalized OpenAI `input`/`cache_read` disjoint + conserve
  `prompt_tokens`; no silent total-vs-fresh meaning switch to shrink a number;
  prove `parser -> record_usage -> history -> gauge` and label truthfully;
  fresh-input display needing `proxy.py` = SEPARATE dependent change, reported
  not edited. Independent conservation fix lands first.
- **pK:** existing local admission predicate ONLY; never provider/HTTP scans
  from render callbacks; never fabricate health; never revive genuinely
  rejected Anthropic capacity; account B from current sanitized pF map
  (binding/local admission exist; actual client path UNACCEPTED — display says
  exactly that); no old B-unknown snapshots; no private account evidence in
  MiMo/public output.

## Verify gate (each exact head)

Red-capable targeted synthetic tests first (pH:
`uv run pytest tests/test_ratelimit_usage.py tests/test_tps_gauge.py`; pK: the
new admission-display test file), then green + ruff + normal hooks, normal
hosted PR CI. No heavy desktop suite/build, no extra CI sweep, no paid provider
call. Off-host acceptance path allowed.

## Source transitions / results (live log)

| Time | Owner | Base -> Head | Test command/result | PR state | Next dependency |
|---|---|---|---|---|---|
| 13:0x | pJ | `305-admission-closure-surface` (in flight; hooks caught dup fixture/lint — fixing) | (4 targeted planned) | **no PR published yet** | my independent scope/caller review + green/review-clean squash merge at pJ's empty-input boundary |
| 11:4x | pK PR304 | head **`dce00540684be13f2fa4b33a75b0c2145ea6c921`** (root-reviewed; tests-only `tests/test_admission_display_trusted.py`; render/index+stats_partial real under socket/urllib/aiohttp tripwires; collector stubbed explicitly) | **9/9 SUCCESS** (incl. scan) | **MERGED by this seat** — squash **`953cb838d87d22a8ef31008cbbc15c54ffe95bf5`** (exact-head verified; review COMMENTED only; no duplicate merger) | none (tests-only) |
| 11:2x | pK | `c9e4c9b` -> 307-render-purity-pin | superseded by PR304 delivery | MERGED via 304 | — |
| ROOT | pH PR302 | `273a5b39` -> `f2e95befe…` | 19 targeted; 9/9 | MERGED (gauge fixtures; real-chain conservation retained; no duplicates added) | — |
| ROOT | pK PR303 | `6110af68` -> `c9e4c9be…` | 9 SUCCESS; 1982/153 | MERGED (client-path requirements named) | — |
| ROOT | Notes2206 | `5243caab` -> merge **`ca076234`** | two PASS / no threads | MERGED (hub; root-owned) | — |
| ROOT/pJ | NixOS PR2640 | pinned candidate/helper (pJ custody) + fail-closed/rollback/foreign-dropin findings delivered | (pJ's) | in flight | **old-runtime closure remains the technical gap** (no permission hold); no coordinator runtime editing |

## Consumer trace (independent, read-only — grounds the pending pJ PR review)

- **`ingress._read_lane_admission`** (696-706) reads `lane.admission_url` (the
  lane's own `/__throttle/admission` verdict) and feeds `_fresh_lane_state`
  (730-737: `capacity_ok = admission is not None and admission[0]`,
  `"admission-open"`). This is a LANE-ENDPOINT reader in the state collector —
  **NOT every producer**. A PR may not claim universal endpoint consultation
  without executable call-site evidence.
- **`routing.py`** (138-163) owns URL derivation: `health_url`/`admission_url`
  default from the lane control surface (`/__throttle/health`-derived suffix).
- **`limiter.py`** carries the shared predicate (`bearer_usable` ∧
  ¬`credential_dead` ∧ `meter_binding_allows`) — PR298's request-handler gate
  remains the ENFORCEMENT source; pure handler tests prove DISPLAYED state only.

## Runtime/live audit facts (no runtime action from this seat)

- Live **6ac** runtime PID **3873577**: ungated; **85s** app shutdown vs **535s**
  tails. Nix2640 exact **`77c7846e`** CI custody with pJ; required flake/server
  check queued in healthy serialized runner — **not green yet**.
- Pi **0.99.1** `before_provider_request` audit: awaits but catches exceptions
  and continues — **a throwing hook is FAIL-OPEN**; installed parity has no MiMo
  gate; shim has no quiesce route. Authority-contract fact for LoopConductor
  `home:w2N:p5` (busy, untouched).

## Pending source (pJ, in flight — no PR published yet)

Worktree `anthropic-throttle-proxy-305-admission-closure-surface`: authoritative
admission allow/state + health `admission_closed`, four targeted tests. Commit
hooks caught duplicate fixture/lint; pJ fixing. **Next action for this seat:**
independent scope/caller review against the trace above + normal GREEN/
review-clean squash merge at pJ's natural empty-input boundary — no duplicate
edits while pJ works; no duplicate merger. Xiaomi/rate allocation goals
retained; private binding stays OpenAI lane; pF/pM Astra holds at p5.

**Source vs live vs client — verified statement:**
- **SOURCE: ACCEPTED** — PR299/301/302/303/304 landed with executed targeted tests + hosted CI; gauge chain conservation real (PR299) + label truthful (PR302) + display contract (PR301) + named client requirements (PR303) + render purity pinned (PR304).
- **LIVE: UNACCEPTED** — no provider/runtime claim made by this seat.
- **CLIENT: UNACCEPTED** — `B client-path-unaccepted` stands; exact artifact (C1 direct-MiMo receipt) + private-owner dependency recorded above; no client success invented.
- **24h paired coverage: ABSENT — strict stays OFF.**
| ROOT-1128 | pH PR302 | `a18c2b4d` -> head **`273a5b39`** -> merge **`f2e95befe4528dd59fd0edb9c934d99939d3c2e1`** | 19 targeted; 9/9 SUCCESS | MERGED | — |
| ROOT-1128 | pK PR303 | `c9e4c9be` merge; head **`6110af68`** | 9 SUCCESS; hosted 1982 pass/153 warn/78.18s | MERGED (`docs(specs): name direct-MiMo client-path acceptance requirements`) | — |
| ROOT | PR300 | `9c077a30` -> merged `a18c2b4d880c1aada59cb349c2d15431f9eb7558` | tested | MERGED | forwarding-side redesign at pM303 custody boundary |
| ROOT/pJ | runtime | pJ resumed (candidate/helper review fixes + NixOS PR custody); old 6ac preflight facts recorded | (pJ's) | — | StageB needs CURRENT running gate; no coordinator runtime action |
| (merged) | pH PR299 | `7f916f61` -> `dad10754` | 16 targeted / 1970 hosted / 9 SUCCESS | MERGED | — |

**Exact client artifact for live acceptance (needs PRIVATE owner):** an
end-to-end **direct-MiMo client acceptance receipt** for route **C1** (real
mimo-desktop client → `POST /v1/chat/completions` directly on the MiMo proxy,
no ingress), with named fields only: client class (non-synthetic), exact route,
timestamp window, streamed outcome + nonzero usage, sanitized. **No tokens,
credentials, raw quota or binding evidence** — those stay in the protected
OpenAI lane. **Remaining dependency:** the private owner (protected lane)
executes/attests the real client path; on receipt, update ONLY the project
receipt + route matrix — no runtime state is required or accepted. Until then
**`B client-path-unaccepted` stands** and no client success is claimed.

**Acceptance classes kept separate:** source · live · client (B unaccepted) ·
**24-hour paired observation — strict stays OFF until actual 24h paired
coverage exists.** Private MiMo binding evidence stays in the protected
OpenAI lane; never in public/MiMo-facing artifacts.

## Done-state bar (not inventory)

At least ONE concrete independent source fix through executed tests and a
normal published PR; shepherd safe-class merge when required checks/reviews/
repository permissions permit. Reviewer findings remain real defects;
automated review is advisory.

## Enforcement log

No main writes, hook bypass, force push, test weakening, new agents, billing,
restarts/deploys or registry widening. Root retains shared hub/Plane/registry
writes; Mission Control owns portfolio-throttler.json.

Contract clarification 03:23 folded into the 304/305 contract files: canonical
`src/anthropic_throttle_proxy/ui/...` paths (short `ui/` = shorthand), pK must
not invent a trusted map/health API nor hardcode B state (route matrix keeps
B client-path-unaccepted; pF map = sanitized receipt, not a schema/verdict).
pH/pK occupied panes preserved — no nudges; handoffs at natural boundaries.
