---

description: "Task list for 002-delivery-platform — throttle proxy delivery"
---

# Tasks: Throttle Proxy Delivery Platform

**Input**: Design documents from `specs/002-delivery-platform/`
**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅,
contracts/{http-routes,env-vars,health-json}.md ✅, quickstart.md ✅

**Tests**: NOT regenerated — `tests/` already covers all 4 user stories
with 89 passing tests. Tasks below verify coverage rather than rebuild it.

**Organization**: Grouped by user story per spec.md (US1 = P1 storm
survival; US2 = P2 central fallback; US3 = P2 observability + advisor;
US4 = P3 reboot persistence). Most tasks are **verification +
documentation harmonization**: the proxy code is already in place; the
delivery gap is between constitution + spec + plan + code + docs +
skills + live host.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies).
- **[Story]**: US1 / US2 / US3 / US4 (mapped to spec.md user stories).

## Path Conventions

Single project — `src/anthropic_throttle_proxy/` + `tests/` at repo
root, per plan.md. `ui/` is a submodule. Worktree path
`/home/notroot/Documents/Code/yolo-labz/anthropic-throttle-proxy/.worktrees/anthropic-throttle-proxy-029-local-root-probe`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Confirm the dev environment is in the same shape the spec assumes.

- [x] T001 Verify worktree identity: `git rev-parse --show-toplevel` ends in `-029-local-root-probe`, `git rev-parse --abbrev-ref HEAD` is `002-delivery-platform`. Refuse to proceed otherwise.
- [x] T002 [P] Verify `uv sync` produces a clean lockfile-consistent env: `uv sync && uv pip list | wc -l > /dev/null`.
- [x] T003 [P] Verify `pyproject.toml` build backend is `hatchling` and target is Python 3.13: `grep -E 'hatchling|python = ">=3.13' pyproject.toml`.
- [x] T004 [P] Verify `ruff` is the lint+format tool: `grep -E '^\[tool\.ruff\]' pyproject.toml` (no flake8/black/isort sections).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Lock in the gates that every user story depends on.

**⚠️ CRITICAL**: No user-story verification can claim "done" until these pass.

- [x] T005 Run full test suite: `uv run pytest` — expect 89/89 passing in ≤ 5 s.
- [x] T006 Run lint: `uv run ruff check src tests` — expect "All checks passed!".
- [x] T007 Run format check: `uv run ruff format --check src tests` — expect no diff.
- [x] T008 [P] Verify live local proxy answers `/__throttle/health` in < 50 ms: `time curl -fsS http://127.0.0.1:8765/__throttle/health > /dev/null`.
- [x] T009 [P] Verify live local proxy answers `GET /` and `HEAD /` with `200`: `curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/`; `curl -fsSI -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/`.
- [x] T010 [P] Verify constitution gates: `grep -E 'import (anthropic|openai|groq)' src/anthropic_throttle_proxy/proxy.py src/anthropic_throttle_proxy/forwarding.py src/anthropic_throttle_proxy/limiter.py` returns no matches (Constitution Principle I).
- [x] T011 [P] Verify bearer-id hygiene: `grep -nE 'def _bearer_id|sha256.*\[:8\]' src/anthropic_throttle_proxy/ratelimit.py` shows SHA-256 prefix at length 8 (Constitution Principle II).

**Checkpoint**: Gates green → user-story phases unblocked.

---

## Phase 3: User Story 1 — Storm survival (Priority: P1) 🎯 MVP

**Goal**: Developer's `claude-code` session continues to complete requests through the proxy while upstream returns a sustained `429` storm. Zero hard rate-limit errors surface.

**Independent Test**: Force-mock a 429 storm; observe AIMD shrink, `Retry-After` honor, `529` carve-out, unified-window auto-pause. All covered by existing pytest cases.

