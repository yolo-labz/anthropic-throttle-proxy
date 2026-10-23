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
