# Independent combined-output QA — 07/10/2026

## Verified separate slice

- Fresh `327-combined-qa`, from main `42e5e69`, then fast-forwarded to `353d30a`.
  Previous 325 WIP, #324 heads/receipts, earlier overflow/AX instrumentation
  failures and admission refusals were preserved, not rewritten.
- #324 exact `a6a59a3` required checks passed; zero GraphQL review threads and
  CLEAN. Normal source-only squash verified **MERGED `353d30a61adb3c25eb0d863ce74bba8d58cba286`**
  at 19:43 BRT. See `324-exact-head-ci.json` / `324-merge.json` in this packet.
- After the loaded heavy slot refused the first repaired packet at 19:43, the
  slot became inactive at 19:49. One bounded admitted packet then executed the
  repaired full Chromium AX source and separate deployed checks. Both completed.
  Geometry is **1366/1366 and 390/390**; all quota annotations appear as unignored
  StaticText; polling, focus/details, state fixtures and render-network sentinels
  passed. Source receipt: completed/checks_executed=true, findings=[]. Deployed:
  completed=true, four polls, no unexpected network or console errors.
- This verified previous #323/`45b7d0ef` UI source and exact deployed
  `/nix/store/95ll5i712vsrzri85hsv675ljjr09kk2-anthropic-throttle-proxy-0.1.0` UI/CSS
  bytes plus actual Desktop weekly quota producer. **It is not combined-gauge
  source or live acceptance.**

Receipts/DOM/PNGs and command output live under
`docs/evidence/combined-qa-2026-10-07/{legacy-source-ax,legacy-deployed-ax}/`.
No raw operational journal rows, identities, costs, credentials or content copied.

## New contract and executable oracle

One Pi client completion journal only. Add output tokens over the fixed shared
60-second interval; do not add proxy/local-central copies, prompt/cache tokens or
average per-request generation rates. Observed Pi on this host is not all-host or
non-Pi coverage. Workload selection/HTMX must not narrow the headline.

`tests/check_combined_accounting.py` binds directly to pT's named
`output_usage.read_usage` / `refresh` / `cached` seam; it does not use a second
reader implementation as the system under test. Its synthetic journal fixtures
assert 600+1200=30, duration/input/cache exclusion, Codex aliases/direct Desktop,
repeat reads/same-inode alias, declared **[sample-60, sample)** boundaries, idle,
warm-up, missing/negative/nonfinite/future/missing-output, partial write, file
replacement, unchanged cache age, stale projection and no getter I/O.
`tests/test_combined_independent.py` carries the same runnable assertions into CI.

At 19:55 the first bound candidate attempt was refused before execution. At
**19:58**, a bounded admitted job ran the real candidate reader: **16 independent
assertion cases PASS; pytest wrapper 2 passed in 0.06s**. Reader SHA-256
`54668c9fd9277bc66a3bc6a460f3249aa4b726b3ea136f2cc4b8e5860274924b`
was identical before/after. See `candidate-arithmetic.json`, `candidate-pytest.txt`
and `reader-{before,after}.sha256`. It is still a fingerprinted uncommitted 326
candidate, not immutable merged-source or live acceptance. No pT source edits.

Separate `tests/check_combined_browser.py` reuses repaired geometry/AX/capture and
keyboard helpers, asserts combined30 despite sibling120/local proxy9000 outputs,
source picker and HTMX consistency, cache-only/no-network rendering, and idle/
stale/error/warmup/unsupported UI. It has a separate read-only exact-deployed mode.
The candidate browser launch after 20:07 was refused before execution (loaded
shared slot), so no new browser receipt/pass is claimed. Subsequent control
probes for formatter/metadata/delivery were refused with exit74 (small pool
unavailable, 25-second caller deadline, nothing executed). At **20:13 BRT**, the control lane granted final formatter/lint: all three new
checks pass Ruff; final deployed-receipt enhancement is linted. Normal new-slice
hooks/PR/current-head CI and final-source/live combined acceptance remain pending,
not green. At that checkpoint no combined source PR existed, and the heavy slot
was active since 20:13:29.

