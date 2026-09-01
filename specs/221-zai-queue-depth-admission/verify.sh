#!/usr/bin/env bash
set -Eeuo pipefail
root=$(git rev-parse --show-toplevel)
cd "$root"

# Targeted: the seam this slice changes, plus everything that shares it.
uv run pytest tests/test_queue_depth_admission.py tests/test_queue_wait.py \
  tests/test_limiter.py tests/test_priority_lane.py tests/test_admission.py

# Full suite, parallel when pytest-xdist is available in the dev group.
if uv run python -c 'import xdist' 2>/dev/null; then
  uv run pytest -n auto
else
  uv run pytest
fi

uv run ruff check src tests
uv run ruff format --check src tests
git diff --check

# Published contract: the admission saturation block must carry a POSITIVE
# integer depth bound both for an idle measured lane and for a lane with no
# bearer observed yet (cold start), and must name the estimator's provenance.
uv run python - <<'PY'
from anthropic_throttle_proxy import config, proxy
from anthropic_throttle_proxy.limiter import FairBearerLimiter

lim = FairBearerLimiter(2, "fair")
lim.max_concurrent = 2
idle = proxy._lane_saturation({"b": {"limiter": lim.snapshot()}}, {"b": True})
cold = proxy._lane_saturation({}, {})
for name, block in (("idle", idle), ("cold", cold)):
    depth = block["queue_admit_max_depth"]
    inputs = block["queue_admit"]
    assert isinstance(depth, int) and not isinstance(depth, bool), (name, depth)
    assert depth > 0, (name, depth)
    assert inputs["service_time_s"] > 0, (name, inputs)
    assert inputs["source"] in {"measured", "inflight", "cold", "config"}, (name, inputs)
    assert inputs["max_wait_s"] == float(config.QUEUE_MAX_WAIT_S), (name, inputs)
    print(f"{name}: queue_admit_max_depth={depth} {inputs}")
PY

# Bounded live-compatible smoke: exercise the shipped code in-process only.
# Never touches the running :8766 service (no restart, no /run override, no
# deployment) — this slice ships a producer, it does not activate one.
uv run python - <<'PY'
import asyncio

from anthropic_throttle_proxy import config, proxy
from anthropic_throttle_proxy.limiter import FairBearerLimiter, QueueWaitTimeout


async def main() -> None:
    # 1/100 scale of the measured shape: cold default 0.1 s stands in for 10 s.
    config.QUEUE_DRAIN_DEFAULT_S = 0.1
    lim = FairBearerLimiter(2, "fair")
    lim.max_concurrent = 2
    for _ in range(2):
        await lim.acquire("holder")
    parked = [asyncio.create_task(lim.acquire(f"q{i}")) for i in range(3)]
    # Hold long enough that the occupied slots are themselves evidence the lane
    # is slow; the estimator refuses to reject on a guess.
    await asyncio.sleep(0.2)
    before = lim.snapshot()["queued_total"]
    try:
        async with lim.slot("arriving", max_wait=0.30):
            raise SystemExit("smoke: impossible request was admitted")
    except QueueWaitTimeout as exc:
        assert exc.pre_queue, "smoke: rejection must happen before enqueue"
        resp = proxy._queue_wait_timeout_response(
            "bid00000", "arriving", "v1/messages", lim, 0.30, timeout=exc
        )
        assert resp.status == 503
        assert resp.headers[config.QUEUE_TIMEOUT_HEADER] == "1"
        retry = int(resp.headers["retry-after"])
        assert retry >= config.QUEUE_TIMEOUT_RETRY_AFTER_S, retry
        assert retry <= config.QUEUE_RETRY_AFTER_MAX_S, retry
        print(f"smoke: pre-queue reject, retry-after={retry}s, queued_total unchanged")
    assert lim.snapshot()["queued_total"] == before
    for p in parked:
        p.cancel()


asyncio.run(main())
PY

printf 'queue-depth admission: PASS\n'
