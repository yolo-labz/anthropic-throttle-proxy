# Tasks: Compact `GET /__throttle/statusline` Render Probe

**Feature Branch**: `205-statusline`
**Created**: 17/08/2026
**Spec**: `specs/205-statusline/spec.md` (14 FRs, 3 SCs)
**Gate**: `specs/205-statusline/verify.sh` — executable falsifier, exit non-zero on any FAIL

---

## Coordination status (live, 17/08/2026 12:57 BRT)

A sibling worker is implementing this slice in `src/` RIGHT NOW — **Phases 2–3 are already largely
done in their working tree**, not pending. Measured at time of writing:

```
 M src/anthropic_throttle_proxy/history.py   (+34/-14, level_since track selector)
 M src/anthropic_throttle_proxy/limiter.py   (+14/-1)
 M src/anthropic_throttle_proxy/proxy.py     (+266, _statusline_* helpers + route)
?? tests/test_statusline.py
```

This task file was written against that in-flight tree, not against `main`. **I did not edit
`src/` or `tests/`** — only `specs/205-statusline/`.

**Their code was gated against `verify.sh` and passes.** Run 17/08/2026 12:56 BRT, throwaway
instance on `:19501`/`:19502` with `THROTTLE_UPSTREAM=http://127.0.0.1:1` (dead port, so nothing
left the machine) and no credential paths:

```
20 PASS   0 FAIL   1 SKIP        (exit 0)
```

The single SKIP is the comparative-p95 half of SC-002, correctly withheld because a cold instance
with 0 bearers serves an **842-byte** health body — see the SC-002 refinement below.

### Load-bearing integration hazard found in their diff

`history.level_since` gained a keyword-only track selector:

```python
def level_since(level: str, now: float | None = None, *, track: str = "fleet") -> float:
```

`level_since` is **stateful and idempotent per track** — it remembers the timestamp of the last
*transition*, not of the last call. The UI's background sampler already calls it on the default
`fleet` track. If the statusline handler also calls it with the default track, the two callers
overwrite each other's transition timestamp on every render and `state_since_s` becomes noise
(worse at 35 panes × ~3 Hz, where the statusline would win almost every race).

**Therefore: the statusline MUST pass its own track** (`track="statusline"`). This is not a style
preference — it is the difference between `THROTTLED for 12m` and a number that resets constantly.
Covered by T-08 and its test.

---

## Architectural decision: where the projection lives

