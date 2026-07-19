"""Suite-wide isolation for process-global proxy registries."""

from __future__ import annotations

import pytest

from anthropic_throttle_proxy import limiter


@pytest.fixture(autouse=True)
def _isolate_retry_probe_gates():
    limiter._reset_retry_probe_gates()
    yield
    limiter._reset_retry_probe_gates()
