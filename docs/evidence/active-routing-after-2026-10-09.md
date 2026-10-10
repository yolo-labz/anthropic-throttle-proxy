# Active Routing — after-measurement (2026-10-10, 01:1x BRT)

The "no stall" proof for S1 (PR #338) measured on the **pure API lane** — the
fleet's MiMo traffic migrated off the Desktop membership this night (NixOS PR
#2730 merged `41a823de`, activated live+boot as generation 2262).

## Before → after

| Lane | Burst (n=8, conc=4, max_tokens 8) | Result | p50 total | p95 total |
|---|---|---|---|---|
| `:8774` Desktop-membership bridge (before) | `mimo-v2.6-flash` | **2×200, 6×503** | 0.0016 s | **8.166 s** |
| `api.xiaomimimo.com` PAYG API (after) | `mimo-v2.6-flash` | **8×200** | 3.534 s | 5.175 s |

Raw: `burst-8774-before.json` / `burst-api-after.json`. Harness:
`load/burst_probe.py` (keys via gated helper piped to `--key-file -`; never
stored — the records carry usage totals only).

Reading:

- The stall class is **gone**: 0 failures at concurrency 4, where the old
  bridge queue-wait-timed-out on 6 of 8 and the 2 survivors paid an 8.17 s
  tail. The after p95 is **generation-bound** (first-token + decode on the
  vendor), not queueing: every request completed 200 and the lane's
  account-level limit is 100 concurrent (`accountRateLimit`), so no admission
  shadow touches 4 in-flight.
- The same-bearer wait S1 removed (PR #338) plus the lane's own headroom are
  what make failover-scale latency possible; on this lane there was nothing to
  fail over to *or* from — the migration removed the bottleneck instead.
- Cost of the proof burst: 112 prompt + 63 completion tokens ≈ $0.00002 on the
  delivered MiMo card (PR #339 rows), i.e. the cost accounting now reports the
  truth instead of 107×/268× Opus-fallback money.

## Conditions / controls

- Ran 01:1x BRT with the wallet fresh (`pi-xiaomi-payg-key` gate open, receipt
  ≤300 s old) — the 300 s freshness gate stayed intact throughout.
- TLS: the desktop job lane is `env -i`; the harness needs `SSL_CERT_FILE`
  exported (a bare run fails `ClientConnectorCertificateError`, which is a
  harness environment issue, not a lane signal).
- The retired `:8774` bridge units were stopped after activation (they were
  `not-found active` orphans of the removed unit files; inflight 0 at stop).

## What is now true

1. Seats default to `xiaomi` (pure API) — `~/.pi/agent/settings.json`
   `defaultProvider: "xiaomi"`; the legacy desktop providers stay declared but
   are never offered.
2. Desktop membership: renewal off, prepaid to 21/11, refund chase in flight
   (order ODBR2026092300224637548952920).
3. Spend accounting is truthful per lane/seat (spec 337 + PR #339 rate rows).
4. Token Plan package fork (Pro $50 on 23/10 vs Max $100 renew) remains
   Pedro's one-word call; PAYG wallet (~$52) carries the fleet meanwhile at
   the measured ~$3.4/h burn.
