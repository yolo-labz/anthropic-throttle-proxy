"""Falsifiers for the chat-completions request budget.

An oversized body on this lane is refused outright (413) and the ceiling is
never published, so the proxy fits the body instead of learning the number the
hard way. These assertions pin the contract: the trim happens only for this
endpoint, the anchors and the live tail survive, the cut never separates a tool
call from its answer, and what we rewrite is actually inside the budget we
rewrote it for.
"""

import json

import pytest

from anthropic_throttle_proxy import routing

ZAI = "https://api.z.ai/api/coding/paas/v4/chat/completions"
ANTHROPIC = "https://api.anthropic.com/v1/messages"

# A budget must clear the protected tail (CHAT_KEEP_TAIL turns) to be fittable at
# all: 6 turns of ~4000 bytes is ~24 KB, so a smaller budget can only pass through.
BUDGET = 40_000


def fit(raw, target=ZAI):
    """The body only, for the assertions that do not care about the reason."""
    return routing.fit_chat_completions_body(raw, target)[0]


def meta_of(raw, target=ZAI):
    return routing.fit_chat_completions_body(raw, target)[1]


def build(n_msgs, filler=4000):
    """A transcript with a system ANCHOR first and `n_msgs` numbered turns."""
    msgs = [{"role": "system", "content": "ANCHOR: the assignment, never dropped"}]
    for i in range(n_msgs):
        msgs.append(
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * filler}
        )
    return json.dumps({"model": "glm-5.3", "messages": msgs}).encode()


def tool_transcript(seed=7, n_pairs=40):
    """Native OpenAI shape: an assistant tool_calls turn then its role:"tool" answer.

    Sizes are uneven and aperiodic on purpose - uniform pairs hide a cut that lands
    mid-pair, because dropping an even number of equal turns keeps every pairing
    intact. Deterministic (no RNG) so the falsifier is reproducible.
    """
    msgs = [{"role": "system", "content": "ANCHOR: the assignment, never dropped"}]
    for i in range(n_pairs):
        asked = 40 + (i * i * 37 + seed * 97) % 1300
        answered = 40 + (i * 53 + seed * 211) % 1300
        msgs.append(
            {
                "role": "assistant",
                "content": "x" * asked,
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * answered})
    return json.dumps({"model": "glm-5.3", "messages": msgs}).encode()


def orphans(messages):
    """Tool answers whose originating tool_call is gone - an invalid transcript.

    Independent of the implementation's own helper: it reads only roles and ids.
    """
    called = {
        call.get("id")
        for message in messages
        if isinstance(message.get("tool_calls"), list)
        for call in message["tool_calls"]
        if isinstance(call, dict)
    }
    return [
        message
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id") not in called
    ]


# --- the endpoint guard -------------------------------------------------------


def test_other_target_untouched():
    body = build(20)
    assert fit(body, ANTHROPIC) == body, "the Anthropic lane must not be trimmed here"
    assert meta_of(body, ANTHROPIC)["reason"] == "other-endpoint"


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


def test_a_malformed_port_is_not_an_endpoint_and_never_raises():
    """`.port` raises on a bad port; the old inline check swallowed that."""
    body = build(20)
    assert fit(body, "https://api.z.ai:abc/api/coding/paas/v4/chat/completions") == body
    assert fit(body, "https://api.z.ai:99999/api/coding/paas/v4/chat/completions") == body


# --- pass-through contracts ---------------------------------------------------


def test_small_body_untouched():
    body = build(3, filler=10)
    assert fit(body, ZAI) == body
    assert meta_of(body, ZAI)["reason"] == "under-budget"


def test_kill_switch(monkeypatch):
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", 0)
    body = build(40)
    assert fit(body, ZAI) == body, "0 disables the trim"
    assert meta_of(body, ZAI)["reason"] == "disabled"


