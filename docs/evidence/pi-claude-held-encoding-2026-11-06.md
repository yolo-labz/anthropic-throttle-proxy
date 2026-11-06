# Pi Claude held-stream encoding regression

Verified on desktop, 06/11/2026.

## Confirmed hypothesis

The prepared keepalive/queue-deferral response advertises plaintext SSE, but
`_forward_once_into_sse` forwarded gzip/deflate bytes without decoding them.
It cannot adopt the upstream encoding after HTTP headers or pings have been sent.
Pi therefore received no parsable terminal event and reported
`Anthropic stream ended without a stop reason`.

Synthetic loopback probes against the installed gateway confirmed:

| Requested encoding | HTTP | Downstream Content-Encoding | Body |
|---|---|---|---|
| gzip | 200 | absent | gzip magic `1f8b`; decompression reveals message_stop |
| identity | 200 | absent | plaintext SSE including message_stop |

No user conversation or credentials were included in the probes.

## Fix and scope

Enable aiohttp response decompression in the shared prepared-SSE forwarding
helper. Normal non-held byte-pass-through transport is unchanged. This also
makes compressed error envelopes readable to existing classification logic.
The keepalive emitter is still cancelled before upstream body bytes are written.

The touched-file gate additionally required making two existing recovery paths
explicit: telemetry transport/read failure resets status to zero for its existing
backoff path; terminal error writing returns when the client is already gone.
No exception text or credentials are logged.

## Executable evidence

- New gzip/deflate held-stream tests and compressed-503 capture test fail before
  the fix: **3 failed, 2 passed**; identity/plain-503 controls pass.
- Focused keepalive suite: **46 passed**.
- Final full suite in a dedicated locked offline uv environment: **2644 passed**.
  Pytest still reports aiohttp key/deprecation warnings; zero warnings is not claimed.
- `ruff check src tests` and changed-file format check pass.
- `code-slop-gate --worktree` and `alignment-gate --worktree` pass.

The first full-suite attempt used another checkout's editable environment. Its
nested subprocess imported that checkout and failed on a missing newer limiter
function. A dedicated `uv sync --locked --offline` environment fixes the import
provenance; no test was skipped or assertion weakened.

Generator: OpenAI. A bounded public GLM advisory review could not start because
`pi-zai-token` failed credential resolution. No approval is claimed.

## Reproduction

```sh
uv sync --locked
uv run pytest tests/test_keepalive_hold.py -q
uv run pytest -q
uv run ruff check src tests
```

Source rollback is one revert of the squash commit. Merging/tests alone do not
prove the live gateway has been activated; verify a real Pi request afterward.
