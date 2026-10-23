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


# ── routing-side strip (proxy._route_account_if_enabled) ───────────────────
# The injector above only rewrites the BODY. The header half of the contract
# lives in routing: the sentinel x-api-key must be REMOVED so the account
# router (not the explicit API-key lane) picks the upstream credential. If it
# leaked through, the sentinel would be sent to Anthropic and answer 401 —
# the exact production failure on 22/10/2026 (two gateway units raced for
# :8765 and the survivor had no sentinel configured).
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


def test_list_content_gets_preamble_block(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.dumps(
        {
            "system": [{"type": "text", "text": "You are pi."}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "first"},
                        {"type": "text", "text": "second"},
                    ],
                }
            ],
        }
    ).encode()
    obj = json.loads(proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {}))
    content = obj["messages"][0]["content"]
    assert isinstance(content, list) and len(content) == 3
    assert content[0]["text"].startswith("[system preamble]")
    assert content[1]["text"] == "first" and content[2]["text"] == "second"


def test_no_messages_still_gets_marker(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", SENTINEL)
    body = json.dumps({"system": "You are pi."}).encode()
    obj = json.loads(proxy._inject_lane_system_prompt(_req({"x-api-key": SENTINEL}), body, {}))
    assert obj["system"] == [{"type": "text", "text": config.CLAUDE_CODE_SYSTEM_PROMPT}]
