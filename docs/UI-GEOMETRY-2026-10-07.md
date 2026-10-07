# Gauge-view scroll geometry — 07/10/2026

Owner `thr-ui-frontend`, fresh `323-ui-scroll-geometry` from `origin/main` **de88b98** (#319/#320/#321 included). Date verified 07/10/2026 18:38 BRT. Previous merged 317 WIP/receipts preserved. Generator OpenAI GPT-6.1 Sol; no agents/provider fallback/runtime activation.

## Exact cause, not the falsified metadata hypothesis
Read QA confirmation: narrow document 473px at 390, returned failed-sibling view 1391px at 1366; outside-table rectangle census empty, old live fits. Source tracing identifies #316's quota-specific `.sr-only` spans: clipped absolute boxes inside percentage cells, with no positioned scroll ancestor.

Admitted **fail-before** execution of existing `tests/check_gauge_wire.py` (temporary diagnostic instrumentation, source UI unchanged) reproduced all three failures and exited **1**. At 390px:
- `subs-scroll`: client width **366**, scroll content **480**, `overflow-x:auto`, position **static**.
- Six `.sr-only` quota annotations: right edge **472.75**, **BODY** containing block, `clip:rect(0,0,0,0)`.
- Document width **473**, despite no outside-table rectangle violation. Visual clipping is not scrollable-layout containment for an absolute child whose containing block is outside the scroller.

Reversible trials in the owned synthetic page prove causality:
- Only `.bearers-wrap { position: relative; }`: document **473 → 390**.
- Removing that temporary style restores **473**.
- Making only those annotations static also reduces width to **390** (diagnostic control, not shipped).
- Initial desktop measured view stays **1366** through both trials.

Receipts `docs/evidence/ui-geometry-2026-10-07/before/`: JSON, sanitized screenshots/DOM and failing command output. Admitted run: **12.168s**, **7.441 CPU s**, **846.5MiB** peak. No operator profile/credentials/inference/live changes. This is synthetic cached throughput plus the existing read-only lane-report snapshot, not deployed acceptance.

## Minimum correction / checks
Only runtime-source change: `.bearers-wrap` gains `position:relative`, with one causal comment. This binds absolute annotations to their actual scroll container; it adds no clipping, hides no metric, changes no class or gauge/source/freshness/unit semantics, and preserves the existing auto scroll overflow.

Existing checker extended, not duplicated: capture scroll-region/absolute containing-block geometry, assert hidden annotations' containing blocks are scroll-local, retain the old root-overflow measurements/failure keys, capture stale/error/local-unknown and returned failed-sibling geometry at 1366/390, and test actual native ArrowRight scrolling in a genuinely wider table with focus preserved. Temporary trial hook was removed from the shipped checker after baseline evidence; acceptance never injects a fix stylesheet.

Speckit spec/plan/tasks: `specs/323-ui-scroll-geometry/`. Ownership coordinated in canonical UI-TEAM: pT owns this CSS/additive checker seam, pW independent confirmation; pV runtime service custody. No QA/runtime doc/backend edits.

## Source/browser acceptance (synthetic branch, not live)
The bounded after-fix Chromium pass **passed** (no injected stylesheet):
- Measured workload: document **1366/1366**, **390/390**.
- Stale, error and local-unknown at 390: document **390/390** for all three.
- Returned failed-sibling state: **1366/1366**, **390/390** (before 1391/1366, 473/390).
- All six quota annotations now have `subs-scroll` as their containing block, including the desktop returned-sibling boxes reaching1391.14px inside their own scrollport. None enlarges root; no annotations removed.
- Narrow table remains **480px content / 366px client**, `overflow-x:auto`; native ArrowRight moved scrollLeft **9px** with focus retained and document still390. Existing HTMX source/focus/details, stale/error/unknown, keyboard/touch and no-network checks passed. Zero findings, unexpected requests and console errors.
- JSON, sanitized PNG/DOM and full output in `after/`. Screenshots at 390 and returned-sibling1366 inspected. This proves root containment/interaction, not a full visual/a11y endorsement: the existing failed-sibling desktop layout still has a large empty unavailable panel and cramped capacity-table meter/burn columns. That unchanged internal-layout debt is logged, not hidden by the root fix.

Same admitted job passed **138 targeted tests** (1 warning, 1.84s) and **2,096 full-suite tests** (155 warnings, 91.94s). Total job **1min47.355s**, **36.782 CPU s**, **587MiB** peak. Read-only cached 321 delivery environment, `PYTHONPATH` explicitly imports323 source/tests. Ruff lint/format (119 files) and diff whitespace pass (`ruff.txt`). Earlier after-run admission refusal is retained as history (`pressure-high`, memory-full19c/io-full1172c); it is not the final result. No deployment/PR merge acceptance is asserted before exact-head CI. Versioned Impeccable 4.5.0 craft-floor reread before CSS edit; engine 0.1.11 detector ran once on finished CSS and returned **exit 2 / five pre-existing side-tab border warnings** (none in the scroll-region correction). Findings preserved in `impeccable-detect.json`, not suppressed or mislabeled as accessibility/browser acceptance. Narrow+desktop admitted confirmation is now captured; exact-head normal PR/CI and source merge remain next. Runtime/live browser remains separately owned even after source passes.

Reproduce under admitted compute, installed Chromium and delivery dependencies:

```sh
PYTHONPATH=src:tests PLAYWRIGHT_NODEJS_PATH="$(command -v node)" \
  BROWSER_EXECUTABLE=/path/to/installed/chromium \
  uv run --group delivery python tests/check_gauge_wire.py --synthetic-only \
  --out docs/evidence/ui-geometry-2026-10-07/after
```