def test_malformed_untouched(monkeypatch):
    assert fit(b"{not json", ZAI) == b"{not json"
    # A short malformed body exits on the budget before it is ever parsed, and part
    # of the contract is that NOTHING we do can make the proxy the reason a forward
    # dies - including a body we cannot read.
    assert meta_of(b"{not json", ZAI)["reason"] == "under-budget"
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", 8)
    assert fit(b"{not json", ZAI) == b"{not json"
    assert meta_of(b"{not json", ZAI)["reason"] == "non-json"
    assert fit(b"", ZAI) == b""
    assert fit(b'{"messages": "nope"}', ZAI) == b'{"messages": "nope"}'
    assert fit(b'{"messages": []}', ZAI) == b'{"messages": []}'


def test_unfittable_body_is_passed_through_untouched(monkeypatch):
    """Dropping every head turn still will not fit: refuse as before, do not mangle.

    The budget is a GUESS at an unpublished ceiling. When even the protected tail
    is past it, the guess is no longer evidence that this request is too big — so
    the provider decides, on the request the client actually sent.
    """
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", 500)
    body = build(40)
    assert fit(body, ZAI) == body
    assert meta_of(body, ZAI)["reason"] == "unfittable", "the give-up must be nameable"


def test_a_pretty_printed_body_is_not_trimmed_to_win_an_argument_about_whitespace(monkeypatch):
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", 20_000)
    compact = json.loads(build(10, filler=100))
    wide = json.dumps(compact, indent=400).encode()
    assert len(wide) > 20_000 > len(json.dumps(compact).encode())
    assert fit(wide, ZAI) == wide
    assert meta_of(wide, ZAI)["reason"] == "fits-when-compact"


# --- the trim ----------------------------------------------------------------


def test_oversize_is_trimmed_and_anchored(monkeypatch):
    # Set the budget rather than out-writing the shipped default: same code path,
    # deterministic, and it keeps the test at milliseconds.
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    body = build(40)
    out, meta = routing.fit_chat_completions_body(body, ZAI)
    assert len(out) < len(body), "an oversized body must shrink"
    assert len(out) <= BUDGET, "what we rewrite must fit the budget we rewrote it for"
    assert meta["fitted"] is True and meta["turns_dropped"] > 0
    msgs = json.loads(out)["messages"]
    assert "ANCHOR" in msgs[0]["content"], "the system prompt is an anchor"
    assert any("throttle-proxy trimmed" in str(m.get("content")) for m in msgs), "breadcrumb"
    assert msgs[-1]["content"].startswith("m39"), "the live tail must survive"
    assert len(msgs) < 41, "history dropped, anchors kept"


def test_keep_tail_is_honoured(monkeypatch):
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    monkeypatch.setattr(routing, "CHAT_KEEP_TAIL", 2)
    msgs = json.loads(fit(build(40), ZAI))["messages"]
    assert msgs[-2]["content"].startswith("m38"), "the configured tail is intact"
    assert msgs[-1]["content"].startswith("m39")


def test_a_body_with_only_just_enough_turns_still_trims(monkeypatch):
    """`KEEP_TAIL + 1` messages are droppable when there is no anchor turn."""
    monkeypatch.setattr(routing, "CHAT_KEEP_TAIL", 2)
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", 20_000)
    raw = json.dumps(
        {"messages": [{"role": "user", "content": f"m{i} " + "x" * 8000} for i in range(3)]}
    ).encode()
    assert len(raw) > 20_000, "fixture must be over budget"
    out, meta = routing.fit_chat_completions_body(raw, ZAI)
    assert meta["fitted"] is True, "the single droppable turn must not be skipped"
    assert len(json.loads(out)["messages"]) == 3  # breadcrumb + 2 kept


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


def test_never_returns_something_bigger(monkeypatch):
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    body = build(40)
    assert len(fit(body, ZAI)) < len(body)


# --- tool linkage: a cut must not separate a call from its answer --------------


