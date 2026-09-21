"""Synthetic regressions for PR #236's Z.AI content-only normalization."""

import json

import pytest

from anthropic_throttle_proxy.routing import normalize_text_content_blocks as norm

ZAI = "https://api.z.ai/api/coding/paas/v4/chat/completions"
THINKING = {"type": "thinking", "thinking": "hidden"}


def _raw(messages):
    return json.dumps({"model": "glm-5.3", "messages": messages}).encode()


def _normalized(messages):
    return json.loads(norm(_raw(messages), ZAI))["messages"]


def test_internal_blocks_remain_useful_text():
    messages = [
        {
            "role": "assistant",
            "content": [
                THINKING,
                {"type": "text", "text": "here is the plan"},
                {"type": "toolCall", "name": "bash", "arguments": {"command": "ls"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "toolResult", "content": [{"type": "text", "text": "file-a\nfile-b"}]},
                {"type": "image", "source": {"data": "AAA"}},
            ],
        },
        {"role": "assistant", "content": [THINKING]},
        {"role": "user", "content": "plain string content"},
    ]
    out = _normalized(messages)
    assert len(out) == 3
    assert all(isinstance(m["content"], str) for m in out)
    assert "here is the plan" in out[0]["content"]
    assert "hidden" not in out[0]["content"]
    assert '[tool call: bash({"command": "ls"})]' in out[0]["content"]
    assert "[tool result] file-a\nfile-b" in out[1]["content"]
    assert "[image omitted" in out[1]["content"]
    assert out[-1] == messages[-1]


@pytest.mark.parametrize("prefix", [[], [{"role": "user", "content": "KEEP THIS REQUEST"}]])
@pytest.mark.parametrize("trailing_count", [1, 2])
def test_final_emptied_turn_never_deletes_preceding_turn(prefix, trailing_count):
    out = _normalized(prefix + [{"role": "assistant", "content": [THINKING]}] * trailing_count)
    assert out == prefix + [{"role": "assistant", "content": "[no text content]"}]


@pytest.mark.parametrize("content", [None, "", [{"type": "text", "text": "calling"}], [THINKING]])
@pytest.mark.parametrize("result", ["", "actual result"])
def test_native_tool_protocol_survives(content, result):
    calls = [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]
    messages = [
        {"role": "assistant", "content": content, "tool_calls": calls},
        {"role": "tool", "tool_call_id": "call_1", "content": result},
        {"role": "user", "content": "continue"},
    ]
    out = _normalized(messages)
    assert len(out) == 3
    assert out[0]["tool_calls"] == calls
    assert out[0]["role"] == "assistant"
    assert out[1:] == messages[1:]


def test_native_call_without_content_survives():
    raw = _raw([{"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function"}]}])
    assert norm(raw, ZAI) == raw


@pytest.mark.parametrize(
    "block",
    [
        {"type": "text", "text": 123},
        {"type": "text", "text": None},
        {"type": "text"},
        {"type": "toolResult", "content": [{"type": "text", "text": 123}]},
        {"type": "toolResult", "content": [{"type": "tool_result", "content": [False]}]},
        {"type": "tool_result", "content": {"unexpected": "object"}},
        {"type": "toolCall", "name": ["bad"], "arguments": {}},
        {"type": "function", "function": ["bad"]},
        {"type": "future_block", "payload": "DO NOT DROP"},
        123,
        None,
        [],
    ],
)
def test_malformed_nested_blocks_pass_through_transactionally(block):
    raw = _raw(
        [
            {"role": "user", "content": [THINKING, {"type": "text", "text": "keep"}]},
            {"role": "user", "content": [block]},
        ]
    )
    assert norm(raw, ZAI) == raw


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{not json",
        b"\xff",
        b"null",
        b"[]",
        b"{}",
        b'{"messages": "nope"}',
        b'{"messages": {}}',
        b'{"messages": []}',
        b'{"messages": [123]}',
        b'{"messages": [{"content": 123}]}',
        b'{"messages": [null]}',
    ],
)
def test_malformed_or_absent_messages_pass_through(raw):
    assert norm(raw, ZAI) == raw


@pytest.mark.parametrize("content", ["", [], [{"type": "text", "text": ""}]])
def test_original_empty_content_does_not_delete_turn(content):
    messages = [{"role": "user", "content": content}, {"role": "user", "content": "keep"}]
    out = _normalized(messages)
    assert len(out) == 2
    assert out[-1] == messages[-1]
    assert out[0]["content"] in (content, "")


@pytest.mark.parametrize("args", ["", False, 0, None, {}, [], "x" * 401 + "TAIL"])
@pytest.mark.parametrize("kind", ["toolCall", "tool_use", "function_call", "function"])
def test_arguments_preserved_without_truthiness_or_truncation(args, kind):
    block = {"type": kind, "name": "read", "arguments": args}
    expected = args if isinstance(args, str) else json.dumps(args)
    assert _normalized([{"role": "assistant", "content": [block]}])[0]["content"] == (
        f"[tool call: read({expected})]"
    )


def test_nested_function_arguments_and_anthropic_input():
    for block in [
        {"type": "function", "function": {"name": "read", "arguments": False}},
        {"type": "tool_use", "name": "read", "input": False},
    ]:
        assert _normalized([{"content": [block]}])[0]["content"] == "[tool call: read(false)]"


@pytest.mark.parametrize("nested", [False, True])
def test_tool_result_not_truncated(nested):
    text = "x" * 4001 + "IMPORTANT TAIL"
    content = [{"type": "text", "text": text}] if nested else text
    block = {"type": "tool_result", "content": content}
    assert _normalized([{"role": "user", "content": [block]}])[0]["content"] == (
        f"[tool result] {text}"
    )


@pytest.mark.parametrize(
    "target",
    [
        "https://api.openai.com/v1/chat/completions",
        "https://api.anthropic.com/v1/messages",
        "https://api.z.ai/api/anthropic/v1/messages",
        "http://127.0.0.1:8766/api/coding/paas/v4/chat/completions",
        "https://api.z.ai.example/api/coding/paas/v4/chat/completions",
        "https://example.test/api/coding/paas/v4/chat/completions",
        "https://api.z.ai/api/coding/paas/v4/chat/completions/extra",
        "https://api.z.ai/other?next=/chat/completions",
        "https://api.z.ai:444/api/coding/paas/v4/chat/completions",
        "http://api.z.ai/api/coding/paas/v4/chat/completions",
        "http://[invalid/chat/completions",
        "",
        "/chat/completions",
    ],
)
def test_unrelated_targets_are_byte_identical(target):
    raw = _raw([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}])
    assert norm(raw, target) == raw


@pytest.mark.parametrize(
    "target", [ZAI, ZAI + "?trace=synthetic", ZAI.replace("api.z.ai", "API.Z.AI:443")]
)
def test_exact_zai_endpoint_and_query_normalize(target):
    raw = _raw([{"role": "assistant", "content": [THINKING]}])
    assert json.loads(norm(raw, target))["messages"][0]["content"] == "[no text content]"


def test_normalization_is_idempotent():
    raw = _raw([{"role": "assistant", "content": [THINKING, {"type": "text", "text": "hi"}]}])
    assert norm(norm(raw, ZAI), ZAI) == norm(raw, ZAI)