The normal 20:14 commit hook executed and correctly rejected two new test-helper
clones (129 and 51 tokens). QA refactored the shared cause: extracted the existing
isolated render app/network guards into `check_gauge_wire.render_app`, reused by
legacy and combined checks; no suppression or bypass. Hook re-verification is
pending, not an asserted fix. Clone diagnostic is durable in this packet. The subsequent final lint/restage/normal-hook/push command was
admission-refused (exit74, nothing executed); commit/PR delivery is not asserted.

## Latest verified QA progression

At **20:20 BRT** Ruff passed after the shared guard extraction. At **20:22 BRT**
normal commit hooks re-executed: aislop100/zero errors, clone ratchet zero relevant/
zero regressions, alignment zero findings. Local QA commit **c2f9f3b** exists;
not yet pushed/PR-tested while accepted combined source dependency is outstanding.

An admitted candidate combined browser run at 20:20 executed and stopped on QA's
case-sensitive rendered-label assertion: existing CSS `text-transform: uppercase`
affects Chromium `inner_text`. Value30 and preceding HTMX/focus checks passed,
but this is **failed instrumentation, not a passing UI verdict**. Immutable
failure command and receipt remain at `candidate-combined-browser/`. The lookup
now uses DOM `text_content`; no frontend/CSS change. A repaired attempt uses a
separate directory. Source and deployed combined acceptance remain pending.

At last 20:24 source metadata, pT `344f1e2` existed but template/checker follow-up
was uncommitted; no source PR existed. Final reader source changed since the
first arithmetic receipt (format/source delta); re-run arithmetic and browser
on final immutable source rather than relabel the old fingerprint.

## Repaired combined candidate actually executed

At **20:25 BRT**, a normal admitted packet re-ran all **16 accounting cases**
and the repaired browser against pT's source. The new immutable receipt directory
`candidate-repaired-combined-browser/` records **completed=true**, **11 polls**,
keyboard local/touch sibling selection keeps **30**, ignoring sibling120 and
seeded local proxy9000 outputs; 1366/1366 and390/390, hidden quota AX, no console/
unexpected requests, journal/collector/network render guards. Fixture states:
idle0, stale/error/warm-up/unsupported —. Source fingerprints unchanged during
this run: reader `5172d8bc…`, template `3297ea5f…`. This passes real-source/
synthetic-journal acceptance only. First failed instrumentation receipts retained.
Focused-table screenshots prove geometry but are not a dial visual review; final
immutable check explicitly scrolls the gauge into view for capture.

At **20:26 BRT**, pT published source **PR#327 / 742fafd22553501017e485824534fd5da930034c**.
The branch suffix327 here is QA; GitHub PR#327 is pT source. Current-head source
CI and accepted main binding still must be verified, then normal QA PR delivery
and pV exact deployed acceptance. The live health checkpoint remains old95ll5i712.

## Final-source binding and normal delivery underway

At **20:28 BRT**, QA rebased its two unpushed commits onto pT's published
`3a398791ee5b49478dac9fb920014ddf81c4feb7`. No pT backend/CSS edits; all source
bytes come from that candidate. Mergiraf's automatic helper merge was reviewed
and found a moved lexical fixture call; QA moved only the cache-injection patch
back into its legacy caller, leaving shared refresh/network guards intact. A
small CI regression asserts the helper preserves an injected accounting fixture
and refuses collection. Reviewed source diff stored as `rebased-render-helper.diff`.
Normal Ruff passes after the resolution; no unreviewed automerge shipped.

The bounded full pytest + final combined/legacy browser packet was refused
before execution (loaded heavy unit race). Its final immutable/dial-visual/helper
execution is pending, not conflated with the earlier candidate run. QA will
publish a normal draft dependent PR and require exact-head CI, then re-bind
accepted main and normal merge. pT source#327 current head3a39879 was still
OPEN/BLOCKED on real required CI at20:28; neither source nor runtime is accepted
from historical green.

## Current stacked QA source dependency

Normal QA draft **PR#328 / 7c1ea73** was pushed at **20:30 BRT** (different from
pT source PR#327). That exact head's required Ruff+pytest and slop passed;
required scan still pending at20:32. No source/live acceptance inferred from it.

pT advanced source#327 to **da08c3318310dc7bc64300af44221d65428ac491** (completion
age expires inside an otherwise fresh cache). At **20:33**, QA fetched/merged
that source parent without modifying its backend/CSS bytes; source diff against
the fetched parent is empty. Added an independent case: completion119s→121s must
expire with only2s cache age. This is a new **17th** case pending execution, not
part of the earlier16-case pass. Current immutable parent full/browser/helper
execution remains admission-blocked; the helper's normal CI regression now
runs as the third pytest case. Parent and QA current-head CI remain real gates.

