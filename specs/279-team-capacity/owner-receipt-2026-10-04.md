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

## SOURCE-COHERENCE (THROTTLER-1405) at merged head `beca493e` — 04/10 14:1x BRT

**Actual merged order (supersedes earlier plan; no duplicate merges):**
1. **306** `8acd16183114e2352e3d6ceba276c72231432c64` (head `ff0cae317`) — 2040 tests, 9 green — 13:50
2. **307** `ee3778aa2e57d1c54797a549cff950855b4b4e34` (head `27b23ef5`) — 2042 tests, 9 green — 14:01
3. **305** `beca493e8bb23c818c3a725a8ae2153ac1037e1a` (head `766a6027`) — 2045 tests, 9 green, 4 files, 0 threads — 14:05 (portfolio-throttler normal merge)

**Final coherence review at `beca493e` (source-only; hosted evidence read):**
| Property | Verdict | Evidence |
|---|---|---|
| proxy usage sites retain fresh-input accounting + paired-per-attempt collector | **COHERENT** | `history.observe_tokens(out, in_, fresh)` (110-121) carries the fresh subset; `prospective_calibration.py` = "Observe-only numeric pairing; no content exports, ledger access or policy" with duplicate-key-safe parsing (`_unique_object`); 306 paired-per-attempt + 307 fresh additive merged in order 306→307 with tests rising 2040→2042→2045 |
| total conserved | **COHERENT** | `history.py:67-69` "TOTAL tok_in meaning untouched"; `in_` = fresh + cache reads + cache writes; fresh is the uncached subset — additive only |
| sibling-unknown preserved | **COHERENT** | `lanes.py:15` "UNKNOWN IS NOT HEALTHY… reports unknown with a reason"; staleness→unknown (174) unchanged by the pair |
| render no new network | **COHERENT** | PR304's tripwire tests (socket/urllib/aiohttp, root-reviewed on real index/stats_partial call sites) survive all three merges; 305's 4 files did not touch the display tests |
| real HTTP closure/refusal/control/cancel source-accepted | **COHERENT** | `tests/test_admission_closure_surface.py`: `proxy._admission_closed` state, `test_health_reports_admission_closed_state` publishes `admission_closed` affirmatively; 305 merged clean (0 threads, 2045 green) |

**pJ record (coordinate at natural boundary):** pJ valid MiMo resumed minimal
coherent additive candidate reconcile/build/normal CI (**no full host build**)
because the current `a18` package lacks 306/307. Nix2640 current **`89b`**
native CI pending; necessary functional pin may change head but **no cosmetic
reset**. pJ used a **forbidden `-f` flag during fixup**; published `4ab`
ancestor `766` preserved; future ordinary push/no-force corrected at idle
boundary.

**Other:** Live 6ac unchanged; **all-producer request barrier still needed**
(Pi native `onPayload`+headers run before SDK retry). Loop p5 busy untouched.
Client proof/private binding stays OpenAI; pF/pM current Astra finish then Sol
migration before any new turn. **Strict OFF until real paired 24h.**
**Notes2220 MERGED `ddc02173`** (lessons; root owns the consolidated note).

## SUPPORTED-FETCH-REHEARSAL (1437) — seam + dependency record

- **Supported seam (exact):** `options.fetch` — `anthropic-messages.js:379`
  passes it into `createClient`; `:393` retries `client.beta.messages.create`
  with SDK `maxRetries: 0` inside `retryProviderRequest`'s per-attempt
  callback. A reusable fetch-level hold rides this seam and gates EVERY
  physical attempt without retry/SSE reimplementation or ID changes.
  **Header placement RESOLVED:** `Models.applyAuth` 438-444 awaits
  `transformHeaders` then STRIPS before `provider.stream` — headers are
  once-per-provider-call, outside physical retry (contract doc `1547aee`).
- **Rehearsal assigned to pK** (fresh done/blank boundary verified):
  worktree **308-fetch-hold-rehearsal** (base `beca493`), one bounded
  source-only prototype + 8-point loopback verification with real installed
  SDK (first attempt allowed; close before internal retry; zero wire sends
  until resume; slow body closed pre-upload; held cancel releases; admitted
  SSE preserved; resume negative control; failure exits nonzero). Synthetic
  key/payload only; no provider traffic; no native provider registration or
  live extension loading. Nonoverlapping new dir (no pJ/305/306/307/Loop paths).
- **Remaining dependency for busy LoopConductor `home:w2N:p5` (NOT prompted,
  WIP untouched):** safe loader acceptance for the fetch wrapper (when/how the
  extension-side loader may install it through the accepted reload boundary)
  and the **all-producer census** (P1-P6 classes enumerated in
  `native-transport-contract.md`) must be confirmed against the loaded runtime.
  Source rehearsal alone authorizes NO live-gate/client/24h claim.

## 309 REHEARSAL MERGED + CONCRETE DEFECTS (1505) — 04/10 15:0x

