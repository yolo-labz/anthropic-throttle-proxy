# 378 — plan (phase 1)

1. `src/anthropic_throttle_proxy/consumers.py` (new): pure `snapshot(share, now)` built on `ProjectShare.view()` (one locked copy, no new counters); `attach_consumers(app)` registers two GET routes only when `THROTTLE_CONSUMERS_VIEW=1`.
2. `proxy.py`: one call `attach_consumers(app)` next to the existing `/__throttle/admission` route (before the catch-all).
3. `tests/test_consumers_view.py`: latency (500 consumers), `_direct` fallback, GET-only routes, default-off.
4. Gates: `ruff check`, `ruff format --check` on touched files; targeted pytest only (no full local suite, quota is scarce).
