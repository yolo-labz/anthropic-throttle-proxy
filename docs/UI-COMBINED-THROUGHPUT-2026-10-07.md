# Combined output throughput — 07/10/2026

## Implementation and coverage

Worktree `326-combined-throughput` starts from fetched `42e5e69`. `output_usage.read_usage(path, *, now, max_bytes)` is the immutable snapshot seam; `refresh` reads off the event loop in the existing five-second collector, and `cached` is the pure render projection. `THROTTLE_PI_USAGE_PATH` overrides the installed journal; `PI_USAGE_STATE_DIR` is respected. No producer, proxy/hot-path, model, dependency, Nix or service mutation.

The installed `pi-usage-telemetry.ts` records provider-reported completion output. Proxy rings lack aligned timestamp identity and omit direct Desktop/Codex; they also duplicate journal-accounted proxied turns. The gauge deliberately uses **one client domain only**, excluding every proxy/self/central copy. Repeated refresh replaces the snapshot. File device/inode binds source identity; replacement/concurrent mutation cannot perpetuate an old healthy sample.

Count exact integer output in `[sampled_at-60, sampled_at)` and divide once by 60 wall-clock seconds. Ignore input/cache/cost/durations. A bounded 2 MiB tail must establish the boundary; empty, unsupported, malformed, partial, future, warmup or insufficient-tail evidence is unknown. Newest completion older than 120 seconds is stale; this turn-only writer has no heartbeat. A verified idle window is zero. Cached projections age out after 15 seconds without refresh. Neither a touch nor HTML refresh freshens old completion evidence.

Headline and ARIA scope stay combined through local/sibling/default/hidden-primary selection and HTMX polls. Every reading visibly discloses **partial coverage: Pi on this host only; non-Pi / other hosts unmeasured**, with completion-accounting rather than an instantaneous stream claim. Codex provider aliases are event labels, not separate aggregate counters. Native non-Pi Codex/Claude and other hosts are not claimed covered.

## Evidence so far

- Baseline route file matched fetched main by Git object hash. New single-domain fixture was made available without changing that baseline route: expected `1800/60=30` failed (`red-baseline-route.txt`, exit 1). Baseline selected-proxy behavior ignored the client domain.
- Admitted targeted tests: **77 passed** (`targeted.txt`); Ruff check passed. Arithmetic, boundaries/output-only, idle/unknown, invalid/future data, partial writes, tail bounds, source aliases/repeated reads/replacement, cache aging, proxy/self copies, hidden primary and HTMX routes covered. One additional default-clock regression is subsequently written, not yet counted in that receipt.
- Actual allowlisted source checkpoint **20:00:01 BRT**: 53,464 output /60 = **891.0667 accounted tokens/s**; observed codex-a/b/c, openai-codex and zai in that window. Direct Desktop was observed in earlier journal traces, not necessarily this minute. This checkpoint is not a fixed target, deployed reading or independent acceptance. No raw journal, seats, costs or content persisted.
- Full first coverage run: **2,133 passed, 1 failed**. A nested pytest process overrides PYTHONPATH and resolved the borrowed venv's old editable package, where the new reader is absent. Own locked worktree environment is required; no exclusions/threshold changes. That failed run is retained, not relabeled green. Reader line coverage in that attempt was 98%.
- Heavy-slot loaded-unit refusals and a control-slot deadline refusal occurred before execution. No admission bypass or shared-cache/worktree cleanup.

## Validated intermediate source

Own locked worktree environment fixed the nested editable-import failure. Admitted **2,135 full tests passed**, Ruff check/format passed, and the updated synthetic browser packet passed: 30 tokens/s through HTMX/source changes, selected workload stale/error cannot replace it, separate client-cache stale/unknown states, preserved focus/keyboard/native scrolling and widths 390/390 and 1366/1366; findings/console/unexpected network all empty. Receipts are under `docs/evidence/combined-throughput-2026-10-07/`. This intermediate packet precedes validation-helper extraction, reader fingerprint metadata and rebasing the newly merged independent AX oracle; final-head rerun still required. No deployed/live acceptance follows from it.

## Final-source acceptance

Rebased onto independent AX-oracle main `353d30a`; reviewed the auto-merged checker diff and retained `rebase-review.txt`. Final executable packet: **2,135 full PASS**, output reader **100% line coverage**, Ruff check/format PASS. Final synthetic browser also PASS: full quota AX oracle, retained focus/poll state, native ArrowRight scrolling, coverage text/30 tokens/s, selected-sibling failure cannot replace the dial, client cache stale/unknown and 390/390 + 1366/1366 without document overflow. Exact UI/reader hashes are in `browser-final/receipt.json`; live list is empty and scope explicitly synthetic/not deployed. Source caption contrast tokens and scroll CSS unchanged. Normal initial commit hooks passed: slop 92 with only inherited routes warnings; alignment zero errors/warnings. No automated model approval claimed.

Source PR **#327** initially opened at `742fafd22553501017e485824534fd5da930034c`. A final admitted `browser-742fafd` run passed against that committed head including the template comment and async reader fingerprint: exact reader/UI hashes retained, findings empty, 30 tokens/s/partial scope, two HTMX polls, AX, native scroll and both widths passed. README records the new environment seam and coverage boundary. This follow-through is documentation/evidence only; source byte equivalence with the tested head is checked before delivery.

## Completion-age boundary hardening

Final trace found a narrow freshness gap: a 119s-old completion could age past its 120s bound while the 15s cache still appeared fresh. Hypothesis: render-time completion-age validation, without I/O or refreshing sampled_at, closes that gap. Exact old-reader red preserved (`red-event-expiry.txt`: 121s still seen=True); new regression passes. Deleting the journal on the next refresh also invalidates a previous healthy snapshot. Admitted final packet **2,137 full PASS**, reader100% lines, Ruff PASS and matching `browser-event-expiry` full-AX/HTMX/focus/geometry PASS with reader SHA-256 `7d2ccc07a36c7f881c84fbc2e9030d6dd9695d242ce37df6c25ec36114264d53`. These supersede earlier 2,135-test/fingerprint source acceptance, not runtime/live acceptance. No telemetry suppression or threshold relaxation.

## Required quality repair — not yet accepted

Historical 3a39879 gate ERROR was bound CE0919a4c2→analysisdea8dd03→that SCM: coverage93.6/duplication0 pass; complexity17>15 and two dataclass return-type issues. Exact da08c33 scan run37702817772 subsequently bound CEe57bad44→analysis685648ee→da08c33: **ERROR**, four actual issues (same complexity and three `replace` returns), with CI coverage **2,137 PASS**. Both failure receipts/logs retained. These are real required failures, not an advisory approval issue.

Repair keeps the immutable Snapshot/dataclass API: move positive/capped read-bound validation into existing `_read_tail` and explicitly type `dataclasses.replace` results with `typing.cast(Snapshot, ...)`. No guard lost, no runtime behavior or metric/exclusion/threshold change. The patch is currently source-only; its targeted/full/browser requests were admission-refused (loaded shared unit, then MemAvailable24,025,056kB below24GiB). No repair pass, merge or deployment is claimed until actual acceptance and its exact historical analysis exist.

## Outstanding delivery

Admitted repair checks and exact superseding-head required CI/historical Sonar binding remain. Protected safe merge is not claimed yet. Runtime pV and independent QA pW retain activation/live acceptance custody; their old selected-gauge acceptance is not acceptance of this source. Reversal after merge is one ordinary revert PR of its squash; no runtime deployment happened here.
