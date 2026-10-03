# THRTL-19 — bounded registry integration

Authority: Plane THRTL-19 (`3fd66a66-97d1-4dee-a09f-becdcba18748`), canonical
hub audit T13 (universal provider registry), NOT spec221 T13. Mac owns tracker
updates and publication. Base: `8ea1e8d03a5f14e96028f6ffd7ded86a5ea2d967`.

## Gap and scope

`routing.ROLE_CHAINS` and `routing.default_lanes()` are separate static inputs;
`ingress.LANES` is built once. `lanes._registry_rows()` is display-only.
`fleet_ui_config.load()` already demonstrates bounded last-good reloads, but
presentation config is not routing authority. `config.load_overrides()` handles
proxy tuning, not lane inventory. No ingress registry reload exists at this base.

Smallest vertical slice: an opt-in allowlist of already-configured ingress lane
IDs. It intersects the existing effective role chain, never adds/reorders lanes,
changes model remaps/URLs/credentials, certifies capacity, or supplies a new
privacy permission. Existing caller privacy gates and subscription CLASS/CAPACITY
checks remain prerequisites; this slice does NOT implement a universal privacy
classifier. Registry membership is necessary, never sufficient, for selection.

Files: `src/anthropic_throttle_proxy/{provider_registry,ingress}.py`,
`tests/test_provider_registry.py`, this document. `routing.py` is reused unchanged.
No pK meter-admission, pG token-accounting, Nix pins, live config, session/model,
private evidence, or Plane writes. This is a narrow THRTL-19 increment, not full
universal-provider onboarding or a live rollout acceptance claim.

## Contract / plan

- `INGRESS_PROVIDER_REGISTRY` defaults to empty: no file I/O, no chain change,
  no extra health field. No environment/config is changed by this work.
- When explicitly set, read a local JSON file of at most 16 KiB:
  `{"version":1,"lanes":["anthropic","codex"]}`. Exact keys, integer version 1,
  unique configured string IDs only. An empty list intentionally denies new
  selections. Unknown/retired lane IDs, duplicates and malformed input reject
  the whole candidate, never partially publish it.
- Read off the event loop at startup and on the existing health-poll cadence;
  atomically replace one immutable allowlist. Failed first load stays closed;
  failed later load retains last-good policy with a bounded error token in
  ingress health. Missing files do NOT disable enforcement. Publication should
  use atomic rename. Health reads cached state only; no watcher/endpoint/restart.
- Filter normal selection, session-pin reuse, constrained selection and spill
  lookahead through the same effective-chain helper. A constrained probe must
  re-check membership after awaiting its lane's fresh admission evidence.
- Reload does not modify `LANES`, `lane_state`, `_session_lane`, connection pools,
  or any already-selected `Lane`/response. Existing streams finish unchanged.
  Future selections re-check eligibility; an excluded session pin gets a 403
  `ingress-session-lane-not-registered`, not automatic migration to another
  provider. A new session with an empty effective chain gets 503
  `ingress-no-registered-lane`, not invented capacity exhaustion. Normal
  health-driven pin eviction is unchanged.
- Poll interval <= 0 loads once at startup, with no background reload loop.

## Acceptance / tasks

Hypothesis: a removal-only filter at selection boundaries makes registry reload
executable without changing an already-selected lane or active stream.

- [x] Write bounded validation and opt-in ingress polling/selection integration
  (native file readback confirmed; executable verification below is still open).
- [x] Write `tests/test_provider_registry.py`, including the bounded SSE regression
  and fail-closed/last-good/selection cases. Tests are authored, NOT executed.
- [ ] Hold a synthetic local SSE stream open, reload a policy excluding its lane,
  and prove unchanged bytes, Lane object, ClientSession and session-pin map.
  A new session must be refused; restoring the allowlist must restore selection.
- [ ] Exercise default-off, empty/invalid/oversized/unreadable/unknown-ID input,
  last-good retention, pin policy, role/overflow restrictions, and constrained
  CLASS/CAPACITY refusal (including reload during a fresh probe).
- [ ] Run focused ingress/routing tests and ruff if admitted; freeze exact files
  and test status for Mac. No provider calls or live traffic required.

## Rollback

No deployment is part of this slice. In a future opted-in instance, restoring the
previous valid JSON by atomic rename restores its selection policy at the next
poll without touching active streams. Restoring all configured IDs removes only
this extra restriction; it grants no capacity. An unreadable/deleted file is NOT
a rollback: last-good enforcement remains. Removing the opt-in environment
variable takes effect on a separately authorized future process start, not via
a gratuitous restart. Source rollback is one revert PR after publication.

## Frozen handoff — 03/10/2026

Date was verified with `date` at recovery entry (03/10/2026 17:19, -03).

