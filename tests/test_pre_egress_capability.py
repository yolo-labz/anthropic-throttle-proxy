"""Read-only control-plane capability; fixture metadata is never runtime proof."""

import hashlib
import inspect
import json
import os
import re
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import body_shrink, capability, config, proxy

NONCE = "a" * 64


@pytest.fixture
def supported(monkeypatch):
    # Unit-fixture attestation inputs only. Real immutable imports are qualified
    # separately in an owned subprocess, never inferred from these mock values.
    monkeypatch.setattr(capability, "_PID", os.getpid())
    monkeypatch.setattr(capability, "_INVOCATION_ID", "1" * 32)
    monkeypatch.setenv("INVOCATION_ID", "1" * 32)
    monkeypatch.setattr(capability, "_BUILD", "/nix/store/" + "0" * 32 + "-fixture/package")
    monkeypatch.setattr(
        capability, "_LOADED", {name: "2" * 64 for name in capability._MODULE_NAMES}
    )
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(proxy, "_admission_closed", False)


@pytest.fixture
async def client(supported):
    app = web.Application()
    capability.attach_capability(app)
    app.router.add_post("/__throttle/quiesce", proxy.quiesce)
    app.router.add_post("/v1/messages", proxy.handler)

    async def forbidden(_request):
        raise AssertionError("capability escaped the literal control-plane route")

    app.router.add_route("*", "/{path:.*}", forbidden)
    async with TestClient(TestServer(app)) as instance:
        yield instance


async def get_cap(client, nonce=NONCE):
    response = await client.get(capability.PATH, params={"nonce": nonce})
    assert response.headers["Cache-Control"] == "no-store"
    return response, await response.json()


async def test_schema_fresh_nonce_and_no_secrets(client, monkeypatch):
    monkeypatch.setattr(config, "COPILOT_TOKEN", "synthetic-secret-must-not-be-returned")
    first, data = await get_cap(client)
    assert first.status == 200
    assert set(data) == {
        "schemaVersion",
        "kind",
        "nonce",
        "pid",
        "invocationId",
        "build",
        "modules",
        "configSha256",
        "centralUrl",
    }
    assert data["schemaVersion"] == 1 and data["kind"] == "gateway-pre-egress-capability"
    assert data["nonce"] == NONCE and data["pid"] == os.getpid()
    assert data["invocationId"] == "1" * 32 and data["build"] == capability._BUILD
    assert set(data["modules"]) == {"proxy.py", "forwarding.py", "capability.py"}
    assert all(re.fullmatch("[0-9a-f]{64}", value) for value in data["modules"].values())
    assert re.fullmatch("[0-9a-f]{64}", data["configSha256"]) and data["centralUrl"] == ""
    assert "synthetic-secret" not in json.dumps(data)
    second, again = await get_cap(client, "b" * 64)
    assert second.status == 200 and again["nonce"] == "b" * 64
    assert again["configSha256"] == data["configSha256"]


@pytest.mark.parametrize(
    "query",
    [
        [],
        [("nonce", "")],
        [("nonce", "a" * 63)],
        [("nonce", "a" * 65)],
        [("nonce", "A" * 64)],
        [("nonce", "g" * 64)],
        [("nonce", "a" * 63 + "\n")],
        [("nonce", NONCE), ("nonce", NONCE)],
        [("nonce", NONCE), ("extra", "x")],
    ],
)
async def test_bad_challenge_fails_closed(client, query):
    async with client.get(capability.PATH, params=query) as response:
        assert response.status == 400
        assert await response.json() == {"error": "invalid_nonce"}
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("method", ["POST", "HEAD", "PUT", "OPTIONS"])
async def test_other_methods_never_forward(client, method):
    async with client.request(method, capability.PATH, params={"nonce": NONCE}) as response:
        assert response.status == 405 and response.headers["Allow"] == "GET"
        assert response.headers["Cache-Control"] == "no-store"


