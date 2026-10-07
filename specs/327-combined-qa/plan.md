# Combined accounting independent QA — 07/10/2026

Canonical contract: Notes-2363 Throttler/COMBINED-THROUGHPUT-2026-10-07.md.
Fresh `327-combined-qa` from protected main; old 325 WIP, #324 exact source heads,
AX-failed/refusal receipts retained. #324 `a6a59a3` current required checks and
zero threads verified; source-only merge receipt stored before continuing.

Hypothesis: one authoritative Pi completion journal, summed over a common
trailing 60-second wall-clock window, produces 30 output tokens/s for 600+1200
outputs regardless of request duration, without counting prompt/cache/proxy
copies or narrowing the headline by selected workload.

Falsifiers: request-speed mean/variable denominator, aliases double count, proxy
copies added, source picker changes the headline, boundary/future/invalid records
make a healthy zero, incomplete/missing/stale evidence becomes complete coverage,
replacement retains obsolete data, partial write is accepted as complete, or UI
render reads/parses the journal/network rather than the background cache.

## Bounded tasks

1. Independently build minimal timestamped journal fixtures and assertions on
   pT's real reader/projection seam once named. No implementation/CSS/backend
   edits. Fixtures contain only synthetic allowlisted usage facts.
2. Require: common-window arithmetic/output-only; lower/upper boundaries; idle,
   warmup, stale/missing; negative/nonfinite/future/malformed; partial final line;
   atomic file replacement; duplicate source/request policy; Codex aliases and
   explicit observed-provider/non-Pi/other-host coverage.
3. Complete the repaired existing #324 AX source/live checker under one admitted
   job, then adapt only its acceptance expectations to the new combined gauge.
4. Actual combined candidate tests and browser check must use exact source/hash,
   be labeled synthetic where journal fixtures are synthetic, and preserve all
   geometry/AX/focus/state/no-network oracles. Full pytest/Ruff via admitted local
   or current-head CI. No agents, synthetic inference or device/auth effects.
5. pV alone activates exact accepted immutable build. Read-only post-deploy
   acceptance must bind source, served CSS/UI, native producer/domain and units;
   no summing proxy copies, no broad deploy. No all-green claim without receipts.

Durable reports/evidence: `docs/COMBINED-QA-2026-10-07.md` and
`docs/evidence/combined-qa-2026-10-07/`. Coordination stays in UI-TEAM. All
admission waits before job acquisition; no slot manipulation/control render.
