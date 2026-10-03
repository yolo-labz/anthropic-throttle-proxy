"""Startup authority is explicit, restart-stable and exclusive to one writer."""

import asyncio
import json
import sys

import pytest

from anthropic_throttle_proxy.prospective_config import acquire_custody, runtime_settings


@pytest.fixture
def manifest(tmp_path):
    return {
        "sources": ["operator-slot"],
        "endpoints": ["https://provider.invalid/v1"],
        "models": ["alias", "canonical"],
        "entries": [
            {
                "source": "operator-slot",
                "endpoint": "https://provider.invalid/v1",
                "alias": "alias",
                "upstream": "provider",
                "account": "account",
                "model": "canonical",
                "budgets": {"max_requests": 2, "max_tokens": 1000},
                "output_default": 20,
            }
        ],
        "state_directory": str(tmp_path / "state"),
        "allow_cold_start": False,
        "max_pending": 2,
        "wait_timeout": 1,
        "budget_label": "public-budget",
        "retry_after_s": 5,
    }


def settings(tmp_path, manifest, mode="strict"):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(manifest))
    return runtime_settings(str(path), mode)


def test_aliases_share_custody_but_observe_cannot_supply_strict_debt(tmp_path, manifest):
    original, directory = settings(tmp_path, manifest)
    manifest["entries"].append({**manifest["entries"][0], "alias": "canonical"})
    strict, same_directory = settings(tmp_path, manifest)
    observe, observe_directory = settings(tmp_path, manifest, "observe")
    assert strict["scopes"] == original["scopes"]
    assert directory == same_directory
    assert strict["scopes"][0].state_path != observe["scopes"][0].state_path
    assert observe_directory != directory
    assert not directory.exists()  # parsing never initializes or clears a ledger
    assert not strict["scopes"][0].allow_cold_start
    assert (
        strict["resolver"]
        .resolve("operator-slot", "https://provider.invalid/v1", "alias")
        .output_default
        == 20
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("allow_cold_start", 1),
        ("sources", "operator-slot"),
        ("sources", []),
        ("models", [" "]),
        ("endpoints", ["duplicate", "duplicate"]),
        ("entries", []),
        ("entries", [{}]),
        ("state_directory", "relative"),
    ],
)
def test_malformed_authority_refuses_before_state_creation(tmp_path, manifest, field, value):
    manifest[field] = value
    with pytest.raises(ValueError):
        settings(tmp_path, manifest)
    assert not (tmp_path / "state").exists()


def test_unknown_and_duplicate_keys_fail_closed(tmp_path, manifest):
    manifest["entries"][0]["output_defaut"] = 20
    with pytest.raises(ValueError, match="unknown fields"):
        settings(tmp_path, manifest)
    del manifest["entries"][0]["output_defaut"]
    manifest["typo"] = True
    with pytest.raises(ValueError, match="exactly"):
        settings(tmp_path, manifest)
    path = tmp_path / "duplicate.json"
    path.write_text('{"sources": [], "sources": ["other"]}')
    with pytest.raises(ValueError, match="duplicate"):
        runtime_settings(str(path), "strict")


def test_manifest_size_and_unknown_mode_are_bounded(tmp_path):
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="1 MiB"):
        runtime_settings(str(path), "observe")
    with pytest.raises(ValueError, match="mode"):
        runtime_settings("/nonexistent-config", "other")


def test_observe_directory_alias_cannot_reuse_strict_debt(tmp_path, manifest):
    state = tmp_path / "state"
    (state / "strict").mkdir(parents=True)
    (state / "observe").symlink_to(state / "strict", target_is_directory=True)
    with pytest.raises(ValueError, match="alias"):
        settings(tmp_path, manifest, "observe")


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_ledger_file_alias_cannot_escape_its_custody_lock(tmp_path, manifest, alias):
    configured, _ = settings(tmp_path, manifest)
    from pathlib import Path

    target = tmp_path / "other-owner.json"
    target.write_text("{}")
    state = Path(configured["scopes"][0].state_path)
    state.parent.mkdir(parents=True)
    if alias == "symlink":
        state.symlink_to(target)
    else:
        state.hardlink_to(target)
    with pytest.raises(ValueError, match="alias"):
        acquire_custody(state.parent, configured["scopes"])
    with pytest.raises(ValueError, match="alias"):
        settings(tmp_path, manifest)


