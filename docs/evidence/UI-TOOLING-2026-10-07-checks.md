# UI tooling research receipts — 07/10/2026

Baseline `66ad9dc9df82887bccc90572ae0d84dadf5df5fe`; desktop control calls
07/10/2026 17:40–17:51 BRT. No credentials, raw logs, account payloads,
private screenshots or provider inference were collected.

## Commands actually completed

- `date '+%d/%m/%Y %H:%M %Z'`: 07/10/2026, starting 17:40 `-03`.
- `git rev-parse --show-toplevel`: feature worktree ending
  `anthropic-throttle-proxy-320-ui-tooling-research`; branch
  `320-ui-tooling-research`. Source remained at baseline during research.
- Impeccable versioned `context`: ran once, reported missing PRODUCT/DESIGN;
  no setup mutation. `engine-probe`: `impeccable-engine 0.1.11`.
- `git -C /home/notroot/.local/share/impeccable/skill-v4.5.0
  rev-parse 'skill-v4.5.0^{}'`: `508d7e8955de3b3caf2d8676e85206723d41a887`.
  `status --short`: clean.
- Read-only upstream `git ls-remote --tags`: maximum semantic tags
  `skill-v4.5.0`, `engine-v0.1.11`, `cli-v4.1.0`; separate release tracks.
- Installed skill SHA-256 **50d4b14134fc06daeac0aab6960800f171ef85250d3bcce6f246cf81e824463f**:
  matches canonical tagged raw SKILL.md (HTTP 200).
- Installed `/home/notroot/.impeccable/bin/0.1.11/impeccable` SHA-256
  **0221607e1f535af937ea267c347b1f90233b85dc2563cbd2eeefdf42e0e5c594**:
  matches canonical engine release `.sha256` (HTTP 200). No download/upgrade.
- `impeccable detect --help`: usable JSON switch **`--json`**; **not**
  `--ruleset quality --format json`. Actual detection was not executed.
- `playwright --version`: **1.58.0** global Python tool; launcher uses
  `/home/notroot/.local/share/uv/tools/playwright/bin/python`.
- That interpreter's `-m playwright install --dry-run`: requires Chromium
  **1208**, headless shell **1208**, Firefox **1509**. All three cache directories
  absent; existing Chromium **1200**/Firefox **1497** do not satisfy that pairing.
  Feature `uv.lock` instead pins Playwright **1.63.0**. No browser download/launch.
- Global tool interpreter running `browser-receipt.py --help`: exit 0; options
  `--url`, `--out`, `--engines`, `--widths`. Main venv lacks Playwright.
- `node tests/dashboard-refresh-check.mjs`: exit **0**, output
  **`PASS single-script invariant + server-side freshness stamp`**.
- Main venv metadata, read-only: pytest **9.0.3**, Ruff **0.15.13**, aiohttp
  **3.13.5**, Jinja2 **3.1.6**, prometheus-client **0.25.0**, Playwright absent.
  `command -v axe pa11y impeccable`: no such commands on PATH; the versioned
  Impeccable launcher is addressed explicitly.
- npm package-registry GET (not model/API inference): axe-core **4.14.0**, MPL-2.0,
  integrity `sha512-9WTZxEjsZ7b13TH8JPmbV2z8CHbl80/2hm3XPEG4JgNdQLK81IBRXmSxHfMAOkSqQeRxT/0dwNDz2GOm3zzpcQ==`.
  No package installed. Global npm inventory exceeded its **15 s** deadline;
  no conclusion about cached Node module availability.

## Bounded synthetic source checks

Run from this feature worktree. This block uses the cached main interpreter
**read-only**, imports only test helpers, copies seven small source files into
owned temporary storage, and neither contacts a provider nor launches a browser.
It documents the baseline gaps; when implementation lands, its inverse assertions
must replace these counterexamples in the owning tests.

