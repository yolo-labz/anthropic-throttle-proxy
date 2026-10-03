"""Offline dispatch-boundary tests: explicit fictional authority, no network."""

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from anthropic_throttle_proxy import ledger as ledger_module
from anthropic_throttle_proxy import prospective_runtime as runtime_module
from anthropic_throttle_proxy.ledger import Budgets
from anthropic_throttle_proxy.prospective import account_request
from anthropic_throttle_proxy.prospective_admission import OwnerFaulted, Scope
from anthropic_throttle_proxy.prospective_refusal import RefusalReason
from anthropic_throttle_proxy.prospective_runtime import (
    RUNTIME_KEY,
    LocalProspectiveRefusal,
    ProspectiveRuntime,
    SelectedDispatch,
    get_runtime,
    get_selected_dispatch,
    set_selected_dispatch,
)
from anthropic_throttle_proxy.prospective_scope import ScopeResolver

KEY = ("fixture-upstream", "fixture-account", "canonical")
ENDPOINT = "https://fixture.invalid/api"
SELECTED = SelectedDispatch("configured-source", ENDPOINT, "direct", False)
BODY = b'{ "model": "alias", "messages": [{"role":"user","content":"final text"}] }'


class Request(dict):
    def __init__(self):
        super().__init__()
        self.app = {}

    @property
    def headers(self):
        raise AssertionError("runtime accessors must not read headers")


def configured(tmp_path, *, mode="strict", budgets=None, output_default=7, **changes):
    budgets = budgets or Budgets(3, 500)
    rows = [
        {
            "source": SELECTED.credential_source,
            "endpoint": ENDPOINT,
            "alias": alias,
            "upstream": KEY[0],
            "account": KEY[1],
            "model": KEY[2],
            "budgets": budgets,
            "output_default": output_default,
        }
        for alias in ("alias", "canonical")
    ]
    config = {
        "mode": mode,
        "resolver": ScopeResolver(
            rows,
            sources=[SELECTED.credential_source],
            endpoints=[ENDPOINT],
            models=["alias", "canonical"],
        ),
        "scopes": [Scope(KEY, budgets, str(tmp_path / "ledger.json"), True)],
        "max_pending": 2,
        "wait_timeout": 2.0,
        "budget_label": "fixture-authority",
        "retry_after_s": 3,
    }
    return ProspectiveRuntime(**(config | changes))


@pytest.fixture
async def runtimes(tmp_path):
    created = []

    async def make(**kwargs):
        runtime = configured(tmp_path, **kwargs)
        created.append(runtime)
        await runtime.start()
        return runtime

    yield make
    for runtime in created:
        try:
            await runtime.aclose()
        except OwnerFaulted:
            pass  # fault tests assert it; every underlying worker still drains


async def dispatch(runtime, selected=SELECTED, body=BODY):
    async with runtime.reserve(selected, body) as permit:
        permit.handoff()
        return body  # fake transport retains the EXACT body object


def saved_entries(tmp_path):
    return json.loads((tmp_path / "ledger.json").read_text())["lanes"][0]


async def test_off_and_absent_accessor_never_touch_accounting(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("off path touched accounting")

    monkeypatch.setattr(runtime_module, "PersistenceOwner", forbidden)
    monkeypatch.setattr(runtime_module, "account_request", forbidden)
    monkeypatch.setattr(runtime_module.json, "loads", forbidden)
    first, second = Request(), Request()
    fallback = get_runtime(first)
    assert fallback is get_runtime(second)
    with pytest.raises(AttributeError):
        fallback.mode = "strict"
    disabled = ProspectiveRuntime(resolver=object(), scopes=object())
    for runtime in (fallback, disabled):
        await runtime.start()
        # Invalid input is intentionally untouched in off mode.
        body = object()
        assert await dispatch(runtime, selected=object(), body=body) is body
        assert runtime.reserve(None, None) is runtime.reserve(None, None)
        assert runtime.observations() == {}
        await runtime.aclose()


def test_request_metadata_is_local_clearable_and_immutable(tmp_path):
    request, sibling = Request(), Request()
    assert get_selected_dispatch(request) is None
    set_selected_dispatch(request, SELECTED)
    assert get_selected_dispatch(request) is SELECTED
    assert get_selected_dispatch(sibling) is None
    with pytest.raises(AttributeError):
        SELECTED.endpoint = "caller-value"
    with pytest.raises(TypeError):
        set_selected_dispatch(request, {"source": "caller-value"})
    set_selected_dispatch(request, None)
    assert get_selected_dispatch(request) is None
    runtime = configured(tmp_path)
    request.app[RUNTIME_KEY] = runtime
    assert get_runtime(request) is runtime
    request.app[RUNTIME_KEY] = None
    with pytest.raises(TypeError):
        get_runtime(request)


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "invalid"},
        {"resolver": None},
        {"scopes": []},
        {"max_pending": True},
        {"wait_timeout": float("inf")},
        {"budget_label": "not/public"},
        {"retry_after_s": None},
    ],
)
def test_enabled_configuration_requires_explicit_valid_inputs(tmp_path, change):
    with pytest.raises((TypeError, ValueError)):
        configured(tmp_path, **change)


