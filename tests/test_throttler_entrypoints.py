"""Installed entrypoints share the runtime without starting listeners or collectors."""

import importlib
import importlib.metadata
import runpy
import sys
import sysconfig
from pathlib import Path

import jinja2
import pytest
from ui_render import _EMPTY_CONTEXT

from anthropic_throttle_proxy import __build__, __version__
from anthropic_throttle_proxy.ui import routes

COMMANDS = {
    "throttler-gateway": ("throttler_gateway.gateway", "anthropic_throttle_proxy.proxy"),
    "throttler-ingress": ("throttler_gateway.ingress", "anthropic_throttle_proxy.ingress"),
    "anthropic-throttle-proxy": (
        "anthropic_throttle_proxy.proxy",
        "anthropic_throttle_proxy.proxy",
    ),
    "anthropic-throttle-ingress": (
        "anthropic_throttle_proxy.ingress",
        "anthropic_throttle_proxy.ingress",
    ),
}


def test_distribution_exports_shared_runtime_and_provenance():
    distribution = importlib.metadata.distribution("throttler-gateway")
    scripts = {entry.name: entry for entry in distribution.entry_points}
    assert distribution.version == __version__
    canonical = importlib.import_module("throttler_gateway")
    assert canonical.__version__ == __version__
    assert canonical.__build__ == __build__
    for name, (entry_module, runtime_module) in COMMANDS.items():
        assert scripts[name].value == f"{entry_module}:main"
        assert scripts[name].load() is importlib.import_module(runtime_module).main


@pytest.mark.parametrize("name", COMMANDS)
def test_installed_console_script_dispatch(name, monkeypatch):
    entry_module, _ = COMMANDS[name]
    calls = []
    monkeypatch.setattr(importlib.import_module(entry_module), "main", lambda: calls.append(name))
    script = Path(sysconfig.get_path("scripts")) / name
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(script), run_name="__main__")
    assert exited.value.code in (None, 0)
    assert calls == [name]


@pytest.mark.parametrize(
    ("module", "target"),
    [
        ("throttler_gateway", "throttler_gateway.gateway"),
        ("throttler_gateway.gateway", "anthropic_throttle_proxy.proxy"),
        ("throttler_gateway.ingress", "anthropic_throttle_proxy.ingress"),
        ("anthropic_throttle_proxy", "anthropic_throttle_proxy.proxy"),
    ],
)
def test_module_dispatch(module, target, monkeypatch):
    calls = []
    monkeypatch.setattr(importlib.import_module(target), "main", lambda: calls.append(module))
    monkeypatch.delitem(sys.modules, module, raising=False)
    runpy.run_module(module, run_name="__main__")
    assert calls == [module]


@pytest.mark.parametrize("show_local", [True, False])
def test_dashboard_product_identity(show_local):
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(routes._TEMPLATES), autoescape=True
    )
    html = environment.get_template("dashboard.html").render(
        **(
            _EMPTY_CONTEXT
            | {"show_local": show_local, "central_url": "(direct)", "asset_v": "test"}
        )
    )
    assert "<title>Throttler" in html
    assert "<h1>Throttler" in html
    favicon = (routes._STATIC / "favicon.svg").read_text()
    assert "<title>Throttler</title>" in favicon
