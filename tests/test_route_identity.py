"""T003 route identity — trusted selected source + exact target metadata.

The route decision's selected operator account label and THIS attempt's exact
configured target must survive into request-local dispatch metadata; a reroute
clears stale authority; nothing may derive a source from caller headers, a
bearer hash or token contents. The #297 bridge (``prospective_runtime``) is
faked here with exactly its published contract (``SelectedDispatch`` +
``get_selected_dispatch``/``set_selected_dispatch``): no files, network, env or
runtime flags. Strict/observe/off behavior belongs to the bridge, not the route
functions, which only ever record explicit metadata or explicit unknown.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from anthropic_throttle_proxy import config, proxy


class _SelectedDispatch:
    """Mirror of prospective_runtime.SelectedDispatch (keyword contract)."""

    def __init__(self, *, credential_source, endpoint, topology, internal_probe):
        self.credential_source = credential_source
        self.endpoint = endpoint
        self.topology = topology
        self.internal_probe = internal_probe


@pytest.fixture
def bridge(monkeypatch):
    fake = types.ModuleType("anthropic_throttle_proxy.prospective_runtime")
    store: dict[int, _SelectedDispatch] = {}
    fake.SelectedDispatch = _SelectedDispatch
    fake.set_selected_dispatch = lambda request, value: store.__setitem__(id(request), value)
    fake.get_selected_dispatch = lambda request: store.get(id(request))
    monkeypatch.setitem(sys.modules, "anthropic_throttle_proxy.prospective_runtime", fake)
    import anthropic_throttle_proxy

    monkeypatch.setattr(anthropic_throttle_proxy, "prospective_runtime", fake)
    return SimpleNamespace(store=store, module=fake)


def _fake_route(monkeypatch, label, claimed=False):
    def fake(headers, bid, *, method, path, model, max_tokens):
        return "bid-selected", label, claimed

    monkeypatch.setattr(proxy, "_route_account_and_claim_retry_probe", fake)


def _request() -> SimpleNamespace:
    return SimpleNamespace(method="POST", query_string="")


def test_claim_route_records_trusted_selected_source(bridge, monkeypatch):
    """The selected operator account label is the ONLY source authority."""
    _fake_route(monkeypatch, "team-b")
    request = _request()
    proxy._claim_route(
        request,
        "v1/messages",
        {"authorization": "Bearer tp-secret-token"},
        "incoming",
        "mimo-v2.6",
        1024,
        "direct",
    )
    recorded = bridge.store[id(request)]
    assert recorded.credential_source == "team-b"
    assert recorded.endpoint == config.UPSTREAM  # exact configured base
    assert recorded.topology == "direct"
    assert recorded.internal_probe is False  # a real user turn, not a probe


def test_claim_route_missing_label_is_explicit_unknown(bridge, monkeypatch):
    """A header or hash-looking bid is never authority; missing stays None."""
    _fake_route(monkeypatch, None)
    request = _request()
    proxy._claim_route(
        request,
        "v1/messages",
        {"authorization": "Bearer b144f62f", "x-api-key": "tp-secret"},
        "b144f62f",
        "mimo-v2.6",
        None,
        "direct",
    )
    recorded = bridge.store[id(request)]
    assert recorded.credential_source is None


def test_central_relay_never_pretends_direct_authority(bridge, monkeypatch):
    _fake_route(monkeypatch, "plan")
    request = _request()
    proxy._claim_route(request, "v1/messages", {}, "incoming", "mimo-v2.6", None, "central")
    recorded = bridge.store[id(request)]
    assert recorded.topology == "central"
    assert recorded.endpoint == config.CENTRAL_URL  # NOT the direct base


def test_unknown_target_stays_explicit(bridge):
    blank = proxy._selected_dispatch("", credential_source="team-b")
    assert blank.endpoint is None
    assert blank.topology == "unknown"
    other = proxy._selected_dispatch("ingress-relay", credential_source=None)
    assert other.endpoint is None
    assert other.topology == "ingress-relay"  # verbatim: strict refuses, never guessed
    assert other.internal_probe is False


def test_reroute_replaces_stale_authority(bridge, monkeypatch):
    request = _request()
    _fake_route(monkeypatch, "team-b")
    proxy._claim_route(request, "v1/messages", {}, "incoming", "m", None, "direct")
    assert bridge.store[id(request)].credential_source == "team-b"
    _fake_route(monkeypatch, "plan")
    proxy._reroute_from_base(
        {},
        "incoming",
        {"bid": ""},
        set(),
        request=request,
        path="v1/messages",
        model="m",
        req_max_tokens=None,
        via="direct",
    )
    assert bridge.store[id(request)].credential_source == "plan"


def test_reroute_without_authority_clears_source(bridge, monkeypatch):
    """A credential replacement with no authoritative label clears the source —
    never derives it from the bearer hash or any label map."""
    request = _request()
    _fake_route(monkeypatch, "team-b")
    proxy._claim_route(request, "v1/messages", {}, "incoming", "m", None, "direct")
    _fake_route(monkeypatch, None)
    proxy._reroute_from_base(
        {},
        "deadbeef",
        {"bid": ""},
        set(),
        request=request,
        path="v1/messages",
        model="m",
        req_max_tokens=None,
        via="direct",
    )
    recorded = bridge.store[id(request)]
    assert recorded.credential_source is None
    assert recorded.endpoint == config.UPSTREAM  # target refreshed too


async def test_direct_fallback_preserves_source_only_from_the_record(bridge, monkeypatch):
    """Same headers = same credential: preserve the recorded source while the
    target identity is refreshed for the new attempt."""

    async def failing_forward(*args, **kwargs):
        return None, RuntimeError("net")

    monkeypatch.setattr(proxy, "_try_forward", failing_forward)
    request = _request()
    bridge.module.set_selected_dispatch(
        request,
        _SelectedDispatch(
            credential_source="team-b",
            endpoint=config.CENTRAL_URL,
            topology="central",
            internal_probe=False,
        ),
    )
    attempt = SimpleNamespace(context={}, final_status=None)
    response = await proxy._retry_direct_once(
        request,
        {"authorization": "Bearer tp-secret"},
        b"{}",
        "v1/messages",
        "direct",
        "http://upstream/v1/messages",
        None,
        RuntimeError("net"),
        attempt,
        "bid-selected",
    )
    assert response.status == 502
    recorded = bridge.store[id(request)]
    assert recorded.credential_source == "team-b"  # preserved: headers unchanged
    assert recorded.endpoint == config.UPSTREAM
    assert recorded.topology == "direct"


async def test_direct_fallback_without_record_is_unknown(bridge, monkeypatch):
    async def failing_forward(*args, **kwargs):
        return None, RuntimeError("net")

    monkeypatch.setattr(proxy, "_try_forward", failing_forward)
    request = _request()
    attempt = SimpleNamespace(context={}, final_status=None)
    await proxy._retry_direct_once(
        request,
        {},
        b"{}",
        "v1/messages",
        "direct",
        "http://upstream/v1/messages",
        None,
        RuntimeError("net"),
        attempt,
        "b144f62f",
    )
    recorded = bridge.store[id(request)]
    assert recorded.credential_source is None  # never derived from the bid hash
    assert recorded.topology == "direct"


def test_retry_after_reroute_replaces_stale_authority(bridge, monkeypatch):
    """P1 (pF): a successful Retry-After replacement swaps credentials WITHOUT
    passing _reroute_from_base — the record must carry the NEW routing label
    (clearing to None when the decision has none), never A's on B's headers."""
    request = _request()
    bridge.module.set_selected_dispatch(
        request,
        _SelectedDispatch(
            credential_source="plan",
            endpoint=config.UPSTREAM,
            topology="direct",
            internal_probe=False,
        ),  # stale A
    )

    def fake_reroute(*args, **kwargs):
        return "bid-b", "team-b", {"authorization": "Bearer b-token"}

    monkeypatch.setattr(proxy, "_retry_after_reroute_headers", fake_reroute)
    outcome = proxy._try_retry_after_reroute(
        {},
        "incoming",
        "bid-a",
        set(),
        method="POST",
        path="v1/messages",
        model="m",
        max_tokens=None,
        cid="cx",
        retry_after_remaining=30,
        source="pre-dispatch",
        request=request,
        via="direct",
    )
    assert outcome == ("bid-b", {"authorization": "Bearer b-token"})
    assert bridge.store[id(request)].credential_source == "team-b"  # RED: stays "plan"

    monkeypatch.setattr(
        proxy, "_retry_after_reroute_headers", lambda *a, **k: ("bid-c", None, {"x": "1"})
    )
    proxy._try_retry_after_reroute(
        {},
        "incoming",
        "bid-b",
        set(),
        method="POST",
        path="v1/messages",
        model="m",
        max_tokens=None,
        cid="cx",
        retry_after_remaining=5,
        source="pre-dispatch",
        request=request,
        via="direct",
    )
    assert bridge.store[id(request)].credential_source is None  # cleared, not derived
