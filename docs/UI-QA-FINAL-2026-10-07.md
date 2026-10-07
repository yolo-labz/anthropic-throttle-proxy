# Independent final-source/deployed browser QA — 07/10/2026

Owner: thr-ui-browser-qa / pW; generator OpenAI GPT-6.1 Sol. Fresh isolated
`325-ui-qa-final`, old 319/321 worktrees and WIP preserved. No agents, CSS/backend,
service/timer mutations, dependencies or admission bypass.

## Exact source / falsifier

#321 is already MERGED `de88b981`; its pending footnote is historical.
#323 candidate `4862145dcaff3a1d499e8d90cc34f0e29dada96c` is MERGED at final source
`45b7d0ef3321cca9c6d4e8513686efa30d889c1a`. All local UI source bytes match that
candidate. Manifest: `docs/evidence/ui-browser-qa-2026-10-07/final-source/source-manifest.json`.

Hypothesis/falsifiers and tasks: `specs/325-ui-qa-final/`. Native installed
Chromium plus the existing declared delivery dependency/checker only. Pinned
Impeccable 4.5.0/engine 0.1.11 and canonical browser INDEX/Cookbook read.

Recorded fail-before: own `confirmation/receipt.json` (390→473 and failed sibling
1366→1391), and frontend geometry `before/receipt.json` (BODY containing block;
reversible positioning experiment). Do not replace those failing receipts.

## Instrumentation attempt / admission

First new admitted command stopped at the added partial AX-tree probe: a generic
span's partial tree had no readable children. **Instrumentation error, not a UI
verdict**, not a pass. 12.068 seconds, 7.998 CPU seconds, 637.2 MiB peak.
Durable `final-source/instrumentation-failure.json` plus command output retain it.
The repaired oracle enables Chromium's full AX tree and requires one unignored
StaticText match per DOM quota annotation; it never deletes annotations or skips
an empty fixture. Only quota-text matches leave the browser, not raw identity AX.

The same attempt captured the newly served page with root widths 1366/1366 and
390/390 and relative scroll containment. This was **partial live geometry**, not
all-oracle deployment acceptance. The repaired source/live attempt has been
refused before execution for loaded slot / pressure recovery; exact results
must supersede this pending state, not be inferred from frontend's own pass.

## Separate live acceptance packet for pV

The checker now offers `--deployed-build <exact activated package root>`.
It does not patch collectors, route state, caches or the running process.
It verifies running imported path, every UI source byte and served CSS; both
widths, actual HTMX local selection/detail/focus stability; real Desktop row;
weekly percent/used+remaining=100/300-second cadence/freshness against the real
atomic report; quota AX text; zero browser console/errors/unexpected requests.
Synthetic stale/error/unknown and raw render-network guard remain in the separate
source reproduction, never relabeled as live induced failures.

Read-only 19:09–19:13 activation receipt binds pV to package
`/nix/store/95ll5i712vsrzri85hsv675ljjr09kk2-anthropic-throttle-proxy-0.1.0`,
final source `45b7d0ef`, filtered hash
`sha256-wpx/L5yiv2vo9rNndRIcvur58MYp7Rkke4LivnWXJrY=`.
Effective MainPID 1096484; final persistent drop-in uses that package. Producer
timer is active and triggered at 19:12:06. Receipt `pv-activation-1909.json`
contains metadata-only effective/persistent chain, pin and timer/service fields.
No broad Nix/HM switch, bridge credential adoption or unrelated restart by QA.

Bounded admitted commands (existing checker):

```sh
BROWSER_EXECUTABLE=$(command -v google-chrome) uv run --frozen --group delivery \
  python tests/check_gauge_wire.py --out docs/evidence/ui-browser-qa-2026-10-07/final-source/accepted
BROWSER_EXECUTABLE=$(command -v google-chrome) uv run --frozen --group delivery \
  python tests/check_gauge_wire.py \
  --deployed-build /nix/store/95ll5i712vsrzri85hsv675ljjr09kk2-anthropic-throttle-proxy-0.1.0 \
  --out docs/evidence/ui-browser-qa-2026-10-07/deployed-45b7d0e
```

No source/CI/browser approval fabricated. A Chromium AX check is not a physical
screen-reader or WCAG certification. pT owns source landing, pV activation and
rollback; QA owns executable evidence. Central deployment remains separate.