async def test_strict_alias_default_and_final_bytes_are_preserved(runtimes, tmp_path):
    runtime = await runtimes()
    assert await dispatch(runtime) is BODY
    canonical = BODY.replace(b'"alias"', b'"canonical"')
    assert await dispatch(runtime, body=canonical) is canonical
    await runtime.aclose()
    cost = account_request(BODY, {"alias": 7})
    lane = saved_entries(tmp_path)
    assert lane["key"] == list(KEY)
    assert [entry["tokens"] for entry in lane["entries"]] == [cost.input_tokens + 7] * 2
    assert runtime.observations() == {
        "admitted": 0,
        "exhausted": 0,
        "unbound": 0,
        "unknown": 0,
    }


@pytest.mark.parametrize(
    "selected,body,reason",
    [
        (None, BODY, RefusalReason.UNBOUND),
        (replace(SELECTED, credential_source="caller-hash"), BODY, RefusalReason.UNBOUND),
        (replace(SELECTED, endpoint=ENDPOINT + "/"), BODY, RefusalReason.UNBOUND),
        (replace(SELECTED, topology="central"), BODY, RefusalReason.UNKNOWN),
        (replace(SELECTED, topology="ingress"), BODY, RefusalReason.UNKNOWN),
        (replace(SELECTED, internal_probe=True), BODY, RefusalReason.UNKNOWN),
        (SELECTED, BODY.replace(b'"alias"', b'"unknown"'), RefusalReason.UNBOUND),
        (SELECTED, b"invalid", RefusalReason.UNKNOWN),
        (SELECTED, BODY.decode(), RefusalReason.UNKNOWN),
        (SELECTED, b'{"model":"alias","input":"responses shape"}', RefusalReason.UNKNOWN),
        (SELECTED, BODY[:-1] + b',"max_tokens":null}', RefusalReason.UNKNOWN),
    ],
)
async def test_strict_unknown_or_unsupported_never_yields(
    runtimes, tmp_path, selected, body, reason
):
    runtime = await runtimes()
    with pytest.raises(LocalProspectiveRefusal) as caught:
        async with runtime.reserve(selected, body):
            pytest.fail("unknown or unsupported dispatch was admitted")
    refusal = caught.value.refusal
    assert refusal.reason is reason
    assert refusal.budget_label == "fixture-authority" and refusal.retry_after_s == 3
    assert not (tmp_path / "ledger.json").exists()


async def test_missing_default_and_mismatched_custody_fail_closed(runtimes, tmp_path):
    no_default = await runtimes(output_default=None)
    with pytest.raises(LocalProspectiveRefusal) as caught:
        await dispatch(no_default)
    assert caught.value.refusal.reason is RefusalReason.UNKNOWN
    await no_default.aclose()
    mismatched = await runtimes(
        scopes=[Scope(KEY, Budgets(10, 1000), str(tmp_path / "ledger.json"), True)]
    )
    with pytest.raises(LocalProspectiveRefusal) as caught:
        await dispatch(mismatched)
    assert caught.value.refusal.reason is RefusalReason.UNBOUND
    assert not (tmp_path / "ledger.json").exists()


async def test_strict_exhaustion_is_typed_and_does_not_dispatch(runtimes, tmp_path):
    from anthropic_throttle_proxy.metrics import M_PROSPECTIVE_REFUSALS

    refused = M_PROSPECTIVE_REFUSALS.labels(reason="exhausted")._value.get()
    runtime = await runtimes(budgets=Budgets(1, 500))
    await dispatch(runtime)
    with pytest.raises(LocalProspectiveRefusal) as caught:
        await dispatch(runtime)
    assert caught.value.refusal.reason is RefusalReason.EXHAUSTED
    await runtime.aclose()
    assert len(saved_entries(tmp_path)["entries"]) == 1
    assert M_PROSPECTIVE_REFUSALS.labels(reason="exhausted")._value.get() == refused + 1


