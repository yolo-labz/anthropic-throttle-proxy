"""Falsifier for the text-only content normaliser (Z.AI 400 code 1210).

The lane accepts only ``{"type": "text"}`` blocks; a history that ran on another
provider family carries ``thinking``/``toolCall``/``toolResult`` and is refused.
These assertions pin both halves of the contract: the rewrite happens for a
chat-completions target, and NOTHING is touched for any other target (the
Anthropic lane requires those block types).
"""

import json
import sys

sys.path.insert(0, "src")

from anthropic_throttle_proxy.routing import normalize_text_content_blocks as norm  # noqa: E402

ZAI = "https://api.z.ai/api/coding/paas/v4/chat/completions"
ANTHROPIC = "https://api.anthropic.com/v1/messages"
BODY = json.dumps(
    {
        "model": "glm-5.3",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "internal reasoning"},
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
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "only thinking"}]},
            {"role": "user", "content": "plain string content"},
        ],
    }
).encode()

out = norm(BODY, ZAI)
doc = json.loads(out)

# 1. every block is text now - this is literally the 1210 condition
for m in doc["messages"]:
    assert isinstance(m["content"], str), f"content not text: {m!r}"

# 2. thinking dropped, text kept
first = doc["messages"][0]["content"]
assert "here is the plan" in first, first
assert "internal reasoning" not in first, "thinking must NOT be forwarded as text"

# 3. tool call and tool result survive as text
assert "[tool call: bash(" in first, first
second = doc["messages"][1]["content"]
assert "[tool result]" in second and "file-a" in second, second
assert "[image omitted" in second, second

# 4. a thinking-only message that is NOT last is dropped (internal reasoning
#    carries nothing the model acted on), so assert by content, not by index
contents = [m["content"] for m in doc["messages"]]
assert not any(c == "" for c in contents), "no message may reach the lane empty"
assert "plain string content" in contents, contents
assert len(contents) == 3, contents

# 4b. a LAST message emptied by normalisation must get a placeholder, not vanish
last_only = json.dumps(
    {"messages": [{"role": "user", "content": [{"type": "thinking", "thinking": "x"}]}]}
).encode()
assert json.loads(norm(last_only, ZAI))["messages"][-1]["content"] == "[no text content]"

# 5. plain string content is untouched
assert "plain string content" in contents

# 6. the Anthropic lane must NOT be rewritten - it needs those block types
assert norm(BODY, ANTHROPIC) == BODY, "normaliser touched a non-chat-completions target"

# 7. never break a forward on bad input
assert norm(b"{not json", ZAI) == b"{not json"
assert norm(b"", ZAI) == b""
assert norm(b'{"messages": "nope"}', ZAI) == b'{"messages": "nope"}'

# 8. OpenAI-style tool_calls is flattened and the key removed
tc = json.dumps(
    {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"type": "function", "function": {"name": "grep", "arguments": "{}"}}
                ],
            },
        ]
    }
).encode()
tcd = json.loads(norm(tc, ZAI))
assert "tool_calls" not in tcd["messages"][-1], tcd
assert "grep" in tcd["messages"][-1]["content"], tcd

print("  text-only normaliser: all assertions passed")
