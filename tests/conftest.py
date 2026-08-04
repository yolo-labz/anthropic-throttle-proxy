"""Suite-wide isolation for process-global proxy registries."""

from __future__ import annotations

import pytest

from anthropic_throttle_proxy import limiter


@pytest.fixture(autouse=True)
def _isolate_retry_probe_gates():
    limiter._reset_retry_probe_gates()
    yield
    limiter._reset_retry_probe_gates()


@pytest.fixture(autouse=True)
def _isolate_lane_report(monkeypatch, tmp_path_factory):
    """Point the lane reader at a path that does not exist.

    Otherwise every UI test reads the developer's OWN
    $XDG_RUNTIME_DIR/throttle-lanes.json and asserts against whatever their
    Codex meters happen to say today.
    """
    absent = tmp_path_factory.mktemp("lanes") / "absent.json"
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(absent))
    from anthropic_throttle_proxy import lanes

    lanes._cache = None
    yield
    lanes._cache = None