- **PR309** `d47da000` -> MERGED `bdd918bc` (9 green, verified). **Inert source
  remains NOT live-barrier-accepted.** Independent source-check 732992 at `d47`
  found three concrete defects (mechanism acceptance WITHHELD until fixed):
  1. **held/reheld microtask race** — `gate.hold(); call=gate.fetch();
     gate.resume(); gate.hold(); await call` => `gateHeld=true`,
     `innerFetchCalls=1`, OS exit 31: `gatedFetch` never rechecks `held` after
     its wait resolves; the call proceeds while re-held.
  2. **Named retry is not physical retry** — the rehearsal calls
     `client.beta.messages.create(...,{maxRetries:0})` twice separately; real
     same-call physical retry must run through the builtin Pi 0.99.1
     `retryProviderRequest` (SDK maxRetries 0) against synthetic loopback:
     first 500 allowed -> close gate before the actual native retry -> zero
     second wire dispatch until resume -> retry completes.
  3. **AbortSignal listener leak** — resumed waiters never remove the listener;
     cleanup needed for both held and resumed paths.
- **Fix assigned to pK** (fresh done/blank boundary): worktree
  **310-gatedfetch-race-fix** (base `bdd918b`, nonoverlapping). Scope: the 3
  defects + deterministic resume/immediate-rehold negative regression +
  aborted held/resumed waiter cleanup + existing slowbody/SSE/resume checks +
  failure-exit proof without vacuous forced-throw acceptance. Preserve
  admitted SSE + cancellation; never change IDs/billing/provider registry/
  live loader; no retry/SSE reimplementation. Normal exact-head PR (tests+
  hooks+CI+review+merge). **Mechanism acceptance withheld until these pass.**
- **pJ:** Nix2640 head **`89d5`** pending native CI; installed HTTP **5/5 +
  fault3 accepted**; no duplicate runtime edits from this seat.
- **LoopConductor loader/all-producer census:** busy, untouched.
- **Publication note:** subsequent 279-doc commits are the published
  coordination feature branch — NOT new normal-PR acceptance. Canonical domain
  note **2245** owns lesson delivery.

## 1535 RECONCILIATION — producer paths × accepted fetch primitive (Pi 0.99.1, public read-only)

Version/path-backed facts (installed `@earendil-works/pi-ai` under
`pi-coding-agent@0.99.1`, `_npx/78709cf4b2f1011c`):

| Producer path | Transport (exact) | `options.fetch` coverage | `options.client` bypass | Physical-retry locus |
|---|---|---|---|---|
| main generation | `api/openai-completions.js:186` (MiMo) / `api/anthropic-messages.js:380` | covered **iff fetch supplied** (core `sdk.js:185-197` `buildRequestOptions` sets NEITHER `client` nor `fetch`) | **openai-completions: none** (only `options?.fetch`); **anthropic-messages:363 BYPASS** (`client = options.client`) | `retryProviderRequest` (provider-retry.js:75 `for(;;)`) around `create(..., {maxRetries:0})` — openai-completions.js:195-197 / anthropic-messages.js:391-394 |
| warm (`cacheWarmer.start`) | same streamFn/requestOptions (sdk.js:~246) | same as main | same | same |
| compaction/summary | own routing ids, same streamFn | same as main | same | same |
| task/subagent | per-agent `streamFn` instances | same as main | same | same |
| virtual/model-redirected | `modelRuntime` mapping to target transport | inherits target | inherits target | target's |
| relay (extension-redirected) | same streamFn | same as main | same | same |
| pre-header work | `Models.applyAuth` awaits+strips `transformHeaders` BEFORE `provider.stream` (once per provider call) | n/a | n/a | headers cannot gate per attempt |
| already-dispatched work | in-flight attempts are past any gate | gate closes BEFORE dispatch; no retroactive control | — | — |

**Minimal missing authority inputs for LoopConductor (consumable):**
1. **Injection locus for `options.fetch`** — core never sets it; the loader must
   define where the hold wrapper enters EVERY producer's request options (all
   rows above) so coverage is complete by construction.
2. **`options.client` policy** — `anthropic-messages.js:363` accepts a prebuilt
   client and bypasses the fetch seam entirely; authority must forbid/override
   `options.client` on gated paths (or gate at the client factory).
3. **All-producer census against the LOADED runtime** (confirm P1-P6 classes
   from `native-transport-contract.md` plus any loader-internal producers).
4. **Already-dispatched boundary semantics** — define gate-close behavior for
   work whose headers are already transformed (applyAuth ran) but not yet
   dispatched vs already dispatched (no retroactive control exists).

No source prerequisite needed (no new worktree assigned; accepted source not
reopened). pJ coordinated with sanitized accepted heads at fresh boundary.

## 1540 MIMO-STREAM REHEARSAL — hypothesis + slice record