def test_cut_never_orphans_a_tool_answer():
    """The gate's blocker: an uneven cut used to land between call and answer.

    Random uneven pairs, many seeds, because the landing index is the first one
    whose cumulative size fits and therefore moves with the sizes.
    """
    routing.CHAT_MAX_BODY_BYTES = 20_000
    try:
        for seed in range(30):
            body = tool_transcript(seed=seed)
            assert orphans(json.loads(body)["messages"]) == [], "fixture must be valid"
            out, meta = routing.fit_chat_completions_body(body, ZAI)
            if not meta["fitted"]:
                assert meta["reason"] == "unfittable"
                continue
            kept = json.loads(out)["messages"]
            assert orphans(kept) == [], f"seed={seed}: orphaned tool answer reached the wire"
            assert kept[-1]["role"] == "tool", "the live turn still has its answer"
    finally:
        routing.CHAT_MAX_BODY_BYTES = 1_800_000


def test_cut_never_orphans_an_anthropic_tool_result_block(monkeypatch):
    """Same rule for the block shape, reachable when the normalizer bails out."""
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", 20_000)
    msgs = [{"role": "system", "content": "ANCHOR"}]
    for i in range(40):
        block = {"type": "tool_result", "tool_use_id": f"c{i}", "content": "z" * 400}
        msgs.append({"role": "user", "content": [block]})
        msgs.append({"role": "assistant", "content": "x" * 400})
    out, meta = routing.fit_chat_completions_body(json.dumps({"messages": msgs}).encode(), ZAI)
    if meta["fitted"]:
        first_live = json.loads(out)["messages"][2]
        assert not routing._answers_a_dropped_call(first_live), "orphaned tool_result kept"


def test_unfittable_when_every_cut_would_orphan_a_tool_answer(monkeypatch):
    """When the only cuts left keep an orphan, refuse as before rather than send it."""
    monkeypatch.setattr(routing, "CHAT_KEEP_TAIL", 2)
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", 2_000)
    msgs = [{"role": "system", "content": "ANCHOR"}]
    msgs += [{"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 400} for i in range(6)]
    # The protected tail starts with another answer, so every reachable cut keeps one.
    msgs += [
        {"role": "tool", "tool_call_id": "t0", "content": "y" * 400},
        {"role": "assistant", "content": "x"},
    ]
    body = json.dumps({"messages": msgs}).encode()
    out, meta = routing.fit_chat_completions_body(body, ZAI)
    assert out == body
    assert meta["reason"] == "unfittable"


# --- sizing and search invariants --------------------------------------------


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


def test_the_cut_is_tight(monkeypatch):
    """One turn fewer must NOT fit, or we dropped history we did not have to.

    Catches a sizing arithmetic that over-estimates (it would drop too much); the
    under-estimating direction is caught by every test that expects a fit, because
    the emit guard turns an over-budget rewrite into a passthrough.
    """
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    body = build(40)
    out, meta = routing.fit_chat_completions_body(body, ZAI)
    dropped = meta["turns_dropped"]
    assert dropped >= 1
    doc = json.loads(out)
    kept_after_anchor = doc["messages"][1:]
    # Put the last dropped turn back in, in the same position it was cut from.
    original = json.loads(body)
    reinserted = dict(doc)
    reinserted["messages"] = (
        doc["messages"][:1] + [original["messages"][dropped]] + kept_after_anchor
    )
    assert len(json.dumps(reinserted).encode()) > BUDGET


def test_sizing_serializes_roughly_once_per_turn(monkeypatch):
    """Pin the CPU shape: one small dump per turn, NOT a full body per probe.

    A binary search that re-serializes the whole body each step would burn ~11x
    the body in JSON work on the event loop for a 3 MB request.
    """
    real_dumps = json.dumps
    serialized = {"bytes": 0}

    def counting(*args, **kwargs):
        out = real_dumps(*args, **kwargs)
        serialized["bytes"] += len(out)
        return out

    monkeypatch.setattr(routing.json, "dumps", counting)
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    body = build(40)
    out, meta = routing.fit_chat_completions_body(body, ZAI)
    assert meta["fitted"] is True
    assert serialized["bytes"] < 3 * len(body), (
        f"serialized {serialized['bytes']} bytes for a {len(body)}-byte body"
    )