@pytest.mark.parametrize("error", [OSError("wire failure"), TimeoutError("wire timeout")])
@pytest.mark.parametrize("handed_off", [False, True])
async def test_transport_exception_identity_is_never_local_policy(
    runtimes, tmp_path, error, handed_off
):
    runtime = await runtimes()
    with pytest.raises(type(error)) as caught:
        async with runtime.reserve(SELECTED, BODY) as permit:
            if handed_off:
                permit.handoff()
            raise error
    assert caught.value is error
    await runtime.aclose()
    assert len(saved_entries(tmp_path)["entries"]) == int(handed_off)


@pytest.mark.parametrize("handed_off", [False, True])
async def test_cancellation_propagates_and_preserves_handoff_boundary(
    runtimes, tmp_path, handed_off
):
    runtime = await runtimes()
    ready, never = asyncio.Event(), asyncio.Event()

    async def request():
        async with runtime.reserve(SELECTED, BODY) as permit:
            if handed_off:
                permit.handoff()
            ready.set()
            await never.wait()

    task = asyncio.create_task(request())
    await asyncio.wait_for(ready.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await runtime.aclose()
    assert len(saved_entries(tmp_path)["entries"]) == int(handed_off)


@pytest.mark.parametrize("phase", ["entry", "unsent-exit"])
async def test_owner_failure_is_local_and_faults_subsequent_dispatch(runtimes, monkeypatch, phase):
    runtime = await runtimes()

    def fail_save(_pool):
        raise OSError("private path must not become client text")

    if phase == "entry":
        monkeypatch.setattr(ledger_module.LedgerPool, "save", fail_save)
    with pytest.raises(LocalProspectiveRefusal) as caught:
        async with runtime.reserve(SELECTED, BODY):
            monkeypatch.setattr(ledger_module.LedgerPool, "save", fail_save)
    assert caught.value.refusal.reason is RefusalReason.UNKNOWN
    assert "private path" not in str(caught.value)
    with pytest.raises(LocalProspectiveRefusal):
        await dispatch(runtime)
    with pytest.raises(OwnerFaulted):
        await runtime.aclose()


async def test_handoff_after_close_is_local_refusal_and_unsent_rolls_back(runtimes, tmp_path):
    runtime = await runtimes()
    async with runtime.reserve(SELECTED, BODY) as permit:
        closing = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)  # runtime closes admission before its first drain await
        with pytest.raises(LocalProspectiveRefusal):
            permit.handoff()
    await closing
    assert saved_entries(tmp_path)["entries"] == []


async def test_observe_samples_exhaustion_without_refusing(runtimes, tmp_path):
    from anthropic_throttle_proxy.metrics import M_PROSPECTIVE_REFUSALS

    refused = M_PROSPECTIVE_REFUSALS.labels(reason="exhausted")._value.get()
    runtime = await runtimes(mode="observe", budgets=Budgets(1, 500))
    for _ in range(2):
        assert await dispatch(runtime) is BODY
        await asyncio.gather(*tuple(runtime._observing))
    assert await dispatch(runtime, selected=None) is BODY
    await runtime.aclose()
    assert runtime.observations() == {"admitted": 1, "exhausted": 1, "unbound": 1, "unknown": 0}
    assert len(saved_entries(tmp_path)["entries"]) == 1
    assert M_PROSPECTIVE_REFUSALS.labels(reason="exhausted")._value.get() == refused


async def test_observe_does_not_wait_for_fsync_and_bounds_shadow_work(runtimes, monkeypatch):
    runtime = await runtimes(mode="observe", max_pending=1)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    fsync = ledger_module.os.fsync

    def blocked(fd):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("test did not release fsync")
        fsync(fd)

    monkeypatch.setattr(ledger_module.os, "fsync", blocked)
    closing = None
    try:
        assert await dispatch(runtime) is BODY
        await asyncio.wait_for(entered.wait(), 2)
        assert await dispatch(runtime) is BODY  # worker still blocked, no request wait
        assert len(runtime._observing) == 1
        assert runtime.observations()["unknown"] == 1
        closing = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release.set()
        if closing is not None:
            await closing
    assert runtime.observations() == {"admitted": 1, "exhausted": 0, "unbound": 0, "unknown": 1}


async def test_observe_start_failure_is_visible_without_request_refusal(runtimes, tmp_path):
    (tmp_path / "ledger.json").write_text("corrupt snapshot")
    runtime = await runtimes(mode="observe")
    assert await dispatch(runtime) is BODY
    assert runtime.observations()["unknown"] == 2
    with pytest.raises(OwnerFaulted):
        await runtime.aclose()
