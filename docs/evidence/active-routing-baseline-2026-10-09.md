# Active Routing — baseline measurement (before), 09/10/2026 15:5x BRT

Harness: `load/burst_probe.py` (bounded burst; synthetic "Reply OK" payloads,
max_tokens 8; keys resolved through gated helpers piped to `--key-file -` and
never echoed or stored).
Metric snapshots: `m8766-before.txt`, `m8774-before.txt`,
`active-routing-baseline-lanes-before.json`. Raw bursts: `burst-8766-before.json`,
`burst-8774-before.json`.

## Before (current build, lanes as-is)

| Lane | Burst (n=8, conc=4) | Result | p50 total | p95 total |
|---|---|---|---|---|
| `:8774` MiMo Desktop weekly bridge | `mimo-v2.6-flash` | **2×200, 6×503** | 0.0016 s | **8.166 s** |
| `:8766` Z.AI coding PaaS | `glm-5.3-flash` | **8×429** | 0.0019 s | 0.010 s |

Reading:

- `:8774` is the operator's complaint in one number: 75 % of a tiny burst fails
  after an **8-second stall** — the lane queues, waits, and answers 503 instead
  of spreading the load or failing over fast. This is the same-bearer
  wait-and-retry shape S1 removes when a sibling credential exists.
- `:8766` fails **fast** (429 in ~10 ms) — healthy failure shape (the lane's
  upstream is rate-limited/5h-exhausted right now); fast refusal is not a stall.

## After (to re-run post-deploy)

```sh
# same commands, then diff status_counts + p95
pi-mimo-desktop-subscription-key | python load/burst_probe.py \
  --url http://127.0.0.1:8774/v1/chat/completions \
  --model mimo-v2.6-flash --n 8 --concurrency 4 --max-tokens 8 \
  --key-file - --out /tmp/burst-8774-after.json
```

Success criteria (design § Measurement plan):

1. p95 total on a pushing-back lane drops from the stall scale (seconds) to the
   failover scale (sub-second when a healthy sibling exists).
2. `rate-pushback-retry` log lines show the retry bid changing (failover) more
   often than staying equal (same-bearer wait).
3. No increase in relayed-429 rate; no queue-wait growth (queue stays a safety
   net).

Cost note: subscription lanes are quota-metered (marginal USD ≈ 0); PAYG cost
accounting per lane/seat is slice S2 of the design.