- [x] T012 [US1] Verify AIMD shrink + ramp + cooldown coverage: `uv run pytest tests/test_pacing.py -v` — expect tests covering 429 shrink, success ramp, `THROTTLE_AIMD_BACKOFF_S` cooldown gate.
- [x] T013 [US1] Verify `529` carve-out: `grep -nE '529|OVERLOAD_STATUSES|anthropic_overload_total' src/anthropic_throttle_proxy/forwarding.py src/anthropic_throttle_proxy/proxy.py src/anthropic_throttle_proxy/config.py` — `529` increments overload counter; never enters AIMD shrink.
- [x] T014 [US1] Verify `Retry-After` honor: `uv run pytest tests/test_pacing.py -k retry_after -v` — block dispatch + block growth during window.
- [x] T015 [P] [US1] Verify unified-window auto-pause: `uv run pytest tests/test_unified.py -v` — `status=rejected` auto-pauses until reset; `THROTTLE_UTILIZATION_TARGET > 0` proactively shrinks.
- [x] T016 [P] [US1] Verify per-bearer fair queue: `uv run pytest tests/test_proxy_app.py -k fair -v` — round-robin across `client_id` for one bearer with two chatty clients.
- [x] T017 [P] [US1] Verify burst-pacing dispatch lock: `grep -nE '_dispatch_lock|MIN_DISPATCH_GAP_S' src/anthropic_throttle_proxy/pacing.py` — process-global lock; gap enforced between consecutive dispatches.
- [x] T018 [US1] Live storm smoke test: in second shell, run `for i in $(seq 1 30); do curl -fsS -X POST -H "Authorization: Bearer test" http://127.0.0.1:8765/v1/messages -d '{}' & done; wait`. Even with all 30 returning `4xx`/`5xx` from upstream, `/__throttle/health` `served` increments by 30 and `bearers[<bid>].limiter.live_cap` is observably bounded.

**Checkpoint**: All US1 verifications pass → MVP story is delivered.

---

## Phase 4: User Story 2 — Central fallback (Priority: P2)

**Goal**: Operator deploys one Dokku central; every local proxy fans out through it; central down → transparent fallback to direct upstream.

**Independent Test**: Disable central via `/etc/hosts` block; within one `THROTTLE_CENTRAL_HEALTH_INTERVAL`, `/__throttle/health` reports `central_status=down` and clients still see successful responses (forwarded direct to upstream).

- [x] T019 [US2] Verify central health loop: `grep -nE 'central_health_loop|CENTRAL_HEALTH_INTERVAL' src/anthropic_throttle_proxy/forwarding.py src/anthropic_throttle_proxy/proxy.py` — background poller polls `/__throttle/health` every interval, flips `state["central_status"]`.
- [x] T020 [US2] Verify central fallback test coverage: `uv run pytest tests/test_forwarding_paths.py -v` — central healthy → forward to central; central unhealthy → forward to upstream.
- [x] T021 [P] [US2] Verify central admission cap when `queue_mode=off`: `grep -nE 'CENTRAL_LOCAL_MAX_CONCURRENT' src/anthropic_throttle_proxy/config.py src/anthropic_throttle_proxy/limiter.py` — default 2; applies only when `queue_mode=off` AND `CENTRAL_URL` set.
- [x] T022 [US2] Live central status confirmation: `curl -fsS http://127.0.0.1:8765/__throttle/health | jq '{central_status, central_url, central_last_check}'` — `central_status=up`, `central_url` non-empty, `central_last_check` is a recent epoch.
- [x] T023 [US2] Verify central tier Dokku health: `curl -fsS https://anthropic-throttle.home301server.com.br/__throttle/health | jq '{queue_mode, max_concurrent, served}'` — `queue_mode=fair`, `max_concurrent=8`.

**Checkpoint**: Central up + verified-down fallback both observable.

---

## Phase 5: User Story 3 — Observability + dashboard + advisor (Priority: P2)

**Goal**: Health JSON, `/metrics`, `/ui` HTMX dashboard, GROQ advisor — all without leaking secrets.

**Independent Test**: `curl /__throttle/health /metrics`, open `/ui`, trigger an advisor call. Verify only 8-char `bearer_id` appears anywhere; no raw `Authorization` header or API key in any surface.

