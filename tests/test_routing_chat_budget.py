"""Falsifier for the chat-completions request budget.

An oversized body on this lane is refused outright (413) and the ceiling is not
published, so the proxy fits the body instead. These assertions pin the contract:
the trim happens only for this target, the anchors survive, and a body that cannot
be made to fit is passed through rather than mangled.
"""

import json

from anthropic_throttle_proxy import routing
from anthropic_throttle_proxy.routing import fit_chat_completions_body as fit

ZAI = "https://api.z.ai/api/coding/paas/v4/chat/completions"
ANTHROPIC = "https://api.anthropic.com/v1/messages"


def build(n_msgs, filler=4000):
    msgs = [{"role": "system", "content": "ANCHOR: the assignment, never dropped"}]
    for i in range(n_msgs):
        msgs.append(
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * filler}
        )
    return json.dumps({"model": "glm-5.3", "messages": msgs}).encode()


def test_other_target_untouched():
    body = build(20)
    assert fit(body, ANTHROPIC) == body, "the Anthropic lane must not be trimmed here"


def test_small_body_untouched():
    body = build(3, filler=10)
    assert fit(body, ZAI) == body


def test_malformed_untouched():
    assert fit(b"{not json", ZAI) == b"{not json"
    assert fit(b"", ZAI) == b""
    assert fit(b'{"messages": "nope"}', ZAI) == b'{"messages": "nope"}'


def test_oversize_is_trimmed_and_anchored():
    # Set the budget rather than out-writing the 2 MiB default: same code path,
    # deterministic, and it keeps the test at milliseconds.
    original = routing.CHAT_MAX_BODY_BYTES
    try:
        routing.CHAT_MAX_BODY_BYTES = 20_000
        body = build(40)
        out = fit(body, ZAI)
    finally:
        routing.CHAT_MAX_BODY_BYTES = original
    assert len(out) < len(body), "an oversized body must shrink"
    doc = json.loads(out)
    msgs = doc["messages"]
    assert "ANCHOR" in msgs[0]["content"], "the system prompt is an anchor"
    assert any("throttle-proxy trimmed" in str(m.get("content")) for m in msgs), (
        "breadcrumb expected"
    )
    assert msgs[-1]["content"].startswith("m39"), "the live tail must survive"
    assert "ANCHOR" in msgs[0]["content"] and len(msgs) < 41, "history dropped, anchors kept"


def test_kill_switch():
    body = build(40)
    original = routing.CHAT_MAX_BODY_BYTES
    try:
        routing.CHAT_MAX_BODY_BYTES = 0
        assert fit(body, ZAI) == body, "0 disables the trim"
    finally:
        routing.CHAT_MAX_BODY_BYTES = original


def test_never_returns_something_bigger():
    original = routing.CHAT_MAX_BODY_BYTES
    try:
        routing.CHAT_MAX_BODY_BYTES = 20_000
        body = build(6, filler=2000)
        out = fit(body, ZAI)
    finally:
        routing.CHAT_MAX_BODY_BYTES = original
    assert len(out) <= len(body)
