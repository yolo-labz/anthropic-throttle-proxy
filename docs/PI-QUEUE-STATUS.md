# Held-stream queue status (version 1)

An opted-in streaming request stays on its original HTTP200 SSE response while
waiting for dispatch. The gateway retains its ordinary `: keepalive` comments
and additionally emits an Anthropic-compatible `event: ping`:

```text
event: ping
data: {"type":"ping","throttler_queue":{"version":1,"source":"throttler-gateway","state":"waiting","reason":"weekly_pacing","elapsed_ms":10000,"next_check_ms":15000}}
```

`reason` is `queue`, `weekly_pacing` (including a project-share deferral), or
`retry_after`. `elapsed_ms` is monotonic time since this response's hold began.
`next_check_ms` is an advisory interval until another wait check/status update,
**not a dispatch ETA**. Both numbers are nonnegative. The namespaced object
contains no prompt, credential, account identifier, usage or model content.

Clients that do not consume this metadata can ignore `ping` normally. A Pi
consumer should validate the configured gateway origin, existing
`x-anthropic-throttle-proxy: 1` response marker, version and field types before
showing status. This is advisory display data, not an authorization or proof
that capacity exists. Once actual generation starts, ordinary provider events
remain authoritative.

A ping creates no assistant message, tool call, token usage, successful stop or
retry. Keep the same model request pending; cancellation closes that request.
Never replay tools or restart a business turn because a status update arrives.
An explicit client patience/inherited tier budget still expires fail-closed;
a genuine upstream error still propagates. No ping renews a deadline or relaxes
weekly pacing, quota, credential eligibility or account concurrency.

The implicit local wait ceiling no longer ends a hold-eligible streaming
request without an explicit bound. Disconnection cancels the owning handler,
including a fair-queue acquire whose occupied slot has not yet completed.
Non-streaming and non-opted-in requests retain their existing bounded behavior.

Runnable checks:

```sh
uv run --frozen pytest -q tests/test_proxy_app.py -k 'held_ or pacing_ or streamed_wait'
uv run --frozen pytest -q tests/test_keepalive_hold.py tests/test_admission_quiesce.py
```

This protocol is not a durable job API: a process restart does not preserve an
HTTP stream, and a new submission is not known to be safe merely because its
predecessor disconnected. Crash-safe resumption requires a separate operation
identity and unknown-outcome contract; automatic business/tool replay is not
provided here.
