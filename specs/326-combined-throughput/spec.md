# Combined accounted output throughput

## Scope and hypothesis

The installed Pi completion journal is the one existing accounting domain covering concurrent direct Desktop, Codex and proxied Pi provider turns; a cached sum of its exact output counts in one trailing wall-clock minute fixes the selected-proxy dial without counting proxy copies.

Falsifiers: a reader uses request durations, counts input/cache tokens, adds a proxy tier, silently narrows the gauge with the picker, refreshes a sample in a render, accepts incomplete/malformed telemetry as zero, or claims non-Pi/other-host coverage.

## Contract

- One authoritative source: `THROTTLE_PI_USAGE_PATH`, default `~/.local/state/pi-harness/usage.jsonl` (respect `PI_USAGE_STATE_DIR`). Proxy health counters are never added to this journal: they overlap its proxied turns and cannot identify direct Desktop/Codex completion coverage.
- Headline = sum of integer nonnegative provider-reported `output` for completion timestamps in `[end - 60, end)` divided by exactly 60 wall-clock seconds, independent of providers/accounts/request durations and of workload selection.
- The producer records completion usage, not continuously generated stream tokens. Only aggregate fields are published; no raw ledger rows, seat names, costs, prompts or credentials.
- Source identity is the opened file's device/inode. Each refresh replaces the snapshot, never accumulates previous reads. File replacement and concurrent mutation cannot preserve an old healthy snapshot.
- A bounded 2 MiB tail must establish history at/before the window start. An absent, empty, malformed, nonfinite, negative, future, partial-write, truncated-without-window-boundary or warming-up feed is unknown, not zero. Newest completion older than 120 seconds is stale: this turn-only producer has no heartbeat, so quiet and unavailable cannot be distinguished forever.
- A verified complete idle interval is measured zero. Cache-only UI projections become stale after 15 seconds without background refresh; rendering does not touch the journal or any network.
- Every measured reading visibly says **partial coverage: Pi on this host; non-Pi / other hosts unmeasured**. Counting all provider labels in that journal is not a claim of complete fleet or native-Codex/Claude coverage.
- Picker continues to select other workload signals; the combined gauge and its coverage persist through HTMX polling, including unavailable selected siblings and hidden local panels.

## Acceptance

Two concurrent providers reporting 600 + 1,200 output tokens in the common minute produce 30 tokens/s. Adding duplicate/self/local/central proxy counters cannot change that figure. Test boundaries, idle, warmup, stale/missing/empty, invalid values/time/provider/schema, bounded tails, partial writes, file replacement, repeated refresh, cache aging and real HTMX routes without render-time I/O. Preserve existing caption contrast and scroll containment. Required executable checks and exact-head CI precede normal protected safe merge; deployment/live acceptance remain separately owned.

## Source trace

`history.observe_tokens` receives normalized SSE/JSON usage at request completion; health exports relative 10-second buckets without timestamp identity. `presentation.apply_workload` currently replaces the local gauge with one sibling. Desktop quota is not output telemetry. `modules/home/lib/pi-usage-telemetry.nix` installs a turn-end journal writer with UTC `ts`, `provider` and exact `output`; other fields are irrelevant here. The installed journal contains Desktop, Codex account labels, OpenAI Codex and Z.AI events. No `pi-usage-report` executable/source was found on this seat's PATH or in the searched Nix sources; the actual installed writer/journal, not a guessed report command, is the accepted seam.