- **Unproved assumption being closed:** the 310 rehearsal exercised the
  **Anthropic SDK**; actual MiMo rides **`openai-completions.js:186/195-197`**
  (native `retryProviderRequest`, OpenAI SDK `maxRetries: 0`). No stream-path
  MiMo acceptance may be inferred from the shared helper.
- **Hypothesis:** the accepted hold-fetch correctly gates the actual
  OpenAI-compatible provider retry and preserves its terminal SSE/tool/usage/
  cancel semantics.
- **Slice assigned to pK** (fresh done/blank boundary): worktree
  **311-mimo-stream-rehearsal** (base `e8c8fa5`, nonoverlapping) — NEW
  `clients/transport-fetch-hold/mimo-rehearsal.mjs` (+ necessary README only);
  **reuse accepted hold-fetch unchanged**; invoke the INSTALLED native
  openai-completions provider stream with synthetic local model/context/key and
  real `options.fetch`; no copied retry/SSE implementations. Loopback: first
  500 -> gate held before same-call native second attempt -> zero wire until
  resume -> valid streamed terminal/tool/usage; already-admitted stream
  preserves bytes/terminal under closure; cancellation/slow-upload/uncertain-
  send negatives must NOT invent completion; substantive nonzero falsifier.
  Admitted bounded tests + normal PR + exact-head CI + review + merge; retain
  lineage. No live traffic/credentials/native registry/loader/billing/
  model-seat changes/new helpers/Astra.
- **Scope guards:** this closes a precise unproved actual transport-path
  assumption (not cosmetic churn); Xiaomi/quota/window semantics and honest
  unknowns preserved; LoopConductor busy untouched; **pJ owns live swap**.
  Mechanism acceptance remains synthetic-scope only.

## 1558 RETRY-REVIEW — finding + required distinctions (delivery pending pK boundary)

**Finding (pK 311 WIP, scenario 3c):** `maxRetries=1` observes TWO
server-accepted requests destroyed with NO completion — that proves **honest
error termination under configured retries**, NOT the original Xiaomi
criterion **"never retry uncertain sends"**. Separately, `provider-retry.js:
75-76` defaults `maxRetries 0`, but **deployed seat settings are NOT attested
by that default** — source default ≠ configured behavior ≠ runtime acceptance
(three distinct layers; keep separate in all wording).

**Correction packet for pK at its natural idle/blank boundary (busy now —
input untouched):**
1. NEW conservative **uncertain/pre-header send** case at `maxRetries: 0`:
   assert **EXACTLY ONE** actual server-accepted request and **no done**.
2. NEW genuinely **post-header/partial-SSE disconnect**, synchronized AFTER the
   client sees a delta: **no retry, no invented done**.
3. RETAIN the positive same-call 500/hold/retry fixture with its **explicit
   `maxRetries: 1`** as a separate labeled scenario.
4. PRESERVE the optional-retry TWO-send counterexample as an **unmet
   policy/binding gate** (useful as the honest-termination witness) — never
   present it as the uncertain-send criterion.
5. **Wording rule:** broad retry-correctness claims are blocked until these
   distinctions are reconciled in the PR; 311 CI can accept only the claimed
   bounded mechanics. Source/default/configured vs runtime acceptance stay
   separated; useful-client/quota/24h goals stay OPEN.

**Scope guards:** no live retry/provider/model/registry/billing settings
changes; no copied retry/SSE code; raw seat config/bindings stay OpenAI; no new
seats/Astra. pK busy input untouched — consume at natural boundary.

## 1605 BOUNDARY — 311 merged + 1558 packet delivered ONCE

- **PR311 MERGED** squash **`96a8536b`** (tested head **`805037e6…`**, 9 green) —
  MiMo stream-path fetch-hold rehearsal. Lineage retained.
- **1558 correction delivered ONCE to pK** at the verified done1435/blank
  boundary → **"working verified"**. Fresh nonoverlapping worktree
  **312-retry-policy-fix** (base `96a8536b`): pre-header uncertainty at
  `maxRetries 0` (exactly ONE accepted request, no done) + post-header/
  partial-SSE disconnect synced AFTER client delta (no retry, no invented
  done) + explicit `maxRetries 1` positive fixture retained separately +
  TWO-send counterexample labeled as FAILING the original no-uncertain-retry
  policy (never accepted useful-client behavior). Scope: mimo-rehearsal +
  README only; wrapper unchanged; no copied retry/SSE; no live
  config/model/provider/registry/billing/loader changes.
- **Corrected revert instruction (recorded):** revert path for the accepted
  squash is **`git revert 96a8536b` via a normal revert PR**; preserve the
  original feature commits — never force cleanup.
- Layer separation in all wording: source/default/configured vs actual runtime
  config/census/client/day. pJ working on installed-CLI startup proof; Loop
  busy owns loader/old-drain contract. No busy-pane/Astra/seat touches; no
  private packet to MiMo.

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