- [x] T024 [US3] Verify `/__throttle/health` schema matches `contracts/health-json.md`: `curl -fsS http://127.0.0.1:8765/__throttle/health | jq 'keys'` includes all top-level fields from the contract.
- [x] T025 [P] [US3] Verify `/metrics` exposition + process-local registry: `curl -fsS http://127.0.0.1:8765/metrics | grep -E '^anthropic_(requests_total|overload_total|ratelimit_unified|bearer_)'` and `grep -n 'CollectorRegistry()' src/anthropic_throttle_proxy/metrics.py` — each metric family present, registry instantiated process-local (NOT the prometheus global default).
- [x] T026 [P] [US3] Verify HTMX-only dashboard: `grep -cE '<script' src/anthropic_throttle_proxy/ui/templates/dashboard.html` — expect exactly 1 `<script>` tag.
- [x] T027 [P] [US3] Verify Catppuccin Mocha tokens: `grep -cE '#[0-9a-fA-F]{6}' src/anthropic_throttle_proxy/ui/templates/dashboard.html src/anthropic_throttle_proxy/ui/templates/partials/*.html` — expect zero raw hex in templates (all colors via `ui/static/style.css`).
- [x] T028 [P] [US3] Verify advisor lazy-import: `grep -nE 'from \.\.ui\.advisor_impl|from anthropic_throttle_proxy\.ui\.advisor_impl' src/anthropic_throttle_proxy/proxy.py src/anthropic_throttle_proxy/forwarding.py src/anthropic_throttle_proxy/limiter.py` — expect zero matches at module scope (imported inside `_maybe_advise` only).
- [x] T029 [US3] Verify advisor gate: `grep -nE 'ADVISOR_ENABLED|GROQ_API_KEY' src/anthropic_throttle_proxy/proxy.py src/anthropic_throttle_proxy/ui/advisor_impl.py` — both gates checked before any GROQ call.
- [x] T030 [P] [US3] Verify no raw bearer tokens in surfaces: `curl -fsS http://127.0.0.1:8765/__throttle/health | jq '.bearers | keys'` — all entries are 8-char hex (or `_anon`).
- [x] T031 [P] [US3] Verify advisor test coverage: `uv run pytest tests/test_advisor.py -v` — covers debounce, gating, lazy-import.

**Checkpoint**: Observability surfaces honest; no secrets leak; advisor optional.

---

## Phase 6: User Story 4 — Reboot persistence (Priority: P3)

**Goal**: After Nix / Home Manager activation, reboot does not silently regress the running build.

**Independent Test**: Compare `systemctl --user cat` (post-reboot resolution) against `systemctl --user show ... -p ExecStart` (effective runtime). Both must reference the same Nix store path.

- [x] T032 [US4] Capture effective ExecStart: `systemctl --user show anthropic-throttle-proxy.service -p ExecStart --value`. Record store hash.
- [x] T033 [US4] Capture persistent ExecStart: `systemctl --user cat anthropic-throttle-proxy.service | grep ExecStart`. Record store hash.
- [x] T034 [US4] Verify hashes match (T032 == T033). If they diverge, apply the surgical symlink swap in `CLAUDE.md` § "Persistence checklist" step 1, then re-run T032 + T033.
- [x] T035 [P] [US4] Verify pkg has root-probe code: `pkg=$(systemctl --user show anthropic-throttle-proxy.service -p ExecStart --value | grep -oE '/nix/store/[a-z0-9]+-anthropic-throttle-proxy-[0-9.]+'); grep -c 'def root_probe\|app.router.add_get("/", root_probe' "$pkg/lib/python3.13/site-packages/anthropic_throttle_proxy/proxy.py"` — expect ≥ 2.
- [x] T036 [P] [US4] Verify HM gcroot reachable: `nix-store --query --roots $(readlink -f ~/.local/state/nix/profiles/home-manager)` — expect at least one `system-N-link` or `/run/current-system` reference (otherwise GC will eat the canonical HM-files derivation).
- [x] T037 [US4] Re-run the persistence checklist in `CLAUDE.md` (steps 1–7); record any drift in `handoff.md`. **Evidence 2026-05-26**: cat hash == show hash == `mg70cbx3rjk0vmh8kd72d0cfx2mcwa3i-anthropic-throttle-proxy-0.1.0` (T034 verified). `root_probe` symbol present in the store path's `proxy.py:915` + `:940` (T035). HM gcroot resolves (T036). No drift; no `handoff.md` entry required.

