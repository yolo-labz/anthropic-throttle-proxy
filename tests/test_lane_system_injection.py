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
