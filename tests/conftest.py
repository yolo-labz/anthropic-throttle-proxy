"""Suite-wide isolation for process-global proxy registries."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from aiohttp import web

from anthropic_throttle_proxy import config, fleet_ui_config, forwarding, limiter, pacing, proxy


def _shared_memory_dir() -> Path | None:
    """The host's writable tmpfs ``.../shm`` mount, read from /proc/mounts."""
    try:
        with open("/proc/mounts", encoding="ascii", errors="replace") as mounts:
            rows = [line.split() for line in mounts]
    except OSError:
        return None
    for row in rows:
        if len(row) > 2 and row[2] == "tmpfs" and row[1].endswith("/shm"):
            path = Path(row[1])
            if path.is_dir() and os.access(path, os.W_OK):
                return path
    return None


@pytest.fixture
def tmpfs_state_path(tmp_path_factory):
    """Per-test directory on tmpfs when the host has one, else a normal tmp dir.

    For tests that time loop/worker hand-offs around REAL ledger saves. The file,
    replace and directory-fsync code paths still run; only the host disk's
    journal latency is gone. On ext4 one small-file fsync waits for every other
    writer's dirty data: the shared CI runner stalled one past the 30 s settle
    wait (main run 102, PR #9 run 106) while nothing in the owner was wrong.
    """
    shm = _shared_memory_dir()
    if shm is None:
        yield tmp_path_factory.mktemp("state")
        return
    with tempfile.TemporaryDirectory(dir=shm, prefix="pytest-state-") as path:
        yield Path(path)


@pytest.fixture
def proxy_admission_state(monkeypatch):
    """Shared isolation for HTTP admission tests; never use another test's pools."""
    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    monkeypatch.setitem(config.state, "inflight", 0)
    monkeypatch.setitem(config.state, "queued", 0)
    yield
    config.bearer_limiters.clear()
    config.bearer_state.clear()


@pytest.fixture
def proxy_admission_app(proxy_admission_state):
    """Real admission handler, without background probes or dashboard tasks."""
    app = web.Application()
    app.on_response_prepare.append(forwarding.stamp_proxy_marker)
    app.router.add_route("*", "/{path:.*}", proxy.handler)
    return app


@pytest.fixture(autouse=True)
def _isolate_fleet_ui_config(monkeypatch, tmp_path):
    """Never render the operator's YAML in a test."""
    path = tmp_path / "test-fleet-ui.yaml"
    path.write_text("subscriptions: []\n", encoding="utf-8")
    monkeypatch.setenv("FLEET_UI_CONFIG", str(path))
    monkeypatch.setenv("THROTTLE_LANE_REGISTRY_FILE", str(tmp_path / "absent-native-registry.json"))
    fleet_ui_config.reset_cache()
    yield
    fleet_ui_config.reset_cache()


@pytest.fixture(autouse=True)
def _isolate_retry_probe_gates():
    limiter._reset_retry_probe_gates()
    yield
    limiter._reset_retry_probe_gates()


@pytest.fixture(autouse=True)
def _isolate_retry_after_state_file(monkeypatch, tmp_path):
    """Never let the suite read or write the operator's live state file.

    ``RETRY_AFTER_STATE_FILE`` defaults to the XDG state dir since the 19/10
    state-persistence hardening (config._resolve_retry_after_state_file), so
    without this fixture every test that notes a window would mutate the
    developer's real ``~/.local/state/.../retry-after-state.json`` and would
    load real bearer windows into the module-global cache. The cache is
    forced to ``None`` (not monkeypatched) so teardown also drops any dict a
    test populated.
    """
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", str(tmp_path / "retry-after-state.json"))
    limiter._retry_after_state = None
    yield
    # Drain the background writer so no write lands in the NEXT test's file.
    limiter.flush_retry_after_writes()
    limiter._retry_after_state = None


@pytest.fixture(autouse=True)
def _isolate_lane_report(monkeypatch, tmp_path_factory):
    """Point the lane reader at a path that does not exist.

    Otherwise every UI test reads the developer's OWN
    $XDG_RUNTIME_DIR/throttle-lanes.json and asserts against whatever their
    Codex meters happen to say today.
    """
    absent = tmp_path_factory.mktemp("lanes") / "absent.json"
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(absent))
    monkeypatch.setenv("THROTTLE_PI_USAGE_PATH", str(absent.with_name("absent-usage.jsonl")))
    monkeypatch.delenv("THROTTLE_MIMO_DESKTOP_REPORT", raising=False)
    from anthropic_throttle_proxy import lanes, output_usage

    monkeypatch.setattr(output_usage, "_cache", None)
    lanes._cache = None
    yield
    lanes._cache = None


@pytest.fixture
def meter_rows():
    """Synthetic independently scoped quota rows for endpoint/dispatch acceptance."""

    def rows(owner="exhausted", secondary="ok"):
        return [
            {
                "id": lane,
                "kind": "mimo",
                "status": status,
                "meters": [{"used_pct": 100.0 if status == "exhausted" else 25.0}],
            }
            for lane, status in (("mimo:plan", owner), ("mimo:team-b", secondary))
        ]

    return rows


@pytest.fixture
def funded_api_metadata(monkeypatch, tmp_path):
    """Explicit fresh receipt for owned automatic API routing in local tests."""
    import json
    import time
    from datetime import UTC, datetime

    from anthropic_throttle_proxy import config

    def seed(model):
        path = tmp_path / "api-funding.json"
        now = time.time()
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "accounts": [
                        {
                            "id": "fixture",
                            "provider": "anthropic",
                            "accountId": "owned",
                            "workspaceId": "fleet",
                            "currency": "USD",
                            "source": "operator-receipt",
                            "observedAt": datetime.fromtimestamp(now - 1, UTC).isoformat(),
                            "validUntil": datetime.fromtimestamp(now + 60, UTC).isoformat(),
                            "credit": {"status": "available", "remaining": 1},
                            "authorized": True,
                            "spendLimit": {"remaining": 1, "enforced": True},
                            "rateLimits": [
                                {
                                    "model": model,
                                    "scope": "fleet",
                                    "rpmRemaining": 5,
                                    "tpmRemaining": 5000,
                                }
                            ],
                        }
                    ],
                }
            )
        )
        for name, value in {
            "API_KEY_ACCOUNT_ID": "owned",
            "API_KEY_WORKSPACE_ID": "fleet",
            "API_KEY_SCOPE": "fleet",
            "API_CREDITS_FILE": str(path),
        }.items():
            monkeypatch.setattr(config, name, value)

    return seed
