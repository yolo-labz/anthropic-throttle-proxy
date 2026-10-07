# UI tooling and integration research — 07/10/2026

Owner: `thr-ui-tooling-rs` (`320-ui-tooling-research`); generator family **openai**.
Desktop date verified: **07/10/2026, 17:47 BRT**. Inspected source:
`66ad9dc9df82887bccc90572ae0d84dadf5df5fe` (PR #316).
This is a **docs-only** extension to [IMPROVEMENTS](IMPROVEMENTS-2026-10-07.md)
and [GAP-AUDIT](GAP-AUDIT-2026-10-07.md), not a replacement roadmap or a deployment.

**Hypothesis:** remaining UI acceptance risk lies in incomplete contrast coverage,
interaction/receipt coverage and collector-outcome evidence, not a missing UI framework.
**Falsifiers:** the actual gauge text passes contrast, receipts change when gauge
math changes, browser checks cover independent source selections through polling,
and existing metrics distinguish successful versus failed collector attempts.
Two small synthetic counterexamples below establish source gaps, not live incidents.

## Research and execution boundary

- Z.AI public research was **quota-refused**, as reported in the team assignment;
  this seat did not retry it, spawn agents, use another model or a paid/API fallback.
  Session environment remained `openai-codex/gpt-6.1-sol`.
- Three SearXNG angles: **current Impeccable release/engine**, **HTMX 1.x polling and
  focus**, **WCAG 2.2/accessible UI validation**. Each returned results but several
  engines reported HTTP errors, CAPTCHA or access denial. One simpler retry per
  angle preceded canonical-source reads. This was partial engine failure, **not**
  an unreachable SearXNG service or permission to use a public search engine.
- Unauthenticated GitHub release/ref API reads returned **403 rate limit exceeded**.
  Read-only `git ls-remote --tags`, pinned raw files and the release checksum sidecar
  provided independent primary evidence without credentials.
- Python's Playwright accessibility documentation URL returned **404**; the official
  Node documentation was read instead. Its `@axe-core/playwright` examples are **not**
  installed Python tooling. Prometheus instrumentation-page GET returned **403**;
  guessed raw-doc URLs returned **404**. No claim below depends on those unread pages.
- Heavy admission failed twice: first `reopen-pending: 17 s of below-threshold
  remaining`, then `desktop-job-slot.service was already loaded or has a fragment
  file`. Neither payload started. No local full pytest, Ruff scan, Impeccable scan
  or browser run is claimed; no small-lane test/build bypass was used.
- A short Node invariant check **passed**. Contrast arithmetic and receipt-hash
  counterexamples ran without a browser. Details and runnable checks are in
  [the durable receipt](evidence/UI-TOOLING-2026-10-07-checks.md).

## Verified workbench — do not update shared installations

| Tool/surface | Observed state | Useful boundary |
|---|---|---|
| Impeccable skill | Latest semantic `skill-v*` tag advertised by upstream: **4.5.0**; installed checkout **508d7e8955de3b3caf2d8676e85206723d41a887**, clean | Explicitly load this versioned skill; do not let the older marketplace copy win discovery |
| Impeccable engine | Latest advertised `engine-v*`: **0.1.11**; `engine-probe` agrees; installed SHA-256 **0221607e1f535af937ea267c347b1f90233b85dc2563cbd2eeefdf42e0e5c594** matches upstream release sidecar | Static binary launcher; **no Node required**. Cached engine probe is not a scan |
| npm Impeccable CLI | Separate upstream `cli-v*` track ends at **4.1.0** | Do not substitute `npx impeccable` for the installed 4.5.0 skill/0.1.11 engine |
| Python/dev | Host Python **3.14.7**, uv **0.9.2**; shared main venv has pytest **9.0.3**, Ruff **0.15.13**, aiohttp **3.13.5**, Jinja2 **3.1.6** | Read-only cached executables available; source tests must import the feature worktree, not main. CI uses its locked Python 3.13 environment |
| Node | **22.23.2**, npm **10.9.8** | Existing `node tests/dashboard-refresh-check.mjs` runs without npm install |
| Playwright | Global Python tool **1.58.0**; `browser-receipt.py --help` succeeds; main venv has **no Playwright** | `--dry-run` expects Chromium/headless **1208**, Firefox **1509**; all three paths are absent. Cache has Chromium **1200**, Firefox **1497**: those are **not** a qualified 1.58.0 pair |
| axe | No `axe`/`pa11y` command found; global npm inventory timed out after 15 s, so module availability is not established. npm registry says `axe-core` **4.14.0** | Available upstream **is not installed/accepted**. Do not add a dependency for this research slice |

Impeccable setup ran once from the project cwd. It reported no `PRODUCT.md` or
`DESIGN.md`; existing `docs/DASHBOARD-DESIGN.md` remains the incumbent reference.
No automatic init, context migration, hook replacement or design-score claim.
The explicitly loaded skill and its `reference/audit.md` are guidance, not WCAG
certification. A Jinja source scan cannot substitute for a rendered gauge check.

### Exact commands and prerequisites

Run compute/scans/browser capture **only inside an admitted job**. These are tool
commands, not an alternative admission mechanism. No `npx ...@latest` download.

```sh
SKILL=/home/notroot/.local/share/impeccable/skill-v4.5.0/.pi/skills/impeccable
"$SKILL/scripts/impeccable" engine-probe
# Once per session only; already completed by this seat:
# "$SKILL/scripts/impeccable" context
# Help/entrypoint verified; actual scan NOT run in this slice:
"$SKILL/scripts/impeccable" detect src/anthropic_throttle_proxy/ui --json
# Exit 0: no primary findings; 1: scan failure; 2: primary findings.
# Older --ruleset/--format flags are NOT in this installed engine's help.
node tests/dashboard-refresh-check.mjs

# Reproducible feature-local environment; not a shared-venv mutation:
uv sync --frozen --group dev
uv run --frozen pytest -q tests/test_gauge_routes.py tests/test_tps_gauge.py \
  tests/test_dashboard_data_contract.py tests/test_ui_focus_stability.py \
  tests/test_ui_contrast.py tests/test_ui_assets.py
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen pytest -q
```

Browser owner: use the **existing** `delivery` group and frozen lock (**Playwright
1.63.0** in `uv.lock`), not the unqualified global 1.58.0 installation. Provision only the two required engines after
admission; this is a future prerequisite, **not performed here**:

```sh
uv sync --frozen --group delivery
uv run --frozen --group delivery python -m playwright install --dry-run
uv run --frozen --group delivery python -m playwright install chromium firefox
LD_LIBRARY_PATH="$(specs/227-dashboard-audit/playwright-browser-libs.sh)" \
  uv run --frozen --group delivery python specs/227-dashboard-audit/browser-receipt.py \
  --url http://127.0.0.1:8765/ui --out docs/evidence/ui-browser-candidate
DASHBOARD_BROWSER_RECEIPT="$PWD/docs/evidence/ui-browser-candidate/receipt.json" \
  python3 specs/227-dashboard-audit/verify-live.py
```

Capture privacy handling is mandatory before publication: live account tables
may contain identities. Preserve private originals separately; publish only safe
captures. Post-capture redaction changes screenshot digests and must not be passed
as the original receipt. The last command is the **existing desktop-specific oracle**:
`show_primary=false`, named Codex/Z.AI rows, fresh lane report, matching persisted
and effective unit plus running Nix source. It is **not** a generic central-deploy
oracle. A small exploratory `--widths 390,1440` capture is not its complete
four-width/two-zoom/both-engine receipt. Library discovery itself must share the
admitted budget. Missing browsers or libraries are blockers, not green results.

## Four smallest options for the implementation seats

These are proposed bounded slices. **None is implemented by this docs PR.**
Future substantive source changes use Speckit plan → tasks → implementation,
normal hooks/tests/CI and one isolated PR. Check the frontend/QA seats' exact
heads before dispatching overlapping work.

### 1. Close the gauge-caption contrast-test hole

**Evidence:** `ui/static/style.css:1150` paints `.tps-foot` with
`--ctp-overlay0` on `--panel`/Catppuccin mantle; `--fs-meta` is `0.75rem`.
The text at `partials/stats.html:147–148` carries freshness, unknown-versus-zero
and quota-versus-throughput meaning. Existing `tests/test_ui_contrast.py`
`TEXT_TOKENS` omits overlay0. Its own arithmetic gives **3.5943:1**, below
normal-text AA **4.5:1** [S4]. The caption arrived in PR #278 after the earlier
contrast gate; this is not a proposal to introduce another contrast engine.

**Smallest change:** frontend seat retokens only this caption to the existing
accessible text token and adds one selector/surface regression to the existing
contrast test. Do not flatten all decorative strokes or flag the large unknown
readout against the normal-text threshold.

**Owner/dependency:** `thr-ui-frontend`; QA consumes its resulting exact head.
**Acceptance:** red-before caption check; ≥4.5:1 after; rendered computed
foreground/background and unknown/stale captions checked at narrow/desktop sizes.
Synthetic ratio is established; rendered/live contrast is **unmeasured** here.
**Rollout/rollback:** source-only CSS/test PR, then authorized UI deployment;
revert that one squash commit if its rendered result is wrong. No palette update.

### 2. Extend the existing HTMX browser receipt, not the UI framework

**Evidence:** dashboard already uses per-tab `/ui?source=...` navigation and
query-preserving `hx-get`; PR #316 route tests cover selection/network-free render.
The details checkbox is outside `#stats`. Scroll-region IDs and a focus regression
already exist. `tests/check_gauge_wire.py` also already exercises one source's
HTMX polls, failure refresh, unknown gauge and 1366/390 widths with synthetic
telemetry. It was not executed here. `browser-receipt.py` checks one
`#subs-scroll` focus, stamp refresh, disconnect, layout and zoom; neither baseline
harness exercises two independent source choices or details state through swaps. HTMX 1.x documents input-ID focus
restoration and `hx-preserve` input/caret limitations [S2]; its actual 1.9.12 code
also restores focus by ID during swaps. Do not add `hx-preserve` blindly or assume
an ID proves every widget's caret/scroll behavior.

**Smallest change:** QA seat extends its owned `tests/check_gauge_wire.py` and
records explicit case booleans, composing with the existing delivery receipt rather
than duplicating its gauge/source tests. Its campaign extension is already in
progress; qualify that exact artifact first. Two pages with different selected
sources remain independent across ≥2 observed
swaps; checked details stay checked; keyboard focus survives the relevant swap;
slow/failed stats requests neither fake freshness nor lose selection. Deterministic
producer fixtures must include fresh measured zero, missing, malformed and stale
values. Preserve the one-script HTMX invariant.

**Owner/dependency:** `thr-ui-browser-qa`; frontend source and runtime producer
sample/build receipts; qualified frozen Playwright/browser pair.
**Acceptance:** 390px/desktop plus existing zoom/engine matrix; one batched pass,
one repair/confirmation pass maximum. Check error announcements manually with
assistive technology as well as DOM/focus assertions [S5]. No `aria-live` on the
whole two-second panel. Native navigation links are not an ARIA tab widget [S6];
WCAG 2.2 AA target minimum is **24 CSS px with exceptions**, not a blanket 44px [S3].
**Rollout/rollback:** test-only qualification first; any UI repair is separately
scoped. Revert the receipt-extension commit; never replace a failed receipt with
fixture success or suppress its failing checks.

### 3. Bind gauge evidence to its real source dependencies

**Evidence:** `browser-receipt.py` and `verify-live.py` both hash seven source
files. They include routes, presentation, CSS and stats but **not** `history.py`
or `ui/signals.py`, which produce gauge values/geometry. A temporary-copy
counterexample changes `ui/signals.py` while the oracle's hash stays identical.
The oracle already checks running versus persisted/effective Nix package, receipt
age, screenshot digests and a 16-case matrix: **reuse those**, do not build another
provenance service or claim they are missing.

**Smallest change:** extend both dependency lists together with gauge-producing
files and test that mutations to those dependencies invalidate the receipt. Keep
source-head/deployed-head identifiers in the accompanying delivery note; a partial
surface hash is not a whole-repository revision. Preserve the existing timestamp
and screenshot verification.

**Owner/dependency:** `thr-ui-browser-qa`, coordinated with `thr-ui-runtime` for
actual Nix build/pin evidence. Include additional producer dependencies only when
capturing those surfaces; do not pretend a hash proves fresh input data.
**Acceptance:** current counterexample red-after tightening; both lists agree;
wrong source/old receipt rejected; correct deployed source and newly measured
browser receipt accepted. Runtime acceptance remains **not measured** by this seat.
**Rollout/rollback:** test/oracle-only PR; regenerate evidence after authorized
activation. Revert that one commit; old receipts must not be relabeled new.

### 4. Instrument collector outcomes separately from report age

**Evidence:** `_panel_refresh_loop` already gathers Fleet/Copilot in the background;
`_collect_view` reads cache without external HTTP. Fleet's `_refresh_one` writes
`(now, view)` for success **and** `ok=False` failure; `_cached_fleet` exposes attempt
age and ages samples out. Generic collector logging sees exceptions, whereas
normal failure views are returned data. `metrics.py` already has
`subscription_lane_report_age_seconds` and account/identity metrics; **do not add
another subscription-age gauge** or make HTML polling trigger a collector.

**Smallest change:** runtime seat adds bounded UI-collector success/attempt evidence
at the existing background boundary using installed `prometheus-client`: e.g.
last-success timestamp and attempt/failure counts for a fixed collector family.
Inspect the result's `ok`, not only `isinstance(Exception)`. If individual sibling
health is needed, expose cached attempt age/outcome in the existing details view,
not arbitrary URL/account/error-text metric labels. Unknown and failed are distinct;
no successful sample must not become a green epoch-zero metric.

**Owner/dependency:** `thr-ui-runtime`; no admission/limiter/credential-path change.
**Acceptance:** synthetic success/failure/timeout/recovery tests; returned failures
counted; last-success unchanged on failure; normal render makes zero external
collector calls; background-only updates and disabled collectors tested. A live
collector sample, metrics scrape and rendered age/outcome must agree before
claiming observability delivered. No live fault injection into inference lanes.
**Rollout/rollback:** optional read-only collector metrics/details slice after
producer contract work; single proxy-service rollout only when authorized; revert
one squash and return to the previous package/unit. No new network in render paths.

## Already present / deliberately rejected

- Server-rendered gauge, history/sparklines, unknown/stale labels, background
  collection, display-only source selection, CSS tokens, reduced-motion handling,
  asset cache busting and contrast tests already exist. Their **coverage** is the
  issue, not their absence. Do not add a SPA, chart library, routing state store,
  second UI sampler or synthetic throughput inferred from quota.
- The config success toast is native `<output>` and errors already have
  `role="alert"`; missing an explicit success `role` is not proof of missing native
  semantics. Verify announcements in a browser/AT instead of adding redundant ARIA.
- Optional axe could augment the existing Python browser harness by loading a
  checksum-pinned local `axe.min.js`, but no artifact/runner was installed here.
  The official Playwright docs explicitly require manual checks too [S7]. An
  empty scan is not WCAG compliance; a latest-version registry response is not
  reason to create an npm dependency tree for this Python/HTMX repo.
- Docs-only delivery does not authorize deployment, a service restart, a shared
  marketplace update or changes to PR #315's Actions gate. Existing roadmap and
  audit ranking remain unchanged.

## Primary sources (retrieved 07/10/2026)

- **S1 — Impeccable:** [upstream releases](https://github.com/pbakaus/impeccable/releases),
  [tagged skill](https://raw.githubusercontent.com/pbakaus/impeccable/skill-v4.5.0/.pi/skills/impeccable/SKILL.md),
  [engine pin](https://raw.githubusercontent.com/pbakaus/impeccable/skill-v4.5.0/.pi/skills/impeccable/scripts/VERSION),
  [release sidecar](https://github.com/pbakaus/impeccable/releases/download/engine-v0.1.11/impeccable-linux-x64.sha256).
  Git refs and installed digests are in the receipt; release dates were not API-verified.
- **S2 — HTMX 1.x:** [swap/focus](https://v1.htmx.org/attributes/hx-swap/),
  [trigger/polling](https://v1.htmx.org/attributes/hx-trigger/),
  [preserve caveats](https://v1.htmx.org/attributes/hx-preserve/),
  [actual pinned 1.9.12 source](https://unpkg.com/htmx.org@1.9.12/dist/htmx.js).
- **S3 — W3C:** [WCAG 2.2 target-size minimum](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html).
- **S4 — W3C:** [contrast minimum](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html).
- **S5 — W3C:** [status messages](https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html).
- **S6 — W3C APG:** [tabs pattern and keyboard contract](https://www.w3.org/WAI/ARIA/apg/patterns/tabs/).
- **S7 — Playwright:** [automated/manual accessibility limits; Node integration](https://playwright.dev/docs/accessibility-testing).
- **S8 — npm:** [axe-core public package metadata](https://registry.npmjs.org/axe-core/latest),
  observed 4.14.0; not an installed-tool or acceptance receipt.