**Checkpoint**: Reboot is safe — `cat` and `show` agree.

---

## Phase 7: Polish & Cross-Cutting

**Purpose**: Docs/skills/specs coherence + PR readiness + adversarial review.

- [x] T038 [P] Verify docs coherence: `CLAUDE.md`, `README.md`, `docs/DEPLOY-DOKKU.md` agree on invariants (no SDK on hot path, AIMD floor, root-probe local, central fallback, persistence checklist). Diff any contradiction; resolve by editing the doc, not the constitution.
- [x] T039 [P] Verify skills coherence: `.claude/skills/{throttle-incident,nix-user-service,deploy-dokku}/SKILL.md` reference the same commands and store paths as `CLAUDE.md` § "Host service verification".
- [x] T040 [P] Verify constitution gates remain green post-design: re-run `grep -E 'import (anthropic|openai|groq)' src/anthropic_throttle_proxy/proxy.py src/anthropic_throttle_proxy/forwarding.py src/anthropic_throttle_proxy/limiter.py` (Constitution Principle I, must be empty).
- [x] T041 Review the `/speckit.analyze` cross-artifact consistency report and act on remediation findings. Inconsistencies are resolved by editing the lower-tier artifact (constitution wins over spec wins over plan wins over tasks). The /speckit.analyze run from 26/05/2026 produced 6 LOW + 1 MEDIUM findings, zero CRITICAL.
- [x] T042 Run `/speckit.checklist` to generate delivery + security + ops checklists in `specs/002-delivery-platform/checklists/`.
- [x] T043 Mandatory Codex adversarial review (`codex:codex-rescue` agent, session `019e6754-dbde-70b1-aea1-ebdd336f630c`, 2026-05-27T02:50Z, verdict **YELLOW**). 10 challenge points: 4 VERIFIED (FR-001/003/010 trace, 529 carve-out, bearer-hash isolation), 3 PARTIAL (central health flap has no hysteresis but the failed-forward fallback path at `proxy.py:536` still routes direct; cat/show persistence recipe correct but the `grep | head -1` heuristic can mis-pick under multi-HM closure; FR-008 covered by unit test only), 3 UNSUPPORTED (since-addressed this turn): (a) AIMD floor — code had no import-time clamp on `THROTTLE_AIMD_MIN` so an env value of 0 broke Constitution III → **fixed** `config.py:66` `AIMD_MIN = max(1, int(...))` + regression test `tests/test_pacing.py::test_aimd_min_is_clamped_to_floor_one`; (b) advisor lazy-import claim ignored the on-demand site at `ui/routes.py:149` → **fixed** spec FR-019 now enumerates both runtime triggers (`_maybe_advise` auto + `ui.routes.advisor` on-demand) and asserts neither lives at module-import scope on the hot path; (c) doc drift: `quickstart.md` `https://` vs Tailscale `http://` → **fixed** to `http://` with TLS caveat, and `~85%` Sonar reworded as *aspirational* in both `spec.md` FR-021/SC-007 and `constitution.md` (the workflow has no numeric fail gate today and skips when `SONAR_HOST_URL` is unset). Partial findings deferred as non-blocking — central-health hysteresis is correctness-tolerant under the existing fallback path, HM-files grep is an operator-runbook concern not a runtime invariant.
- [x] T044 [P] Final test gate: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests`. Note: SonarQube line-coverage ≥ 85 % is enforced in CI via `PROJECT_ANALYSIS_TOKEN` (never `USER_TOKEN`) — not runnable from the worktree.
- [x] T044a [P] Verify Dockerfile shape (FR-020): `grep -cE '^FROM .* AS|uv sync|uv pip|hatchling' Dockerfile` — multi-stage build + `uv` install path present. No Heroku buildpack references.
- [x] T045 [P] Final live-proxy gate: `curl -fsS http://127.0.0.1:8765/__throttle/health | jq .served` and `curl -fsS http://127.0.0.1:8765/` both return.
- [ ] T046 Push branch + open PR: `git push -u origin 002-delivery-platform && gh pr create --title "docs(speckit): 002 delivery platform spec/plan/tasks" --body "$(cat <<'EOF'\nSummary, Constitution gates, Phase outputs, Verification evidence, Adversarial review (Codex findings).\nEOF\n)"`. Babysit CI green per ownership rule.