**What landed (the sibling's call, and it is defensible):** flat private helpers inside `proxy.py`
— `_statusline_unified` / `_statusline_best_configured` / `_statusline_best_observed` /
`_statusline_elect` / `_statusline_window` / `_statusline_queue` / `_statusline_fleet` /
`_statusline_state`, plus the `statusline` handler (`proxy.py:4243-4460`). `proxy.py` grows
4,365 → **4,631 lines**.

**What I recommended:** a separate `statusline.py` taking readings in, on the `history.py`
precedent — *"No dependency on `proxy` — the sampler passes readings in, so this module stays
importable from the hot path without a cycle."* A pure shaping function is unit-testable without
standing up HTTP, and keeps the largest file in the repo from growing further.

**Why the in-proxy form is acceptable anyway:** the election step must call `proxy`'s own private
ranker (`_account_routing_candidate_score`) so selection cannot drift from the hot path. Extracting
only the shaping half would split one 200-line flow across two files for a payload with no
out-of-process consumer, and `health()` already lives here. The helpers are flat, single-purpose,
and individually testable, which is the property that actually mattered. **Not worth churning
now** — if `proxy.py` needs splitting, that is its own slice, not a rider on this one.

Either way the ranking authority stays in the hot path and nothing imports backwards.

Ponytail check: rungs 2–5 are exhausted first. Every number the payload needs already exists —
`_binding_utilization`/`_binding_window` (ratelimit), `unified_live_view`/`bearer_usable` (routing),
`bearer_labels`/`parse_spec` (accounts), `level_since` (history), `snapshot` (limiter). **Nothing new
is computed; this slice only selects and projects.** No new dependency, no new vendor SDK
(invariant #1).

---

## FR → implementing file

Line numbers are against the sibling's in-flight tree (17/08/2026 12:57 BRT) and will drift.

| FR | Requirement (abbrev.) | Implementing symbol | Reuses (no edit) |
|---|---|---|---|
| FR-001 | route `GET /__throttle/statusline`, **above** the catch-all | `proxy.py:4589` (catch-all is `:4595`) | — |
| FR-002 | no bearer slot, no `served`, never forwarded | route placement beside `root_probe`/`health` | `root_probe` precedent |
| FR-003 | ≤1024 B, no client/bearer-scaled collection | `statusline` handler body, `proxy.py:4446+` | — |
| FR-004 | selection = what the hot path would pick now | `_statusline_elect` + `_account_route_decision` | `_account_selection`, `_account_routing_candidate_score` |
| FR-005 | binding window via **live-viewed** unified | `_statusline_window` `:4340` | `ratelimit._binding_utilization`, `_binding_window`, `_unified_live_view` |
| FR-006 | `stale` when raw binding window is past reset | `_statusline_window` `:4340` | `_unified_live_view` |
| FR-007 | optional `?bearer=`/`?model=`/`?max_tokens=` request context | `statusline` handler → `_statusline_elect` | `_account_route_decision` + `_account_selection` |
| FR-008 | `state` enum + severity resolution | `_statusline_state` `:4417` | `config.UTILIZATION_WARN` |
| FR-009 | HTTP 200 in **every** state, incl. egress down | `web.json_response(body, …)` (defaults 200) | — |
| FR-010 | `Cache-Control: no-store` | same `json_response` call | — |
| FR-011 | no upstream I/O, no per-request cred parse | `_statusline_fleet` `:4391` | `accounts.account_snapshot` mtime cache |
| FR-012 | no tokens / paths / emails; 8-hex id + label only | `_statusline_elect` + handler | `_bearer_id` (invariant #2) |
| FR-013 | `/__throttle/health` schema unchanged | *(no edit)* — regression test only | `tests/test_statusline.py` |
| FR-014 | `schema: "statusline/1"` version pin | `_STATUSLINE_SCHEMA` `:4243` | — |

Edge cases from spec.md that the implementation already handles explicitly:
`_STATUSLINE_PSEUDO_BEARERS = frozenset({"_anon", "api-key"})` (`:4247`) and
`_bearer_credential_dead` in the election filter — the two "never elect this as the account serving
me" traps.

**Files touched:** `src/anthropic_throttle_proxy/proxy.py`, `tests/test_statusline.py`, and the two
dependency files (`history.py` track selector, `limiter.py`). Nothing else.

---

## Phase 1 — Ground the slice (no source edits)

- [x] **T-00 · Verify the dependency surface still exists.** Read-only greps for
  `_binding_utilization`, `_binding_window`, `unified_live_view`, `bearer_usable`, `bearer_labels`,
  `parse_spec`, `level_since`, `limiter.snapshot`, and the snapshot keys `queued_total`, `inflight`,
  `max_concurrent`, `retry_after_until`. **Done 17/08/2026** — all present, including in the
  sibling's in-flight tree. Re-run after their work lands.
- [x] **T-01 · Prove the falsifier discriminates before writing code.**
  `./specs/205-statusline/verify.sh` against a conforming mock and against 5 mutations. **Done** —
  `good` exits 0; `oversize`, `dropfield`, `stale`, `conflated`, `notlocal` each exit 1 on the
  correct check. CHECK 4 self-tests pass with no proxy at all.
- [x] **T-02 · Confirm the FR-001/FR-002 hazard is real, not theoretical.** **Done** — measured
  17/08/2026: `GET /__throttle/statusline` today is swallowed by
  `add_route("*", "/{path:.*}", handler)`, answers **404 from Anthropic's edge**
  (`Server: cloudflare`, `CF-RAY: …-GRU`) and **spends a bearer slot**. An unimplemented endpoint is
  not an inert 404 here — it is real upstream traffic.

## Phase 2 — Projection helpers — LANDED in the sibling's tree, pending review

All of Phase 2 exists at `proxy.py:4243-4445`. `[x]` below means *implemented and observed green
through `verify.sh`*, not *reviewed*. Each item names what the live gate still cannot prove, which
is the real remaining work and belongs to T-13.

- [x] **T-03 · Schema pin + helper surface.** `_STATUSLINE_SCHEMA = "statusline/1"` (`:4243`) plus
  eight flat `_statusline_*` helpers. Landed as private helpers in `proxy.py` rather than a new
  module — see the architectural note above for why that is accepted rather than churned.
- [x] **T-04 · Window resolution.** (FR-005, FR-006) `_statusline_window` (`:4340`) applies the live
  view FIRST, then `_binding_window`/`_binding_utilization` on it, and sets `stale=True` when the raw
  snapshot's binding window is past reset. All windows dropped ⇒ `window`/`util`/`status`/`reset` are
  `None` and `stale=True`, so an emitted `reset` is always in the future — that invariant is the
  point. Gate evidence: cold instance renders `{"window":null,"reset":null,"stale":true}`; CHECK 2
  confirms no past-reset window is presented as live.
- [x] **T-05 · State resolver.** (FR-008) `_statusline_state` (`:4417`), most-severe-first
  `down → exhausted → throttled → queued → warn → ok`. **Gate cannot prove this** — it only observes
  whichever state the instance happens to be in. An ascending-order bug resolves everything to `ok`
  and stays invisible in a happy-path render (the defect caught in spec review). **Owed: the
  ordering test.**
- [x] **T-06 · The 18 leaves.** (FR-003, FR-012) Gate evidence: 334 B, exactly 18 leaves, zero
  client/bearer-scaled collections. **Owed:** the ≥1,000-client bound as a *unit* test — the live
  gate only proves it at whatever client count the instance holds (0 cold, 1,369 on the production
  proxy).
- [x] **T-07 · Fleet counters.** (FR-011) `_statusline_fleet` (`:4391`); labels via the
  `(mtime_ns, size)` cache, so no new credential-file traversal on a path polled ~100×/s.
- [x] **T-08 · `state_since_s` on its OWN track.** Calls
  `_history.level_since(level, now, track="statusline")`; the `track` selector added to `history.py`
  exists precisely for this. The hazard documented above was real and is solved. **Owed:** the
  track-isolation test — interleaved `fleet`-track sampler calls must not reset the statusline timer.

## Phase 3 — HTTP surface — LANDED, pending review

- [x] **T-09 · Route ABOVE the catch-all.** (FR-001) `proxy.py:4589`, catch-all at `:4595`. Gate
  evidence: CHECK 0 reports 200, `schema=statusline/1`, zero upstream edge headers. T-02 is the
  evidence for why the ordering is load-bearing.
- [x] **T-10 · Handler gathers readings and delegates.** (FR-004, FR-007) The handler validates
  optional non-secret `bearer`, `model`, and positive `max_tokens` context, then delegates to the
  same `_account_route_decision` and `_account_selection` as the hot path. Tests cover model/token
  pacing, healthy-unconfigured preservation, explicit client API keys, and configured pay-go
  `prefer`/`overflow`; malformed context is never treated as a credential.
- [x] **T-11 · Always 200 + `no-store`.** (FR-009, FR-010) Gate evidence: `Cache-Control: no-store`
  present, 60/60 responses HTTP 200. **Owed:** an egress-down `state:"down"` case — the gate cannot
  induce that safely against a live host.
- [x] **T-12 · Never consume a slot.** (FR-002) Same bypass class as `root_probe`/`health`; must not
  increment `served`. **Gated with a unit test, not the live probe** — on Pedro's desktop ~35
  panes move `served` concurrently (measured +3 on a single probe), so a live delta is not a sound
  assertion. `verify.sh` therefore judges local handling by the absence of upstream edge headers plus
  a `statusline/1` body, and reports the `served` delta as informational only.
  `test_the_probe_consumes_no_slot_and_never_reaches_the_catch_all` asserts `served` unchanged,
  `inflight == 0`, and that no `_anon` limiter was allocated — the catch-all would have made one.

## Phase 4 — Tests + gates

- [x] **T-13 · `tests/test_statusline.py`.** 42 tests. Covers: ≤1024 B **and byte-identical** across
  a 0-client and a 1,000-client-per-bearer fixture; exact 18-leaf key set (via a `_leaves` walker
  that keeps `false`/`null`, which `paths(scalars)` drops); every FR-008 state **and the severity
  ordering** (`test_state_resolution_is_most_severe_first` — all five preconditions true at once,
  peeled one at a time, so the answer must walk the enum downward); stale-window drop and its two
  mirrors (a live `rejected` is NOT stale; every-window-dead reports nulls, not capacity);
  `?model=` (FR-007); no-secret assertion (FR-012); `served` unchanged (FR-002); `level_since`
  track isolation (T-08); **a bearer with no windows never elected** — `_anon`, `api-key`, both,
  and a quarantined credential, parametrized because they are four ways to trip ONE property (all
  four look like a zero-utilization account, the cheapest candidate in any ranking); the
  configured-path mirror where a dead account must also not count toward `fleet.usable`; and `0/0`
  reading `ok` rather than `exhausted`.

  The repo's `jscpd` pre-commit gate rejected the first draft with 2 clone regressions (62 and 53
  tokens, both inside this file). Fixed structurally, not suppressed: the two "never elected" tests
  collapsed into the parametrization above, and the credential-file setup became
  `_configure_accounts`. `npx jscpd tests/test_statusline.py --min-tokens 50` now reports **0
  clones**. The parametrization also pays for itself — removing the
  `_bearer_credential_dead`/pseudo-bearer gate from `_statusline_best_observed` fails all **4**
  params, where the pre-refactor pair caught 2.
- [x] **T-14 · FR-013 health regression.** `test_health_keeps_its_own_schema` pins health's 26
  top-level keys **and** the three collections this slice deliberately drops
  (`bearers[].clients`, `unified`, `limiter.queued_per_client`) — moving one out of health to make
  the projection cheaper would break `claude-account-pick` and `/ui`, which read exactly those.
- [x] **T-15 · Mutation falsifiers.** Both run 17/08/2026, source restored + md5-verified after each:
  - neutering `_unified_live_view` in `_statusline_window` (`live = raw`) ⇒ **2 failed**, exactly
    `test_a_window_past_its_own_reset_is_dropped_and_flagged_stale` and
    `test_every_window_dead_reports_nulls_rather_than_capacity` — the FR-005/006 pair, right reason.
  - hoisting `depth > 0 → queued` above `down`/`exhausted`/`throttled` ⇒ **2 failed**, with
    `test_state_resolution_is_most_severe_first` reporting `AssertionError: assert 'queued' ==
    'down'`. This is the defect a happy-path render cannot see, and it is now caught at the first
    peel.
  - the third (route below the catch-all) is covered structurally instead: the test app registers
    the real routes in the real order **including** the catch-all, so "never forwarded" is a
    property of the wiring under test, not of an assertion. `verify.sh` CHECK 0 judges it live via
    absence of upstream edge headers — measured 17/08/2026, `edge-headers=0`, `marker=1`.
- [x] **T-16 · `uv run ruff check src tests` + `uv run ruff format --check src tests` + full
  `uv run pytest`.** 17/08/2026: `All checks passed!`, `56 files already formatted`,
  **862 passed** (856 before this slice's tests).
- [x] **T-17 · Run the live gate against a WARM proxy.** `./specs/205-statusline/verify.sh` — every
  judged check PASS, exit 0. Already green against a cold throwaway instance (20 PASS / 0 FAIL /
  1 SKIP), but the comparative half of SC-002 is withheld there and is the whole point of the
  criterion, so this is **not yet complete**. Run it against the desktop service (health measured
  69–91 KB) once the new build is activated — `HOST=127.0.0.1:8765 ./specs/205-statusline/verify.sh`.

  **Done 17/08/2026 — 21 PASS / 0 FAIL / 0 SKIP, exit 0.** The cold-instance SKIP is gone because
  the gate finally ran against a blob: a throwaway instance on `:19510` with
  `THROTTLE_UPSTREAM=http://127.0.0.1:1` (dead port — nothing left the machine) was warmed with 360
  short-lived connections across 3 synthetic bearers, each minting its own ephemeral-port key, which
  took health from **842 B → 24,958 B at 360 tracked clients**. Measured there:

  | Criterion | Measurement |
  |---|---|
  | SC-001 payload | **340 B** ≤ 1024, exactly 18 leaves, zero client/bearer-scaled collections |
  | SC-001 O(1) | statusline **340 B** while health is **24,958 B** at the same 360 clients |
  | SC-002 ceiling | statusline p95 **1.032 ms** < 50 ms (invariant #4) |
  | SC-002 comparative | **1.032 ms < 1.368 ms** health — judged, not skipped |
  | FR-009 | 200/200 responses HTTP 200 |
  | FR-002 | `served` 0 → 0 across the probe; `edge-headers=0`, `marker=1` |

  The desktop service on `:8765` was left untouched throughout (verified after teardown:
  `build=…vrvn7sa0…`, `served=3388`, `queue_mode=fair`), and the fixture process was killed.

  **SC-002 refinement (found by running the gate, 17/08/2026).** The `< health p95` claim is judged
  only when health is ≥ 4 KB. A cold instance with 0 bearers serves an **842-byte** health body, at
  which point both endpoints collapse to the same ~1 ms per-request floor and the comparison is a
  coin flip — it failed on noise (0.001091s vs 0.001063s) against a correct implementation. A
  projection can only be proven cheaper than the blob it projects when a blob exists. spec.md SC-002
  now carries this precondition; the absolute 50 ms ceiling still applies unconditionally.
- [x] **T-18 · Mandatory Codex adversarial review** (repo CLAUDE.md). The 17/08/2026 review returned
  **DO-NOT-MERGE** with 5 MAJOR, 3 MINOR, and 2 NIT findings. PR #208 nevertheless merged unchanged;
  follow-up branch `217-statusline-parity` addresses the review before the feature is treated as done:
  - findings 1–2: `_account_selection` is now the single side-effect-free selector used by both the
    request router and statusline, including pressure-vs-strict comparison, soft-target spillover,
    and `allow_retry_probe=True` parity;
  - findings 4–5: unknown-window bearers rank behind measured ones, rejection is derived across both
    live windows, and `exhausted` cannot contradict a healthy elected bearer;
  - findings 7–8: routing reads limiter scalars directly and queue depth includes the priority lane;
  - finding 10: a router-vs-statusline parity test plus six mutation checks prove each repaired
    predicate discriminates. Fresh follow-up evidence: `ruff` clean, **967 passed**, warm isolated
    gate **21 PASS / 0 FAIL / 0 SKIP**, 340 B / 18 leaves, p95 0.914 ms < health 1.329 ms;
  - MAJOR finding 3 was initially deferred, but the 30/08/2026 exact-diff Codex re-review correctly
    BLOCKED the remaining overclaim. The endpoint now accepts non-secret `bearer` and `max_tokens`
    context and shares the router's top-level decision, covering healthy-unconfigured preservation,
    explicit client keys, configured pay-go `prefer`/`overflow`, and token-size pacing;
  - MINOR finding 6 is deliberately not implemented: `fleet.configured` remains the operator-declared
    `THROTTLE_ACCOUNT_CRED_PATHS` denominator. Counting only readable snapshots would hide a missing,
    expired, or unreadable configured credential; a separate `misconfigured` leaf would break the
    frozen 18-leaf schema and belongs in a new version;
  - NIT finding 9 remains an accepted cache-miss risk: the warm p95 proves only the steady cached
    path, not a slow filesystem. Moving credential refresh/I/O off the event loop is a separate
    architectural change; this follow-up adds no upstream I/O and preserves the existing cache path;
  - the 30/08/2026 Codex re-review's second MAJOR found that unsuffixed `status="rejected"` was
    throttled but still counted in `fleet.usable`. `_statusline_bearer_usable` now shares the all-slot
    rejection predicate, with a parser-valid regression fixture;
  - the subsequent different-family exact-diff review found two more MAJORs: a display-only
    best-observed fallback could suppress `exhausted`, and `allowed` + `util=1.0` still counted as
    usable although routing hard-gates it. Election now returns authoritative route provenance and
    only selected/preserved context can suppress exhaustion; all live 5h/7d util slots hard-fail at
    1.0. Both review reproductions are permanent red-capable tests;
  - the root availability owner, `routing.bearer_usable`, now rejects all live aggregate/5h/7d
    `rejected` statuses and 5h/7d util ≥ 1.0, while preserving its stale-reset unlock rule. Health,
    admission, lane routing, and statusline therefore share the same hard-availability predicate
    instead of leaving the sibling surfaces contradictory;
  - final exact-diff Codex review: **ALLOW**, no BLOCKER/MAJOR. Its queue-depth MINOR is documented
    honestly: direct properties avoid per-client snapshot allocations, but `queued_total` remains
    O(active queued clients); the response stays O(1), and maintained counters are the upgrade if
    measured CPU warrants the added mutation bookkeeping;
  - final different-family review of the OpenAI repair delta: **ALLOW**, no BLOCKER/MAJOR. Its
    request-context MINOR is router-faithful and explicit in the spec: a supplied, well-formed but
    unobserved bearer may be preserved with null/stale gauges while `fleet` still exposes 0/N. Bool
    utilization remains fail-closed, short Retry-After precedence and API-key cache-miss I/O are
    documented residual semantics. Source merge is not runtime activation.

## Phase 5 — Consumer (separate repo, NOT this PR)

- [ ] **T-19 · `~/NixOS` statusline renderer.** Point `claude-code.nix:1331+` at the endpoint,
  keeping the stdin-JSON path as fallback when the proxy is unreachable. Sibling worktree
  (`~/NixOS-NNN-slug`), separate PR. Out of scope for Spec 205 per spec.md.

---

## Definition of done

1. Phases 2–4 complete; `verify.sh` exits 0 against a **warm** proxy (≥ 4 KB health), so the
   comparative half of SC-002 is actually judged rather than skipped.
2. `ruff` clean, full `pytest` green.
3. Each mutation in T-15 fails for the right reason.
4. `/__throttle/health` schema unchanged (FR-013).
5. Codex adversarial review addressed, or a documented evidence-backed reason not to act.

## Explicitly out of scope

- Reshaping / shrinking / paginating `/__throttle/health` — FR-013 forbids it here. The `clients`
  map's unbounded growth (**1,006 → 1,369 entries and 69,408 → 91,118 B observed across 16–17/08**)
  is real but a separate slice.
- Implicit per-client attribution from TCP identity — impossible by construction because the
  statusline is a distinct process from its TUI. Exact caller-specific election instead uses the
  consumer-supplied non-secret bearer hash; no `_client_id` join or raw credential is introduced.
- Prometheus changes, fleet/multi-proxy aggregation, any Nix or deployment mutation.