```sh
PYTHONPATH="$PWD/src:$PWD/tests" \
  ../anthropic-throttle-proxy/.venv/bin/python - <<'PY'
import runpy
import shutil
import tempfile
from pathlib import Path

contrast = runpy.run_path('tests/test_ui_contrast.py')
declarations = contrast['_declarations']()
resolve = contrast['_resolve']
ratio = contrast['contrast_ratio']
for token in ('--ctp-overlay0', '--ctp-overlay1', '--muted'):
    measured = ratio(resolve(token, declarations), resolve('--panel', declarations))
    print(token, 'on --panel', round(measured, 4), 'normal_text_AA', measured >= 4.5)
assert ratio(resolve('--ctp-overlay0', declarations), resolve('--panel', declarations)) < 4.5

gate = runpy.run_path('specs/227-dashboard-audit/verify-live.py')
with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    for name in gate['FILES']:
        target = base / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path('src/anthropic_throttle_proxy') / name, target)
    before = gate['source_hash'](base)
    (base / 'ui/signals.py').write_text('# altered gauge math\n')
    after = gate['source_hash'](base)
    assert before == after
    print('receipt_hash_unchanged_after_signals_change', before == after)
PY
```

Observed exit **0**:

```text
--ctp-overlay0 on --panel 3.5943 normal_text_AA False
--ctp-overlay1 on --panel 4.7508 normal_text_AA True
--muted on --panel 7.8856 normal_text_AA True
receipt_hash_unchanged_after_signals_change True
```

Classification: **source/arithmetic counterexamples only**, not rendered
accessibility, live throughput, real producer data, deployment or an incident.

## Three public search tracks and source receipts

SearXNG `/search?...&format=json` queries, each with one simpler retry after
`unresponsive_engines`:

1. `Impeccable skill-v4.5.0 engine-v0.1.11` → `Impeccable documentation`.
2. `htmx 1.9.12 polling focus preserve queue swap` → `htmx documentation`.
3. `WAI WCAG 2.2 tabs meter status axe Playwright` → `WAI documentation`.

Service answered with useful results, including upstream releases, HTMX and W3C.
Brave/Google/Startpage/Yep reported HTTP errors/access denial and Qwant CAPTCHA.
Canonical reads followed; no public-engine fallback or new Z.AI request.
Later simple queries `htmx polling focus` and `WCAG 2.2 status messages` also
returned HTMX and W3C results; they were not model delegations.

| Canonical source | Result / specific evidence |
|---|---|
| Tagged Impeccable SKILL.md, scripts/VERSION and engine checksum sidecar | HTTP 200; skill 4.5.0, pin 0.1.11, digests above |
| `https://v1.htmx.org/attributes/hx-swap/` | HTTP 200; defined IDs for input focus; focus-scroll modifier |
| `https://v1.htmx.org/attributes/hx-trigger/` | HTTP 200; `every` polling and default event queue `last` |
| `https://v1.htmx.org/attributes/hx-preserve/` | HTTP 200; ID matching and input focus/caret limitations |
| `https://unpkg.com/htmx.org@1.9.12/dist/htmx.js` | HTTP 200; actual pinned `processPolling`, active-element-ID restoration |
| W3C WCAG22 target-size-minimum / contrast-minimum / status-messages | HTTP 200; 24px minimum with exceptions, 4.5:1 normal text, announcements without unnecessary interruption |
| W3C APG tabs pattern | HTTP 200; real tabs require tab/tabpanel semantics and keyboard contract; source selector is currently navigation |
| `https://playwright.dev/docs/accessibility-testing` | HTTP 200; Node `@axe-core/playwright` integration, explicit manual-testing limit |
| GitHub unauthenticated release/ref API | HTTP 403 rate limit; git refs/raw sidecar used instead |
| `https://playwright.dev/python/docs/accessibility-testing` | HTTP 404; do not prescribe a nonexistent Python AxeBuilder integration |
| `https://prometheus.io/docs/practices/instrumentation/` | HTTP 403; not used to support the collector proposal |
| Guessed Prometheus raw docs (main/master) | HTTP 404; no primary-source claim inferred |

## Admission and evidence exclusions

At ~17:46 BRT, first heavy request: **exit 75**, admission refused
`[reopen-pending]: 17 s of below-threshold remaining`. Second heavy request:
**exit 1**, `Failed to start transient service unit: Unit desktop-job-slot.service
was already loaded or has a fragment file.` No job ran, no cgroup allocation was
claimed. The queued scan command initially had legacy detector flags; help
inspection corrected the report before delivery. Never reuse those flags.

The refused payload would have run source tests, Ruff and a static detector; no
results or detector JSON exist. These heavy workloads were not moved to the
control lane. Full tests are delegated to **normal repository CI**, not another
agent. No browser screenshots, contrast scan, accessibility score, activation,
Nix pin or live unit change is asserted by this receipt.
