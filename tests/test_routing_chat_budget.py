"""Falsifiers for the chat-completions request budget.

An oversized body on this lane is refused outright (413) and the ceiling is
never published, so the proxy fits the body instead of learning the number the
hard way. These assertions pin the contract: the trim happens only for this
endpoint, the anchors and the live tail survive, and what we rewrite is actually
inside the budget we rewrote it for.
"""

import json

import pytest

from anthropic_throttle_proxy import routing
from anthropic_throttle_proxy.routing import fit_chat_completions_body as fit

ZAI = "https://api.z.ai/api/coding/paas/v4/chat/completions"
ANTHROPIC = "https://api.anthropic.com/v1/messages"

# A budget must clear the protected tail (CHAT_KEEP_TAIL turns) to be fittable at
# all: 6 turns of ~4000 bytes is ~24 KB, so a smaller budget can only pass through.
BUDGET = 40_000


def build(n_msgs, filler=4000):
    """A transcript with a system ANCHOR first and `n_msgs` numbered turns."""
    msgs = [{"role": "system", "content": "ANCHOR: the assignment, never dropped"}]
    for i in range(n_msgs):
        msgs.append(
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * filler}
        )
    return json.dumps({"model": "glm-5.3", "messages": msgs}).encode()


def test_other_target_untouched():
    body = build(20)
    assert fit(body, ANTHROPIC) == body, "the Anthropic lane must not be trimmed here"


@pytest.mark.parametrize(
    "target",
    [
        "http://api.z.ai/api/coding/paas/v4/chat/completions",  # not TLS
        "https://api.z.ai:8443/api/coding/paas/v4/chat/completions",  # odd port
        "https://api.z.ai.evil.test/api/coding/paas/v4/chat/completions",  # lookalike host
        "https://api.z.ai/api/anthropic/v1/messages",  # sibling protocol, same host
        "https://api.z.ai/api/coding/paas/v4/chat/completions/",  # trailing slash
        "not a url at all",
    ],
)
def test_only_the_exact_endpoint_is_trimmed(target):
    body = build(20)
    assert fit(body, target) == body


def test_small_body_untouched():
    body = build(3, filler=10)
    assert fit(body, ZAI) == body


def test_malformed_untouched():
    assert fit(b"{not json", ZAI) == b"{not json"
    assert fit(b"", ZAI) == b""
    assert fit(b'{"messages": "nope"}', ZAI) == b'{"messages": "nope"}'
    assert fit(b'{"messages": []}', ZAI) == b'{"messages": []}'


def test_oversize_is_trimmed_and_anchored(monkeypatch):
    # Set the budget rather than out-writing the 2 MiB default: same code path,
    # deterministic, and it keeps the test at milliseconds.
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    body = build(40)
    out = fit(body, ZAI)
    assert len(out) < len(body), "an oversized body must shrink"
    assert len(out) <= BUDGET, "what we rewrite must fit the budget we rewrote it for"
    msgs = json.loads(out)["messages"]
    assert "ANCHOR" in msgs[0]["content"], "the system prompt is an anchor"
    assert any("throttle-proxy trimmed" in str(m.get("content")) for m in msgs), "breadcrumb"
    assert msgs[-1]["content"].startswith("m39"), "the live tail must survive"
    assert len(msgs) < 41, "history dropped, anchors kept"


def test_kill_switch(monkeypatch):
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", 0)
    body = build(40)
    assert fit(body, ZAI) == body, "0 disables the trim"


def test_unfittable_body_is_passed_through_untouched(monkeypatch):
    """Dropping every head turn still will not fit: refuse as before, do not mangle.

    The budget is a GUESS at an unpublished ceiling. When even the protected tail
    is past it, the guess is no longer evidence that this request is too big — so
    the provider decides, on the request the client actually sent.
    """
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", 500)
    body = build(40)
    assert fit(body, ZAI) == body


def test_keep_tail_is_honoured(monkeypatch):
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    monkeypatch.setattr(routing, "CHAT_KEEP_TAIL", 2)
    msgs = json.loads(fit(build(40), ZAI))["messages"]
    assert msgs[-2]["content"].startswith("m38"), "the configured tail is intact"
    assert msgs[-1]["content"].startswith("m39")


def test_transcript_without_a_system_turn_is_still_true_to_the_tail(monkeypatch):
    """No anchor to protect: drop from the front, never the tail."""
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    raw = json.dumps(
        {"messages": [{"role": "user", "content": f"m{i} " + "x" * 4000} for i in range(40)]}
    ).encode()
    out = fit(raw, ZAI)
    assert out != raw
    msgs = json.loads(out)["messages"]
    assert msgs[-1]["content"].startswith("m39")
    assert msgs[0]["role"] == "system", "the breadcrumb is inserted as the first turn"


@pytest.mark.parametrize("budget", [500, 5_000, 24_000, BUDGET, 200_000])
def test_rewritten_body_never_exceeds_its_budget(monkeypatch, budget):
    """The property, over budgets that cannot fit (passthrough) and budgets that trim."""
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", budget)
    body = build(40)
    out = fit(body, ZAI)
    if out == body:
        return  # passthrough is always allowed — the proxy is not the reason a forward dies
    assert len(out) <= budget
    doc = json.loads(out)
    assert "ANCHOR" in doc["messages"][0]["content"]
    assert doc["messages"][-1]["content"].startswith("m39")


def test_never_returns_something_bigger(monkeypatch):
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    body = build(40)
    assert len(fit(body, ZAI)) < len(body)


def test_untouched_keys_survive(monkeypatch):
    """Only `messages` is rewritten: tools, stream and model reach upstream intact."""
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    doc = json.loads(build(40))
    doc["tools"] = [{"type": "function", "function": {"name": "read_file"}}]
    doc["stream"] = True
    out = json.loads(fit(json.dumps(doc).encode(), ZAI))
    assert out["tools"] == doc["tools"]
    assert out["stream"] is True
    assert out["model"] == "glm-5.3"