**Checkpoint**: Branch ready for merge. All gates green; Codex findings addressed.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No deps. Run T001 first; T002–T004 in parallel.
- **Phase 2 (Foundational)**: Depends on Phase 1. Blocks all user stories. T005–T011 can interleave (T005–T007 sequential within the same shell; T008–T011 in parallel against the live proxy).
- **Phase 3 (US1)**: Depends on Phase 2. T012–T017 are independent reads; T018 is a live test that holds the proxy briefly.
- **Phase 4 (US2)**: Depends on Phase 2. Independent of Phase 3.
- **Phase 5 (US3)**: Depends on Phase 2. Independent of Phase 3 / 4.
- **Phase 6 (US4)**: Depends on Phase 2. Requires Pedro's desktop host (host-bound).
- **Phase 7 (Polish)**: Depends on at least one user-story phase being complete. T043 (Codex review) MUST run before T046 (PR open).

### User Story Dependencies

- US1, US2, US3, US4 are independent in spec.md and stay independent in execution.
- US3 (observability) is the only story that benefits from doing US1 + US2 first, because the dashboard then has live signal to display.

### Within Each User Story

- Read-only verifications (`grep`, `pytest`, `curl /__throttle/health`) can run in any order.
- Live storm / central-down tests (T018, T022, T023) hold the proxy briefly; run them late in their phase.

### Parallel Opportunities

- Phase 1: T002–T004.
- Phase 2: T008–T011.
- Phase 3: T015–T017.
- Phase 4: T021.
- Phase 5: T025–T028, T030–T031.
- Phase 6: T035–T036.
- Phase 7: T038–T040, T044–T045.

---

## Parallel Example: User Story 1

```bash
# Once Phase 2 gates are green, fire the read-only verifications together:
uv run pytest tests/test_pacing.py -v &
uv run pytest tests/test_unified.py -v &
uv run pytest tests/test_proxy_app.py -k fair -v &
wait
```

---

## Implementation Strategy

### MVP First (US1 only)

1. Phase 1 (Setup) → Phase 2 (Foundational gates) → Phase 3 (US1).
2. Stop and validate: storm-survival path proven.
3. If US1 holds, the proxy is shippable as a local-only tier even without central.

### Incremental Delivery

1. MVP: US1 → ship local tier.
2. Add US2 (central tier) → ship the Dokku app.
3. Add US3 (observability + advisor) → operator visibility.
4. Add US4 (persistence) → reboot safety.
5. Polish + Codex review → PR + merge.

### Parallel Lane Strategy

Single-operator (Pedro): T002–T011 in parallel within their phase. US1 / US2 / US3 verifications can interleave within one terminal session because most are read-only.

---

## Notes

- This is a **delivery** task list, not a feature build. The code is in
  place. Tasks verify alignment between constitution + spec + plan +
  code + docs + skills + live host.
- Test coverage is already at ~85% line; existing pytest covers each
  user story. New tests are NOT generated.
- Codex adversarial review (T043) is **mandatory** per
  `CLAUDE.md` § "Incident workflow and adversarial review" and the
  constitution's Workflow & Incident Response section.
- Worktree-first policy: every task runs from
  `.worktrees/anthropic-throttle-proxy-029-local-root-probe`.
  `main` stays clean.
- Commit cadence: after each phase checkpoint, commit verification
  evidence + any doc edits with a conventional commit.
