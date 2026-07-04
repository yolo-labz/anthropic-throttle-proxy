# Simulation Testing

This repo now has two layers for throttle confidence:

- Fast deterministic checks in `pytest` for routing, limiter, retry, unified-window, and UI contracts.
- Local load rehearsal with a fake Anthropic upstream plus k6 for real client shapes: long coding turns, short evaluator hooks, streaming, and artificial per-bearer caps.

The current SOTA pattern for this service class is model/stateful tests for invariants, contract fuzzing at the HTTP edge, and fault-injection/load rehearsal before a live deployment. The practical stack is:

- Hypothesis stateful tests for limiter/account invariants.
- Schemathesis against an OpenAPI contract if the public proxy API grows.
- Toxiproxy between this proxy and a fake upstream for latency, timeout, and disconnect faults.
- k6 or Locust for repeatable user-flow load.

## Local Rehearsal

Create two fake Claude credential files:

```sh
printf '%s\n' '{"claudeAiOauth":{"accessToken":"sk-ant-oat01-SIM-A","expiresAt":4102444800000}}' > /tmp/claude-a.json
printf '%s\n' '{"claudeAiOauth":{"accessToken":"sk-ant-oat01-SIM-B","expiresAt":4102444800000}}' > /tmp/claude-b.json
```

Start the fake upstream:

```sh
FAKE_ANTHROPIC_MAX_PER_BEARER=1 \
FAKE_ANTHROPIC_DELAY_MS=800 \
uv run python tests/sim/fake_anthropic.py
```

Start the proxy against it in another shell:

```sh
THROTTLE_UPSTREAM=http://127.0.0.1:9000 \
THROTTLE_QUEUE_MODE=fair \
CLAUDE_API_THROTTLE_MAX=4 \
THROTTLE_AIMD_INITIAL_CONCURRENT=1 \
THROTTLE_PRIORITY_RESERVE_SLOTS=0 \
THROTTLE_ACCOUNT_CRED_PATHS="A:/tmp/claude-a.json,B:/tmp/claude-b.json" \
THROTTLE_ACCOUNT_ROUTING=least_loaded \
uv run anthropic-throttle-proxy
```

Run the workload:

```sh
k6 run load/k6-real-world.js
```

Expected behavior:

- `/__throttle/health` shows both bearer hashes after traffic starts.
- Fake upstream 429s cause AIMD shrink and short holds, not duplicate auth prompts.
- With `THROTTLE_ACCOUNT_ROUTING=least_loaded`, new `/v1/messages` requests move away from the bearer that is queued, in-flight, or under `Retry-After`.

## Live-Safety Rule

Use the simulator before changing throttle behavior. For live smoke, keep the k6 rate tiny and point it at a non-production model/account only if you intentionally want to spend real quota.
