"""Tests for body_shrink — keep POST /v1/messages bodies under 32 MiB.

The trim algorithm walks ``messages`` oldest-first and replaces
``tool_result`` content payloads with breadcrumb stubs until the body fits
under ``CAP_BYTES``. These tests cover the contract the proxy depends on:
passthrough on small bodies, trim on oversize bodies, preserve last
``KEEP_TURNS`` messages, leave non-``tool_result`` content alone, bypass on
non-``/v1/messages`` paths, and surface metrics fields the operator needs.
"""

from __future__ import annotations

import json

import pytest

from anthropic_throttle_proxy import body_shrink


def _body(messages, model="claude-opus-4-7"):
    return json.dumps({"model": model, "messages": messages}).encode("utf-8")


def _tool_result(tool_use_id: str, payload_bytes: int) -> dict:
    """A tool_result block whose serialized size is roughly ``payload_bytes``."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": [{"type": "text", "text": "x" * payload_bytes}],
    }


def _user_with(blocks: list[dict]) -> dict:
    return {"role": "user", "content": blocks}


def _assistant_with(blocks: list[dict]) -> dict:
    return {"role": "assistant", "content": blocks}


def test_passthrough_under_cap(monkeypatch):
    monkeypatch.setattr(body_shrink, "CAP_BYTES", 1024 * 1024)
    body = _body([_user_with([{"type": "text", "text": "hi"}])])
    out, meta = body_shrink.shrink_body(body, "/v1/messages")
    assert out == body
    assert meta["trimmed"] is False
    assert meta["original_bytes"] == len(body)


def test_bypass_non_messages_path(monkeypatch):
    monkeypatch.setattr(body_shrink, "CAP_BYTES", 10)  # tiny cap
    body = _body([_user_with([_tool_result("t1", 1000)])])
    out, meta = body_shrink.shrink_body(body, "/v1/some-other-endpoint")
    assert out == body
    assert meta["trimmed"] is False
    assert meta["reason"] == "non-messages-path"


def test_catchall_route_path_shape_still_triggers(monkeypatch):
    # Regression: aiohttp's `/{path:.*}` catchall yields the captured path
    # WITHOUT a leading slash ("v1/messages"), but PR #15's original guard
    # checked for "/v1/messages" — making body_shrink silently dead code in
    # production. This test pins the prod-shape path to ensure we never lose
    # the fire path again.
    monkeypatch.setattr(body_shrink, "CAP_BYTES", 8000)
    monkeypatch.setattr(body_shrink, "KEEP_TURNS", 2)
    monkeypatch.setattr(body_shrink, "MIN_BLOCK_BYTES", 100)
    big = 4000
    messages = [
        _user_with([_tool_result("t1", big)]),
        _assistant_with([{"type": "text", "text": "ok"}]),
        _user_with([_tool_result("t2", big)]),
        _assistant_with([{"type": "text", "text": "done"}]),
    ]
    body = _body(messages)
    out, meta = body_shrink.shrink_body(body, "v1/messages")  # NO leading slash
    assert meta["trimmed"] is True, meta
    assert meta["blocks_trimmed"] >= 1
    assert meta["bytes_saved"] > 0
    assert len(out) < len(body)


def test_bypass_disabled(monkeypatch):
    monkeypatch.setattr(body_shrink, "CAP_BYTES", 0)
    body = _body([_user_with([_tool_result("t1", 1000)])])
    out, meta = body_shrink.shrink_body(body, "/v1/messages")
    assert out == body
    assert meta["reason"] == "disabled"


def test_non_json_body_passes_through(monkeypatch):
    monkeypatch.setattr(body_shrink, "CAP_BYTES", 10)
    body = b"not json at all, just a binary blob" * 100
    out, meta = body_shrink.shrink_body(body, "/v1/messages")
    assert out == body
    assert meta["trimmed"] is False
    assert meta["reason"] == "non-json"


def test_trims_oldest_first(monkeypatch):
    monkeypatch.setattr(body_shrink, "CAP_BYTES", 8000)
    monkeypatch.setattr(body_shrink, "KEEP_TURNS", 2)
    monkeypatch.setattr(body_shrink, "MIN_BLOCK_BYTES", 100)
    big = 4000
    messages = [
        _user_with([_tool_result("t1", big)]),  # oldest — should be trimmed
        _assistant_with([{"type": "text", "text": "ok"}]),
        _user_with([_tool_result("t2", big)]),  # protected by KEEP_TURNS
        _assistant_with([{"type": "text", "text": "done"}]),
    ]
    body = _body(messages)
    assert len(body) > 8000
    out, meta = body_shrink.shrink_body(body, "/v1/messages")
    assert meta["trimmed"] is True
    assert meta["blocks_trimmed"] >= 1
    assert meta["bytes_saved"] > 0
    decoded = json.loads(out)
    # Oldest tool_result swapped for a breadcrumb stub.
    oldest = decoded["messages"][0]["content"][0]
    assert oldest["type"] == "tool_result"
    assert oldest["tool_use_id"] == "t1"
    assert "throttle-proxy trimmed" in oldest["content"][0]["text"]
    # Last two messages untouched (KEEP_TURNS=2).
    last_user = decoded["messages"][2]["content"][0]
    assert last_user["content"][0]["text"] == "x" * big


def test_preserves_keep_turns_even_when_still_oversize(monkeypatch):
    monkeypatch.setattr(body_shrink, "CAP_BYTES", 100)
    monkeypatch.setattr(body_shrink, "KEEP_TURNS", 2)
    monkeypatch.setattr(body_shrink, "MIN_BLOCK_BYTES", 50)
    messages = [
        _user_with([_tool_result("t1", 5000)]),  # oldest — trimmed
        _assistant_with([{"type": "text", "text": "ok"}]),
        _user_with([_tool_result("t2", 5000)]),  # PROTECTED — must survive
        _assistant_with([{"type": "text", "text": "done"}]),
    ]
    body = _body(messages)
    out, meta = body_shrink.shrink_body(body, "/v1/messages")
    assert meta["trimmed"] is True
    assert meta["still_oversize"] is True
    decoded = json.loads(out)
    # KEEP_TURNS guarantee — last tool_result untouched.
    assert decoded["messages"][2]["content"][0]["content"][0]["text"] == "x" * 5000


def test_leaves_non_tool_result_blocks_alone(monkeypatch):
    monkeypatch.setattr(body_shrink, "CAP_BYTES", 1000)
    monkeypatch.setattr(body_shrink, "KEEP_TURNS", 1)
    monkeypatch.setattr(body_shrink, "MIN_BLOCK_BYTES", 100)
    # A big text block + a big tool_use block — neither should be touched.
    huge_text = {"type": "text", "text": "x" * 4000}
    huge_tool_use = {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"x": "y" * 4000}}
    huge_tr = _tool_result("t1", 4000)
    messages = [
        _user_with([huge_text, huge_tool_use, huge_tr]),
        _assistant_with([{"type": "text", "text": "ok"}]),
    ]
    body = _body(messages)
    out, _meta = body_shrink.shrink_body(body, "/v1/messages")
    decoded = json.loads(out)
    blocks = decoded["messages"][0]["content"]
    # text and tool_use intact, only tool_result rewritten.
    assert blocks[0] == huge_text
    assert blocks[1] == huge_tool_use
    assert blocks[2]["type"] == "tool_result"
    assert blocks[2]["tool_use_id"] == "t1"
    assert "throttle-proxy trimmed" in blocks[2]["content"][0]["text"]


def test_skips_blocks_below_min_threshold(monkeypatch):
    monkeypatch.setattr(body_shrink, "CAP_BYTES", 1000)
    monkeypatch.setattr(body_shrink, "KEEP_TURNS", 1)
    monkeypatch.setattr(body_shrink, "MIN_BLOCK_BYTES", 5000)  # everything below threshold
    messages = [
        _user_with([_tool_result("t1", 2000)]),
        _user_with([_tool_result("t2", 2000)]),
        _assistant_with([{"type": "text", "text": "ok"}]),
    ]
    body = _body(messages)
    out, meta = body_shrink.shrink_body(body, "/v1/messages")
    # Nothing was big enough to trim → no trim happened.
    assert meta["blocks_trimmed"] == 0
    assert json.loads(out) == json.loads(body)


def test_stub_drops_cache_control_marker(monkeypatch):
    monkeypatch.setattr(body_shrink, "CAP_BYTES", 1500)
    monkeypatch.setattr(body_shrink, "KEEP_TURNS", 1)
    monkeypatch.setattr(body_shrink, "MIN_BLOCK_BYTES", 100)
    cached_tr = _tool_result("t1", 3000)
    cached_tr["cache_control"] = {"type": "ephemeral"}
    messages = [
        _user_with([cached_tr]),
        _assistant_with([{"type": "text", "text": "ok"}]),
    ]
    body = _body(messages)
    out, meta = body_shrink.shrink_body(body, "/v1/messages")
    assert meta["trimmed"] is True
    decoded = json.loads(out)
    trimmed = decoded["messages"][0]["content"][0]
    # cache_control gone — a stub must not anchor a cache breakpoint, since
    # the hash it would compute has changed.
    assert "cache_control" not in trimmed


@pytest.mark.parametrize("missing", ["messages", "model"])
def test_handles_missing_top_level_fields(monkeypatch, missing):
    monkeypatch.setattr(body_shrink, "CAP_BYTES", 100)
    monkeypatch.setattr(body_shrink, "KEEP_TURNS", 1)
    monkeypatch.setattr(body_shrink, "MIN_BLOCK_BYTES", 100)
    # Payload must be over the cap. Add a sentinel trailing message so the
    # trimmable-window check (KEEP_TURNS=1) has room to walk into the older
    # message and exercise the trim path.
    pad = {"_pad": "x" * 4000}
    payload = {
        "messages": [
            _user_with([_tool_result("t1", 4000)]),
            _assistant_with([{"type": "text", "text": "ok"}]),
        ],
        "model": "x",
        **pad,
    }
    payload.pop(missing)
    body = json.dumps(payload).encode("utf-8")
    out, meta = body_shrink.shrink_body(body, "/v1/messages")
    if missing == "messages":
        assert meta["trimmed"] is False
        assert meta["reason"] == "no-messages-array"
        assert out == body
    else:
        # missing model is fine, trim still runs against messages
        assert meta["trimmed"] is True