async def _lock_probe(path):
    child = """import fcntl, pathlib, sys
with pathlib.Path(sys.argv[1]).open('a+b') as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(75)
"""
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", child, str(path))
    try:
        return await asyncio.wait_for(process.wait(), 3)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_live_writer_excludes_second_process_then_releases_without_cleanup(tmp_path):
    directory = tmp_path / "strict"
    with acquire_custody(directory):
        assert await _lock_probe(directory / ".owner.lock") == 75
        with pytest.raises(BlockingIOError):
            acquire_custody(directory)
    assert (directory / ".owner.lock").exists()
    assert await _lock_probe(directory / ".owner.lock") == 0


@pytest.mark.asyncio
async def test_off_does_not_open_manifest_or_construct_owner(monkeypatch):
    from aiohttp import web

    from anthropic_throttle_proxy import prospective_lifecycle as lifecycle
    from anthropic_throttle_proxy.prospective_runtime import RUNTIME_KEY

    def forbidden(*args, **kwargs):
        raise AssertionError("off touched enabled configuration")

    monkeypatch.setenv("THROTTLE_PROSPECTIVE_MODE", "off")
    monkeypatch.setenv("THROTTLE_PROSPECTIVE_CONFIG", "/invalid/unreadable")
    monkeypatch.setattr(lifecycle, "runtime_settings", forbidden)
    monkeypatch.setattr(lifecycle, "ProspectiveRuntime", forbidden)
    app = web.Application()
    context = lifecycle.prospective_context(app)
    await anext(context)
    assert RUNTIME_KEY not in app
    await context.aclose()


@pytest.mark.asyncio
async def test_strict_debt_survives_real_app_shutdown_and_restart(tmp_path, manifest, monkeypatch):
    from aiohttp import web

    from anthropic_throttle_proxy.prospective_lifecycle import prospective_context
    from anthropic_throttle_proxy.prospective_runtime import (
        RUNTIME_KEY,
        LocalProspectiveRefusal,
        SelectedDispatch,
    )

    manifest["allow_cold_start"] = True
    manifest["entries"][0]["budgets"]["max_requests"] = 1
    path = tmp_path / "config.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setenv("THROTTLE_PROSPECTIVE_MODE", "strict")
    monkeypatch.setenv("THROTTLE_PROSPECTIVE_CONFIG", str(path))
    selected = SelectedDispatch("operator-slot", "https://provider.invalid/v1", "direct", False)
    body = b'{"model":"alias","messages":[{"role":"user","content":"hello"}],"max_tokens":5}'
    app = web.Application()
    context = prospective_context(app)
    await anext(context)
    async with app[RUNTIME_KEY].reserve(selected, body) as permit:
        permit.handoff()
    await context.aclose()
    manifest["allow_cold_start"] = False
    path.write_text(json.dumps(manifest))
    restarted = web.Application()
    context = prospective_context(restarted)
    await anext(context)
    try:
        with pytest.raises(LocalProspectiveRefusal) as refusal:
            async with restarted[RUNTIME_KEY].reserve(selected, body):
                pytest.fail("restart incorrectly granted spent request allowance")
        assert refusal.value.refusal.reason.value == "exhausted"
    finally:
        await context.aclose()


@pytest.mark.asyncio
async def test_close_timeout_retains_custody_even_after_app_teardown(
    tmp_path, manifest, monkeypatch
):
    from aiohttp import web

    from anthropic_throttle_proxy import prospective_lifecycle as lifecycle
    from anthropic_throttle_proxy.prospective_runtime import ProspectiveRuntime

    class SlowClose(ProspectiveRuntime):
        async def start(self):
            return None

        async def aclose(self):
            raise TimeoutError("controlled undrained worker")

    path = tmp_path / "config.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setenv("THROTTLE_PROSPECTIVE_MODE", "strict")
    monkeypatch.setenv("THROTTLE_PROSPECTIVE_CONFIG", str(path))
    monkeypatch.setattr(lifecycle, "ProspectiveRuntime", SlowClose)
    retained = []
    monkeypatch.setattr(lifecycle, "_UNDRAINED_CUSTODY", retained)
    app = web.Application()
    context = lifecycle.prospective_context(app)
    await anext(context)
    with pytest.raises(TimeoutError):
        await context.aclose()
    assert len(retained) == 1 and not retained[0].closed
    del context, app
    try:
        with pytest.raises(BlockingIOError):
            acquire_custody(tmp_path / "state" / "strict")
    finally:
        retained[0].close()  # this fake worker has no pending operation


