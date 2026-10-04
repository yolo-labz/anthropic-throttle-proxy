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
| 13:0x | pJ | PR305 `305-admission-closure-surface` head **`4ab391b6`** — scope ok (`proxy.py +11/-1`, `tests/test_admission_closure_surface.py +87/-0`) | checks: **ruff+pytest FAILURE, scan FAILURE**; 7 others SUCCESS | OPEN MERGEABLE — **red; pJ fixing (dup fixture/lint)** | detailed caller review + green/review-clean squash merge when eligible (pool down this window: diff review deferred one beat) |
| 13:2x | coordinator | docs delta **`e84e995` -> `b9c217a`** (owner receipt + THRTL plan) | hooks PASS | pushed (279-team-capacity) | save-state current per operator order |
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

## SOURCE-RECONCILIATION (THROTTLER-1315) — 04/10 13:5x BRT

### Public delivery handoff fact for pJ (PR305 — hands off while pJ works)

Hosted CI for PR305 checked **`refs/pull/305/merge` = `27413eb`** (combining
head `4ab391b6` with main **`953cb838`**), but the local 305 base **`c9e4c9be`
lacks the PR304 real-render test**. Before reproducing that missing test,
**normal NON-FORCE base reconciliation is required** (merge/update-branch main
into 305 first). pJ reclaimed 305 source custody and is valid-working —
this seat keeps hands off 305 paths; deliver this note to pJ at its next safe
idle boundary. (Also: earlier "picker held operator drafts" episode noted;
no more prompt-file staging for pJ.)

### Open source pair — overlap + merge ordering (both touch `proxy.py` usage sites)

| PR | Head (current) | Files | Owner/custody | CI state |
|---|---|---|---|---|
| **307 fresh-gauge** — `feat(ui): separate fresh input rate beside the conserved total` | `64108a11` | `history.py +13/-3`, `proxy.py +2/-1`, `ui/signals.py +10/-2`, `stats.html +1/-1`, `test_ratelimit_usage.py +4`, `test_tps_gauge.py +27` | pH: repair + normal merge custody (hosted failure `test_health_exports_real_token_history` expects 2 fields vs measured local-fresh third 0; 1 failed/1989 passed) | red (pH repairing) |
| **306 paired-usage** — `feat(prospective): pair observe usage per attempt` | `ff0cae31` (directive head `d1485d0e` superseded by pM fix) | `prospective_calibration.py +292` (new), `forwarding.py +40/-31`, `metrics.py +19`, `prospective_runtime.py +10/-2`, `proxy.py +29/-24`, `test_prospective_calibration.py +498`, `t005-calibration-plan.md +85` | pM (old-Astra/current fix in progress; **no new Astra turn**; preserve work until existing p5 migration) | ruff+pytest green; **Sonar scan FAILED (quality gate)** at `d1485d0e` — new-coverage/new-violations class on the 292-line module (S3776 precedent #282); pM repairing at `ff0cae31` |

**Merge-ordering recommendation (recorded for the merge shepherd):** land **307
FIRST** once pH's repair is green — its proxy delta is +2/-1 (additive
fresh-rate beside the conserved total; no meaning switch) and it is otherwise
unblocked. Then **306 rebases/updates onto the merged main** — its larger
per-attempt-pairing refactor (+29/-24 in proxy.py) absorbs the tiny fresh-rate
change at the same usage sites. Preserve both invariants across the rebase:
**total/fresh meaning** (total remains the conserved total; fresh is a
separate labelled rate) and **per-attempt pairing** (each retry's usage pairs
with its own attempt context, never cross-attempt). Deep semantic diff review
of both resumes next window (job pool down at review time; scopes above are
from GitHub readback).

### Other heads (unchanged)

- Nix2640 exact **`89b16625`** — native CI pending; **no cosmetic pushes**.
- Live **6ac** unchanged (PID 3873577 ungated; 85s/535s); no supported old gate.
- Authority contract fact (LoopConductor `home:w2N:p5`, busy untouched): native
  Pi `onPayload`+headers run **before SDK retry**, so the transport barrier must
  cover **physical attempts / all producers**.
- Acceptance classes stay distinct: SOURCE (merged/pending PRs) · LIVE
  (unaccepted) · CLIENT (B unaccepted; C1 receipt + private owner) · 24h
  paired (ABSENT — strict OFF).

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