async def test_real_closed_admission_needs_no_provider_allow_or_files(client, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("capability must not forward/probe/read credentials or state")

    monkeypatch.setattr(proxy, "admission", forbidden)
    monkeypatch.setattr(proxy, "pick_target", forbidden)
    monkeypatch.setattr(proxy, "_forward_once", forbidden)
    monkeypatch.setattr(config, "save_overrides", forbidden)
    before = dict(config.state)
    async with client.post("/__throttle/quiesce") as closed:
        assert (await closed.json())["admission"] == "closed"
    async with client.post("/v1/messages", data=b"not parsed while closed") as refused:
        assert refused.status == 503 and refused.headers["x-throttle-admission-closed"] == "1"
    with monkeypatch.context() as scope:
        scope.setattr(Path, "open", forbidden)
        scope.setattr(Path, "read_text", forbidden)
        scope.setattr(Path, "read_bytes", forbidden)
        response, data = await get_cap(client)
    assert response.status == 200 and data["nonce"] == NONCE
    assert config.state == before


@pytest.mark.parametrize(
    "change",
    [
        "missing_invocation",
        "upper_invocation",
        "changed_invocation",
        "fork",
        "missing_module",
        "mutable_module",
    ],
)
async def test_generation_and_loaded_metadata_fail_closed(client, monkeypatch, change):
    if change == "missing_invocation":
        monkeypatch.setattr(capability, "_INVOCATION_ID", "")
    elif change == "upper_invocation":
        monkeypatch.setattr(capability, "_INVOCATION_ID", "A" * 32)
    elif change == "changed_invocation":
        monkeypatch.setenv("INVOCATION_ID", "3" * 32)
    elif change == "fork":
        monkeypatch.setattr(capability, "_PID", os.getpid() + 1)
    else:
        loaded = dict(capability._LOADED)
        if change == "missing_module":
            loaded.pop("proxy.py")
        else:
            loaded["proxy.py"] = None
        monkeypatch.setattr(capability, "_LOADED", loaded)
    response, data = await get_cap(client)
    assert response.status == 503 and data == {"error": "unsupported_generation"}


def _changed(value):
    if type(value) is bool:
        return not value
    if type(value) in (int, float):
        return value + 1
    if isinstance(value, str):
        return value + "-capability-drift"
    if isinstance(value, Path):
        return value.with_name(value.name + "-drift")
    if isinstance(value, dict):
        return {**value, "capability-drift": 0.5}
    if isinstance(value, set):
        return value | {"capability-drift"}
    raise AssertionError(f"uncovered effective configuration type {type(value)}")


def test_every_effective_config_field_is_bound(monkeypatch):
    baseline = capability.config_sha256(config, proxy)
    names = [
        key
        for key in vars(config)
        if key.isupper() and not key.startswith("_") and key not in capability._CONFIG_METADATA
    ]
    assert len(names) > 80
    for key in names:
        with monkeypatch.context() as scope:
            scope.setattr(config, key, _changed(getattr(config, key)))
            assert capability.config_sha256(config, proxy) != baseline, key


async def test_actual_hot_override_and_non_config_knob_drift(client, monkeypatch):
    _, initial = await get_cap(client)
    monkeypatch.setattr(config, "save_overrides", lambda: None)
    monkeypatch.setattr(config, "RUNTIME_OVERRIDES", {})
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", proxy.UTILIZATION_TARGET)
    config.set_override("utilization_target", 0.42)
    _, changed = await get_cap(client)
    assert changed["configSha256"] != initial["configSha256"]
    monkeypatch.setattr(body_shrink, "CAP_BYTES", body_shrink.CAP_BYTES + 1)
    _, body_changed = await get_cap(client)
    assert body_changed["configSha256"] != changed["configSha256"]


@pytest.mark.parametrize(
    "key",
    [
        "THROTTLE_CENTRAL_URL",
        "INGRESS_ANTHROPIC_LANE_URL",
        "CLAUDE_API_THROTTLE_MAX",
        "SSL_CERT_FILE",
        "HTTPS_PROXY",
        "NO_PROXY",
    ],
)
async def test_routing_and_transport_environment_drift(client, monkeypatch, key):
    _, initial = await get_cap(client)
    monkeypatch.setenv(key, "synthetic-config-drift")
    _, changed = await get_cap(client)
    assert changed["configSha256"] != initial["configSha256"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), "x" * 65537])
async def test_unrepresentable_config_is_unsupported(client, monkeypatch, value):
    monkeypatch.setattr(config, "CAPABILITY_NEW_CONFIG", value, raising=False)
    response, data = await get_cap(client)
    assert response.status == 503 and data == {"error": "unsupported_configuration"}


async def test_central_report_is_exact_but_never_followed(client, monkeypatch):
    monkeypatch.setattr(config, "CENTRAL_URL", "https://synthetic.invalid/central")
    response, data = await get_cap(client)
    assert response.status == 200 and data["centralUrl"] == config.CENTRAL_URL
    monkeypatch.setattr(
        config, "CENTRAL_URL", "https://user:secret@synthetic.invalid/?token=secret"
    )
    response, data = await get_cap(client)
    assert response.status == 503 and "secret" not in json.dumps(data)


def test_mutable_source_cannot_register_as_immutable(tmp_path, monkeypatch):
    path = tmp_path / "proxy.py"
    path.write_text("mutable")
    monkeypatch.setattr(capability, "_BUILD", str(tmp_path))
    monkeypatch.setattr(capability, "_LOADED", {})
    capability.record_loaded_module(str(path))
    assert capability._LOADED == {"proxy.py": None}


def test_import_snapshot_not_current_disk_and_mixed_reload_is_unsupported(tmp_path, monkeypatch):
    # Structural unit test only: override the store-path predicate to exercise
    # capture time on a writable fixture. Production keeps the strict predicate.
    path = tmp_path / "proxy.py"
    path.write_bytes(b"old imported code")
    monkeypatch.setattr(capability, "_BUILD", str(tmp_path))
    monkeypatch.setattr(capability, "_STORE_PATH", re.compile(".+"))
    monkeypatch.setattr(capability, "_LOADED", {})
    capability.record_loaded_module(str(path))
    digest = hashlib.sha256(b"old imported code").hexdigest()
    assert capability._LOADED["proxy.py"] == digest
    path.write_bytes(b"changed on disk, not imported")
    assert capability._LOADED["proxy.py"] == digest
    capability.record_loaded_module(str(path))
    assert capability._LOADED["proxy.py"] is None


def test_actual_main_registers_literal_capability_before_relay():
    source = inspect.getsource(proxy.main)
    assert source.index("attach_capability(app)") < source.index(
        'app.router.add_route("*", "/{path:.*}", handler)'
    )