**State: written and frozen for Mac acceptance/publication; UNTESTED.** This is
not THRTL-19 completion or a merge/deployment claim. Generator family: OpenAI.
No provider/reviewer call, push, PR, deployment, tracker update, live-config
edit, or runtime/session change was performed. NixOS `2585` and `2588` were
observed clean on their original heads and never modified. Parent/session
preservation evidence was not rewritten.

Worktree:
`/home/notroot/Documents/Code/yolo-labz/anthropic-throttle-proxy-294-registry-slice`

Branch: `294-registry-slice`; verified creation from fresh `origin/main` at
`8ea1e8d03a5f14e96028f6ffd7ded86a5ea2d967` (proxy #287). These are working-tree
files, NOT a commit. There is no release SHA and no unpublished push to repeat.
The creation command succeeded; its later tool-availability probe returned
exit 1 after locating Python, so it did not establish test dependencies.

Frozen files (native readback, not a git-status claim):

1. `src/anthropic_throttle_proxy/ingress.py` — modified.
2. `src/anthropic_throttle_proxy/provider_registry.py` — new.
3. `tests/test_provider_registry.py` — new.
4. `specs/294-provider-registry/plan.md` — new, this handoff.

The one post-implementation acceptance command was refused BEFORE execution:

```text
admission refused [pressure-high]: memory-full=0c io-full=1680c
Command exited with code 75
```

Therefore **even syntax parsing, `git diff --check`, test-tool discovery and
pytest/ruff were NOT executed**. No admission retry followed. Native source
inspection is not executable acceptance. Test assertions, rollback behavior,
and unchanged default behavior remain unverified until Mac runs them.

### Mac acceptance/publication boundary

Take custody of all FOUR files above, including the three untracked files
(`git diff` alone omits them). In a fresh feature worktree on the same base,
inspect the resulting diff before accepting or publishing it. Reconcile any
newer upstream changes; do not transplant unrelated desktop WIP. With an
already-provisioned admitted Python >=3.13 development environment:

```sh
uv run --offline --no-sync pytest -q tests/test_provider_registry.py \
  tests/test_ingress*.py tests/test_routing.py
uv run --offline --no-sync ruff check \
  src/anthropic_throttle_proxy/ingress.py \
  src/anthropic_throttle_proxy/provider_registry.py tests/test_provider_registry.py
uv run --offline --no-sync ruff format --check \
  src/anthropic_throttle_proxy/ingress.py \
  src/anthropic_throttle_proxy/provider_registry.py tests/test_provider_registry.py
git diff --check
```

The offline/no-sync flags deliberately avoid dependency fetches or builds. If
the environment is absent, that is a provisioning prerequisite, not a passing
check. Fix concrete test/lint failures, then run full repository acceptance/CI
under Mac's admitted budget before normal PR publication. Mac owns Plane
updates; no duplicate ticket or Done claim from this seat.

### Explicit limits

- This does not replace `default_lanes`, implement new provider adapters, unify
  all fleet meters, or introduce privacy labels. It cannot assert that the
  legacy ingress alone enforces a universal privacy policy; caller-side privacy
  eligibility is still required. Registration grants none.
- Last-good retention intentionally keeps prior restrictions after an invalid
  edit; revocation is not applied until a valid replacement loads. Health
  reports this failure. No stale registry entry implies fresh provider quota.
- The file is a small operator-controlled local regular file; bounded reads
  run off-thread on the existing polling cadence. No live reload was exercised.
- Do not deploy, flip the opt-in, restart/reload occupied panes, or mutate
  private bindings as part of this handoff. Rollback above is a specified path,
  not a live-tested receipt.

## Mac integration acceptance — 03/10/2026

Transported the four frozen owner files into isolated branch
`291-provider-registry` on merged main `7236d96c1a3ab9c30281829184d415a7ffab5c31`.
The original desktop worktree and session remain intact. Mac found two input
boundary gaps: duplicate JSON fields could override an earlier restriction,
and a FIFO could block the health worker despite the byte limit. The parser
now rejects duplicate fields and validates an opened nonblocking descriptor as
a regular file. Three additional executable fixtures cover these cases.

Focused registry/ingress/routing acceptance passes **207 tests**, including a
real held SSE stream across removal/restoration, with Ruff clean. The bounded
MiMo different-family review was unavailable: its tool emitter repeatedly
produced no-op shell calls instead of native reads, then stopped. That is no
review approval; executable source acceptance remains authoritative. Registry
configuration stays unset, so publication does not activate a new policy.

Full Mac suite on this final candidate: **1,643 passed**, 125 existing warnings,
in 73.62 seconds. This includes the 21 new registry cases plus all 1,622 main
regressions. No external provider call or live registry activation was used.

The cached clone detector then found two duplicated probe fixtures (3.17%
on the new test/parser files). They now share one parameterized policy test;
all21 cases remain and pass, with0% detected clones. Production code is
unchanged by that test-only cleanup. Parser coverage is100% of statements
and branches on the207 focused cases.
