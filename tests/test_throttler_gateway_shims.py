"""Entry shims in ``throttler_gateway`` delegate to the runtime without starting it."""

import importlib
import runpy
import sys
from pathlib import Path

import pytest

from anthropic_throttle_proxy import ingress as runtime_ingress
from anthropic_throttle_proxy import proxy as runtime_proxy


def test_gateway_shim_reuses_proxy_main() -> None:
    gateway = importlib.import_module("throttler_gateway.gateway")
    assert gateway.main is runtime_proxy.main


def test_ingress_shim_reuses_ingress_main() -> None:
    ingress = importlib.import_module("throttler_gateway.ingress")
    assert ingress.main is runtime_ingress.main


def test_package_main_runs_gateway_main(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runtime_proxy, "main", lambda: calls.append("gateway"))
    sys.modules.pop("throttler_gateway.gateway", None)
    runpy.run_module("throttler_gateway", run_name="__main__", alter_sys=False)
    assert calls == ["gateway"]


def test_ingress_shim_script_runs_ingress_main(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runtime_ingress, "main", lambda: calls.append("ingress"))
    sys.modules.pop("throttler_gateway.ingress", None)
    runpy.run_module("throttler_gateway.ingress", run_name="__main__", alter_sys=False)
    assert calls == ["ingress"]


def test_burst_probe_percentile_helper() -> None:
    load_dir = str(Path(__file__).parents[1] / "load")
    sys.path.insert(0, load_dir)
    try:
        probe = importlib.import_module("burst_probe")
    finally:
        sys.path.remove(load_dir)
    assert probe._pct([], 50) is None
    assert probe._pct([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert probe._pct([1.0, 2.0, 3.0, 4.0], 100) == 4.0
    assert probe._pct([1.0, 2.0, 3.0, 4.0], 0) == 1.0