@pytest.mark.parametrize("close_error", [TimeoutError("undrained"), RuntimeError("unknown")])
async def test_start_failure_remains_primary_if_cleanup_also_fails(
    tmp_path, manifest, monkeypatch, close_error
):
    from aiohttp import web

    from anthropic_throttle_proxy import prospective_lifecycle as lifecycle
    from anthropic_throttle_proxy.prospective_runtime import ProspectiveRuntime

    class BrokenStart(ProspectiveRuntime):
        async def start(self):
            raise ValueError("boot failure")

        async def aclose(self):
            raise close_error

    path = tmp_path / "config.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setenv("THROTTLE_PROSPECTIVE_MODE", "strict")
    monkeypatch.setenv("THROTTLE_PROSPECTIVE_CONFIG", str(path))
    monkeypatch.setattr(lifecycle, "ProspectiveRuntime", BrokenStart)
    retained = []
    monkeypatch.setattr(lifecycle, "_UNDRAINED_CUSTODY", retained)
    context = lifecycle.prospective_context(web.Application())
    try:
        with pytest.raises(ValueError, match="boot failure") as caught:
            await anext(context)
        assert caught.value.__cause__ is close_error
        assert len(retained) == 1 and not retained[0].closed
    finally:
        for handle in retained:
            handle.close()  # fake startup never creates a worker


async def test_app_context_injects_one_owner_into_both_probe_loops(tmp_path, manifest, monkeypatch):
    import asyncio

    from aiohttp import web

    from anthropic_throttle_proxy import proxy
    from anthropic_throttle_proxy.prospective_lifecycle import prospective_context
    from anthropic_throttle_proxy.prospective_runtime import RUNTIME_KEY, LocalProspectiveRefusal

    manifest["allow_cold_start"] = True
    path = tmp_path / "config.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setenv("THROTTLE_PROSPECTIVE_MODE", "strict")
    monkeypatch.setenv("THROTTLE_PROSPECTIVE_CONFIG", str(path))
    monkeypatch.setattr(proxy, "_api_key_routing_enabled", lambda: True)
    monkeypatch.setattr(proxy.config, "AUTH_PROBE_INTERVAL_S", 30)
    monkeypatch.setattr(proxy.config, "CREDENTIAL_RECHECK_S", 30)
    monkeypatch.setattr(proxy.config, "CREDENTIAL_RECHECK_MODEL", "synthetic")
    seen = []
    ready = asyncio.Event()

    async def probe_loop(*, prospective):
        seen.append(prospective)
        if len(seen) == 2:
            ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(proxy, "_upstream_auth_loop", probe_loop)
    monkeypatch.setattr(proxy, "_credential_recheck_loop", probe_loop)
    app = web.Application()
    app.cleanup_ctx.extend(
        [prospective_context, proxy._upstream_auth_context, proxy._credential_recheck_context]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await asyncio.wait_for(ready.wait(), 1)
        for probe in seen:
            assert probe.runtime is app[RUNTIME_KEY]
            assert probe.selected.internal_probe is True
            with pytest.raises(LocalProspectiveRefusal):
                async with probe.reserve(b'{"model":"alias","max_tokens":1}'):
                    pytest.fail("strict internal probe incorrectly gained transport authority")
    finally:
        await runner.cleanup()
    assert app[RUNTIME_KEY]._closing


async def test_real_missing_restart_debt_stays_primary_and_drained_owner_releases_lock(
    tmp_path, manifest, monkeypatch
):
    from aiohttp import web

    from anthropic_throttle_proxy.prospective_admission import OwnerFaulted
    from anthropic_throttle_proxy.prospective_lifecycle import prospective_context

    path = tmp_path / "config.json"
    path.write_text(json.dumps(manifest))  # explicit cold-start permission is false
    monkeypatch.setenv("THROTTLE_PROSPECTIVE_MODE", "strict")
    monkeypatch.setenv("THROTTLE_PROSPECTIVE_CONFIG", str(path))
    with pytest.raises(FileNotFoundError) as caught:
        await anext(prospective_context(web.Application()))
    assert isinstance(caught.value.__cause__, OwnerFaulted)
    # Actual owner drained; exclusive custody can transfer without clearing debt.
    with acquire_custody(tmp_path / "state" / "strict"):
        assert list((tmp_path / "state" / "strict").glob("*.json")) == []