## Independent observational reference (not acceptance)

A bounded 2 MiB read at **19:49:07 BRT** observed 92 completion events and 89,813
output tokens /60 = **1,496.8833 accounted output tokens/s**. Six provider aliases
were observed, with zero invalid tail rows, complete trailing line and >60s tail
coverage. See `live-journal-reference.json`. This is an aggregate checkpoint only,
not a constant speed, the pT reader, streamed counts, synthetic inference, or proof
of the live UI's combined semantics.

## Latest exact-head gate / admission checkpoint

At **20:35 BRT**, QA **draft#328 / 65440d8** was clean and pushed through normal
hooks; source diff versus fetched pT/da08c33 empty. Source#327 exact da08c33
Ruff+pytest/slop passed; required scan pending. QA654 current required tests/slop/
scan pending; old7c1ea73 green is historical. GitHub API verified hosted runners
executing coverage/scanner, not a stuck vm103 pool. Live still95ll5i712.
Subsequent metadata probe was refused before execution, exit75:
`MemAvailable=24951268kB below 24GiB` (also prior exit74 control deadlines).
No fallback, ignored gate, in-job waits or pressure stress. Follow-through remains
blocked on actual required CI, admitted final packet and pV activation; all
successful/failed/refused checkpoints are distinct. No all-green assertion.

## Admitted expiry/helper/visible-gauge checks and source gate

At **20:39 BRT**, admitted own locked environment ran **17 independent cases
PASS**, reader `7d2ccc07…` (da08c33), and **8 pytest passes** (three oracle/helper
cases plus sanitizer checks). Actual browser with gauge scrolled into view
completed normally in `source-da08-gauge-visible-browser/`: both widths, full AX,
selection/polls, guards and all states. The 390px dial PNG was visually inspected:
30 tokens/s, common60, completion/partial local-Pi scope, unobscured dial/footnotes.
This is source/synthetic acceptance, not live UI. Earlier failed instrumentation,
focused-table visual limitation and refusals remain unchanged.

QA654 required scan subsequently FAILED on upstream parent's real quality gate,
not runner/admission or skipped tests; sanitized scanner failure summary retained.
pT fixed those source findings in **c16a91c012cf302fe5ad6e3b744e157c42b87ad2**.
At **20:41**, exact source#327 c16a91c **all three REQUIRED checks passed**.
QA fetched/merged that parent; backend/CSS diff versus it empty. This new source
hash must still be re-run/bound, and QA normal updated head must earn its own
required CI before safe merge. No all-green/live claim from source-only CI.

## Exact c16 source acceptance completed (not deployed)

An admitted bounded job completed at **20:46 BRT** on unchanged c16 source:
**17 independent accounting cases PASS**, reader SHA-256
`4872db12cf6ea500fc39299f605d0a8651290677c96f14df06b07d8e92736464`;
**full pytest 2,140 passed** (290 existing warnings, 93.74s); separate visible
combined and legacy/shared-guard browser receipts completed=true, no console/
unexpected network and legacy findings=[]. Full AX and 1366/390 geometry held;
visible dial with30/60s and all stale/error/unknown/idle fixtures passed. No raw
journal or runtime patches. Successful receipts under `source-c16-*` are separate
from all earlier failures/refusals. Actual executable source acceptance finished.

At20:46 QA **77b1530 all REQUIRED checks pass**, source#327 c16 all REQUIRED
pass but still OPEN, runtime still95ll5i712. This report/receipt delta will advance
QA head and earn fresh exact-head CI; do not reuse77b checks. Accepted-main/
normal merge and pV deployed combined acceptance remain separate unfinished gates.

## Pending runtime and reversal

pV alone activates the exact accepted immutable combined build. QA must bind the
new source/build hashes, persistent/effective unit and served CSS/DOM, then run
read-only admitted browser checks; no broad deployment/auth/device changes.
Current `95ll5i712…` acceptance remains old selected-speed UI. No final all-green
claim. QA receipts/test delta is reversible by one normal `git revert` PR; no
operational state was mutated. Canonical coordination updated in UI-TEAM.
