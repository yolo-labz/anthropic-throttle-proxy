"""Keep the independent public journal oracle in normal current-head CI."""

import pytest
from check_combined_accounting import NOW, cache_checks, checks
from check_gauge_wire import render_app
from ui_render import workload_snapshot

from anthropic_throttle_proxy import output_usage


def test_independent_completion_window():
    assert len(checks()["passed"]) >= 12


async def test_independent_cache_projection_and_replacement():
    assert len(await cache_checks()) == 3


def test_shared_render_guard_preserves_accounting_fixture(monkeypatch):
    injected = object()
    monkeypatch.setattr(output_usage, "_cache", injected)

    def forbidden(*args, **kwargs):
        raise AssertionError("render collection")

    with render_app(NOW, workload_snapshot(), forbidden) as app:
        assert output_usage._cache is injected
        assert not app.on_startup and not app.on_cleanup
        with pytest.raises(AssertionError, match="render collection"):
            output_usage.refresh()
