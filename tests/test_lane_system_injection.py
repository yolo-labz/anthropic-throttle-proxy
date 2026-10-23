"""Spec 344 — sentinel-lane system injection scoping.

The injector exists so subscription (OAuth) bearers accept pi's sentinel-lane
requests (Claude Code identity must be the FIRST system block). The original
uncommitted version gated only on the process-wide env var, so it rewrote
EVERY POST — including Claude Code's own traffic via the ingress — destroying
``cache_control`` and with it prompt caching on every pane.

Contract under test:

1. Sentinel-lane request (``x-api-key`` == sentinel, no ``Authorization``)
   with pi's own system → marker-only system, prompt folded into the first
   user message.
2. Claude Code traffic (``Authorization`` bearer) → body byte-identical,
   ``cache_control`` survives untouched.
3. Plain API-key traffic (``x-api-key`` != sentinel, no Authorization) →
   untouched (it is explicit api-key traffic, not lane traffic).
4. Body already carrying the marker as first system block → untouched (no
   double-fold).
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import make_mocked_request

from anthropic_throttle_proxy import config, proxy

SENTINEL = "local-lane-via-8765"


def _req(headers: dict[str, str], method: str = "POST") -> object:
    return make_mocked_request(method, "/v1/messages", headers=headers)


def _pi_body() -> bytes:
    """pi's own shape: its system prompt, a plain user message."""
    return json.dumps(
        {
            "model": "claude-haiku-5-5",
            "max_tokens": 64,
            "system": [
                {"type": "text", "text": "You are pi, a terminal coding agent."},
                {"type": "text", "text": "Be terse."},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode("utf-8")


def _cc_body() -> bytes:
    """Claude Code's own shape: marker first, then a cached tool block."""
    return json.dumps(
        {
            "model": "claude-opus-5",
            "max_tokens": 64,
            "system": [
                {"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT},
                {"type": "text", "text": "<tool guide>", "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode("utf-8")


def test_sentinel_request_gets_marker_and_fold(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), _pi_body())
    obj = json.loads(out)
    assert obj["system"] == [{"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT}]
    first = obj["messages"][0]["content"]
    joined = first[0]["text"] if isinstance(first, list) else first
    assert "[system preamble]" in joined and "You are pi" in joined


# Every shape that must reach upstream BYTE-IDENTICAL. One table instead of a
# near-identical five-line test per case (the gate reads those as clones, and
# the contract is the same: the returned object is the caller's object).
PASS_THROUGH = {
    "authorization-bearer": ({"Authorization": "Bearer oauth-token"}, "POST", "cc"),
    "non-sentinel-api-key": ({"x-api-key": "sk-ant-real-key"}, "POST", "pi"),
    "marker-first": ({"x-api-key": SENTINEL}, "POST", "cc"),
    "unparsable-body": ({"x-api-key": SENTINEL}, "POST", "unparsable"),
    "non-object-json": ({"x-api-key": SENTINEL}, "POST", "list"),
    "no-body": ({"x-api-key": SENTINEL}, "POST", None),
    "non-post": ({"x-api-key": SENTINEL}, "GET", "pi"),
}


@pytest.mark.parametrize(
    ("headers", "method", "kind"), PASS_THROUGH.values(), ids=PASS_THROUGH.keys()
)
def test_untouched_shapes_return_the_same_bytes(monkeypatch, headers, method, kind):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = {
        "cc": _cc_body,
        "pi": _pi_body,
        "unparsable": lambda: b"not-json{",
        "list": lambda: b"[1, 2, 3]",
        None: lambda: None,
    }[kind]()
    assert proxy._inject_lane_system_prompt(_req(headers, method), body) is body


def test_no_sentinel_configured_untouched(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", "")
    body = _pi_body()
    assert proxy._inject_lane_system_prompt(_req({}), body) is body


# With no pi system to fold, the marker is the only system block.
MARKER_ONLY = {
    "no-system": {"messages": [{"role": "user", "content": "hi"}]},
    "empty-system": {"system": [], "messages": [{"role": "user", "content": "hi"}]},
    "no-messages": {"system": "You are pi."},
}


@pytest.mark.parametrize("payload", MARKER_ONLY.values(), ids=MARKER_ONLY.keys())
def test_marker_only_when_nothing_to_fold(monkeypatch, payload):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    out = proxy._inject_lane_system_prompt(
        _req({"x-api-key": SENTINEL}), json.dumps(payload).encode()
    )
    obj = json.loads(out)
    assert obj["system"] == [{"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT}]
    if "messages" in payload:
        assert obj["messages"][0]["content"] == "hi"


def test_list_content_gets_preamble_block(monkeypatch):
    """pi's prompt folds as a leading text block when content is a list."""
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.loads(_pi_body())
    body["messages"][0]["content"] = [{"type": "text", "text": "first"}]
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), json.dumps(body).encode())
    folded = json.loads(out)["messages"][0]["content"]
    assert folded[0]["text"].startswith("[system preamble]") and folded[1]["text"] == "first"


# The header half of the contract: the sentinel key is STRIPPED so the account
# router (not the explicit API-key lane) picks the upstream credential.
ROUTE_CASES = {"sentinel": (SENTINEL, None), "real-key": ("sk-ant-real", "sk-ant-real")}


@pytest.mark.parametrize(
    ("incoming", "still_present"), ROUTE_CASES.values(), ids=ROUTE_CASES.keys()
)
def test_route_account_rewrites_only_the_sentinel(monkeypatch, incoming, still_present):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: False)
    headers = {"x-api-key": incoming, "content-type": "application/json"}
    proxy._route_account_if_enabled(headers, "bearer-in", method="POST", path="/v1/messages")
    assert headers.get("x-api-key") == still_present


@pytest.mark.parametrize(
    ("system", "has_breakpoint"),
    [
        (
            [{"type": "text", "text": "You are pi.", "cache_control": {"type": "ephemeral"}}],
            True,
        ),
        ("You are pi.", False),
    ],
    ids=["list-with-breakpoint", "plain-string"],
)
def test_fold_preserves_the_cache_breakpoint(monkeypatch, system, has_breakpoint):
    """pi's cache_control must survive the system->user move.

    The marker-only system exists so the (large) pi prompt stays a cached
    prefix; dropping the breakpoint re-bills it as fresh input at the
    cache-write rate on every turn.
    """
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    payload = json.loads(_pi_body())
    payload["system"] = system
    out = json.loads(
        proxy._inject_lane_system_prompt(
            _req({"x-api-key": SENTINEL}), json.dumps(payload).encode()
        )
    )
    folded = out["messages"][0]["content"]
    if has_breakpoint:
        assert isinstance(folded, list)
        assert folded[0]["cache_control"] == {"type": "ephemeral"}
        assert folded[0]["text"].startswith("[system preamble]")
        assert folded[1]["text"] == "hi"
    else:
        # Nothing to carry: keep the historical string shape.
        assert isinstance(folded, str) and folded.endswith("hi")
        assert "[system preamble]" in folded


# ── fleet budget reserve (23/10/2026) ─────────────────────────────────────
# Several panes share ONE subscription window with interactive Claude Code.
# Above the ceiling the FLEET yields; the human's own Authorization traffic
# never does — that is the whole point of the reserve.
def _set_lane_util(monkeypatch, util: float) -> None:
    monkeypatch.setitem(
        proxy.bearer_state,
        "budget-test",
        {"unified": {"util_5h": util, "reset_5h": 4_000_000_000}},
    )


@pytest.mark.parametrize(
    ("ceiling", "util", "headers", "expect_status"),
    [
        (0.0, 0.99, {"x-api-key": SENTINEL}, None),  # reserve disabled
        (0.85, 0.10, {"x-api-key": SENTINEL}, None),  # plenty of window left
        (0.85, 0.86, {"x-api-key": SENTINEL}, 429),  # fleet yields
        (0.85, 0.86, {"Authorization": "Bearer cc"}, None),  # Claude Code never
    ],
    ids=["off", "below", "above-fleet", "above-claude-code"],
)
def test_fleet_budget_reserve(monkeypatch, ceiling, util, headers, expect_status):
    monkeypatch.setattr(config, "FLEET_MAX_UTILIZATION", ceiling)
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    _set_lane_util(monkeypatch, util)
    response = proxy._fleet_budget_response(_req(headers))
    if expect_status is None:
        assert response is None
        return
    assert response.status == expect_status
    assert response.headers["x-anthropic-throttle-fleet-reserve"] == "1"
    assert int(response.headers["retry-after"]) > 0
