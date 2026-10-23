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


def _req(headers: dict[str, str]) -> object:
    return make_mocked_request("POST", "/v1/messages", headers=headers)


def _pi_body() -> bytes:
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
    return json.dumps(
        {
            "model": "claude-opus-5",
            "max_tokens": 64,
            "system": [
                {"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT},
                {
                    "type": "text",
                    "text": "<tool guide>",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode("utf-8")


def test_sentinel_request_gets_marker_and_fold(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    out = proxy._inject_lane_system_prompt(
        _req({"x-api-key": SENTINEL}), _pi_body(), {"x-api-key": SENTINEL}
    )
    obj = json.loads(out)
    assert obj["system"] == [{"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT}]
    first = obj["messages"][0]["content"]
    joined = first[0]["text"] if isinstance(first, list) else first
    assert "[system preamble]" in joined and "You are pi" in joined


def test_authorization_bearer_is_byte_identical(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = _cc_body()
    out = proxy._inject_lane_system_prompt(_req({"Authorization": "Bearer oauth-token"}), body, {})
    assert out is body  # cache_control + multi-block system untouched


def test_non_sentinel_api_key_untouched(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = _pi_body()
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": "sk-ant-real-key"}), body, {})
    assert out is body


def test_marker_first_body_untouched(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = _cc_body()  # first system block IS the marker
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {})
    assert out is body


def test_no_sentinel_configured_untouched(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", "")
    body = _pi_body()
    out = proxy._inject_lane_system_prompt(_req({}), body, {})
    assert out is body


# ── Degenerate shapes: the injector must pass them through untouched ───────
# These branches are not exotic: pi sends a plain string system prompt when a
# role has no templated system blocks, and a tool-heavy turn can carry a
# string first-message content. Each one previously sat uncovered, which is
# what failed the new-code coverage gate on #354.


def test_unparsable_body_passes_through(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = b"<html>not json at all</html>"
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {})
    assert out is body


def test_non_dict_json_body_passes_through(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.dumps([1, 2, 3]).encode("utf-8")
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {})
    assert out is body


def test_string_system_is_folded_and_replaced(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.dumps(
        {
            "model": "claude-opus-5-5",
            "max_tokens": 32,
            "system": "You are pi.",
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode("utf-8")
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {})
    obj = json.loads(out)
    assert obj["system"] == [{"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT}]
    # the client's own prompt survives, folded into the first user message
    assert "[system preamble]\nYou are pi." in obj["messages"][0]["content"]


def test_missing_system_only_adds_marker(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.dumps(
        {
            "model": "claude-opus-5-5",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode("utf-8")
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {})
    obj = json.loads(out)
    assert obj["system"] == [{"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT}]
    assert obj["messages"][0]["content"] == "hi"  # nothing to fold


def test_empty_system_list_only_adds_marker(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.dumps(
        {
            "model": "claude-opus-5-5",
            "max_tokens": 32,
            "system": [],
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode("utf-8")
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {})
    assert json.loads(out)["system"] == [{"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT}]


def test_string_first_message_content_is_prefixed(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.dumps(
        {
            "model": "claude-opus-5-5",
            "max_tokens": 32,
            "system": [{"type": "text", "text": "You are pi."}],
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode("utf-8")
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {})
    content = json.loads(out)["messages"][0]["content"]
    assert isinstance(content, str)  # str content stays str
    assert content.endswith("hi")
    assert "[system preamble]" in content and "You are pi." in content


def test_list_first_message_content_is_prefixed(monkeypatch):
    """The shape Claude-Code-style clients actually send: content blocks.

    Folded as a leading text block so the client's own blocks (and any
    cache_control on them) keep their relative order afterwards.
    """
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.dumps(
        {
            "model": "claude-opus-5-5",
            "max_tokens": 32,
            "system": [{"type": "text", "text": "You are pi."}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
                    ],
                }
            ],
        }
    ).encode("utf-8")
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {})
    content = json.loads(out)["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text" and "[system preamble]" in content[0]["text"]
    # the client's own block comes after, untouched
    assert content[1] == {
        "type": "text",
        "text": "hi",
        "cache_control": {"type": "ephemeral"},
    }


def test_route_strips_sentinel_and_drops_explicit_api_key(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: False)
    headers = {"x-api-key": SENTINEL, "content-type": "application/json"}
    proxy._route_account_if_enabled(headers, "bearer-in", method="POST", path="/v1/messages")
    assert "x-api-key" not in headers


def test_route_keeps_real_api_key_when_not_the_sentinel(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: False)
    headers = {"x-api-key": "sk-ant-real", "content-type": "application/json"}
    proxy._route_account_if_enabled(headers, "bearer-in", method="POST", path="/v1/messages")
    assert headers["x-api-key"] == "sk-ant-real"


# ── injector edges: every pass-through must be byte-identical ──────────────


def test_unparsable_body_untouched(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = b"not-json{"
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {})
    assert out is body


def test_non_object_json_untouched(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = b"[1, 2, 3]"
    out = proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {})
    assert out is body


def test_get_request_untouched(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = b'{"system": "x"}'
    req = make_mocked_request("GET", "/v1/models", headers={"x-api-key": SENTINEL})
    assert proxy._inject_lane_system_prompt(req, body, {}) is body


def test_none_body_untouched(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    assert proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), None, {}) is None


def test_missing_system_gets_marker_only(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    obj = json.loads(proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {}))
    assert obj["system"] == [{"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT}]
    # No pi system at all → nothing to fold into the user message.
    assert obj["messages"][0]["content"] == "hi"


def test_empty_system_list_gets_marker_only(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.dumps({"system": [], "messages": [{"role": "user", "content": "hi"}]}).encode()
    obj = json.loads(proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {}))
    assert obj["system"] == [{"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT}]
    assert obj["messages"][0]["content"] == "hi"


def test_string_system_folded_into_string_content(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.dumps(
        {"system": "You are pi.", "messages": [{"role": "user", "content": "hi"}]}
    ).encode()
    obj = json.loads(proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {}))
    assert obj["system"] == [{"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT}]
    content = obj["messages"][0]["content"]
    assert content.startswith("[system preamble]\nYou are pi.")
    assert content.endswith("hi")


def test_no_messages_still_gets_marker(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.dumps({"system": "You are pi."}).encode()
    obj = json.loads(proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {}))
    assert obj["system"] == [{"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT}]


# ── routing-side strip (proxy._route_account_if_enabled) ───────────────────
# The sentinel x-api-key must be REMOVED so the account router (not the
# explicit API-key lane) picks the upstream credential. If it leaked through,
# Anthropic would answer 401 — the 22/10/2026 production failure.
@pytest.mark.parametrize(
    ("incoming", "still_present"),
    [("local-lane-via-8765", None), ("sk-ant-real", "sk-ant-real")],
)
def test_route_account_rewrites_only_the_sentinel(monkeypatch, incoming, still_present):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", "local-lane-via-8765")
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: False)
    headers = {"x-api-key": incoming, "content-type": "application/json"}
    proxy._route_account_if_enabled(headers, "bearer-in", method="POST", path="/v1/messages")
    assert headers.get("x-api-key") == still_present


# ── injector pass-throughs: every skip must be byte-identical ─────────────
@pytest.mark.parametrize(
    ("method", "headers", "body"),
    [
        ("POST", {"x-api-key": "local-lane-via-8765"}, b"not-json{"),
        ("POST", {"x-api-key": "local-lane-via-8765"}, b"[1, 2, 3]"),
        ("POST", {"x-api-key": "local-lane-via-8765"}, None),
        ("GET", {"x-api-key": "local-lane-via-8765"}, b'{"system": "x"}'),
    ],
    ids=["unparsable", "non-object", "no-body", "non-post"],
)
def test_injector_passes_through_unchanged(monkeypatch, method, headers, body):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", "local-lane-via-8765")
    request = make_mocked_request(method, "/v1/messages", headers=headers)
    assert proxy._inject_lane_system_prompt(request, body, {}) is body


# ── injector shaping: marker-only when there is no pi system to fold ──────
@pytest.mark.parametrize(
    "payload",
    [
        {"messages": [{"role": "user", "content": "hi"}]},
        {"system": [], "messages": [{"role": "user", "content": "hi"}]},
        {"system": "You are pi."},
    ],
    ids=["no-system", "empty-system", "system-but-no-messages"],
)
def test_marker_only_when_nothing_to_fold(monkeypatch, payload):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", "local-lane-via-8765")
    body = json.dumps(payload).encode()
    obj = json.loads(
        proxy._inject_lane_system_prompt(_req({"x-api-key": "local-lane-via-8765"}), body, {})
    )
    assert obj["system"] == [{"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT}]
    if "messages" in payload:
        assert obj["messages"][0]["content"] == "hi"


def test_list_content_gets_preamble_block(monkeypatch):
    """pi's prompt folds as a leading text block when content is a list."""
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.loads(_pi_body())
    body["messages"][0]["content"] = [{"type": "text", "text": "first"}]
    out = proxy._inject_lane_system_prompt(
        _req({"x-api-key": SENTINEL}), json.dumps(body).encode(), {}
    )
    folded = json.loads(out)["messages"][0]["content"]
    assert folded[0]["text"].startswith("[system preamble]") and folded[1]["text"] == "first"
