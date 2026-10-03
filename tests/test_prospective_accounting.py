"""T001 — validated final-input token accounting + output bound (spec 245).

Failing acceptance written first; amended after the independent review of the
first slice (tool definitions counted, message shape enforced, explicit null
bound refused, non-finite JSON refused). Default-off slice only — nothing on
the hot path imports this yet, and byte/4 is an estimate, not calibrated truth.
"""

import json

import pytest

from anthropic_throttle_proxy.prospective import TokenAccounting, account_request

MODEL_DEFAULTS = {"mimo-v2.6-pro": 256}


def _body(messages, *, model="mimo-v2.6-pro", max_tokens=None, extra=None):
    obj = {"model": model, "messages": messages}
    if max_tokens is not None:
        obj["max_tokens"] = max_tokens
    if extra:
        obj.update(extra)
    return json.dumps(obj).encode()


def test_final_body_yields_input_tokens_and_explicit_bound():
    body = _body([{"role": "user", "content": "hello"}], max_tokens=128)
    acc = account_request(body, model_defaults=MODEL_DEFAULTS)
    assert isinstance(acc, TokenAccounting)
    assert acc.output_bound == 128
    assert acc.model == "mimo-v2.6-pro"
    assert acc.input_tokens > 0


def test_input_token_count_is_deterministic_and_input_side_only():
    # The estimator counts the whole INPUT side (messages, system, tools, any
    # supported text/tool fields) at full weight, excluding model/max_tokens control
    # fields, and is a pure function of the final bytes (post-shrink truth).
    a = account_request(_body([{"role": "user", "content": "x" * 400}]), MODEL_DEFAULTS)
    b = account_request(_body([{"role": "user", "content": "x" * 400}]), MODEL_DEFAULTS)
    c = account_request(_body([{"role": "user", "content": "x" * 401}]), MODEL_DEFAULTS)
    assert a is not None and b is not None and c is not None
    assert a.input_tokens == b.input_tokens
    assert c.input_tokens >= a.input_tokens


def test_tool_definitions_are_counted():
    # Large tool schemas consume input context and cannot be omitted.
    base = account_request(_body([{"role": "user", "content": "hi"}]), MODEL_DEFAULTS)
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"x": "y" * 2000}}}]
    with_tools = account_request(
        _body([{"role": "user", "content": "hi"}], extra={"tools": tools}),
        MODEL_DEFAULTS,
    )
    assert base is not None and with_tools is not None
    assert with_tools.input_tokens > base.input_tokens


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url", "image_url": {"url": "https://example.test/large.png"}},
        {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
        {"type": "document", "source": {"type": "url", "url": "https://example.test/book.pdf"}},
    ],
)
def test_media_reference_bytes_cannot_be_used_as_token_demand(part):
    assert account_request(_body([{"role": "user", "content": [part]}]), MODEL_DEFAULTS) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"modalities": ["audio"]},
        {"audio": {"voice": "alloy"}},
        {"input": "another protocol"},
        {"max_completion_tokens": 10000},
    ],
)
def test_unsupported_endpoint_or_output_format_has_no_accounting(extra):
    assert (
        account_request(_body([{"role": "user", "content": "hi"}], extra=extra), MODEL_DEFAULTS)
        is None
    )


def test_absent_max_tokens_resolves_to_model_default_bound():
    # Falsifier 5 shape: ABSENT max_tokens resolves to the MODEL DEFAULT.
    acc = account_request(_body([{"role": "user", "content": "hi"}]), MODEL_DEFAULTS)
    assert acc is not None
    assert acc.output_bound == 256


def test_explicit_null_max_tokens_refuses_but_absent_defaults():
    # Review finding: only ABSENT is defaultable — explicit null is a present,
    # invalid bound.
    body = (
        b'{"model": "mimo-v2.6-pro", "messages": [{"role": "user", "content": "hi"}], '
        b'"max_tokens": null}'
    )
    assert account_request(body, MODEL_DEFAULTS) is None


def test_unknown_model_default_refuses_instead_of_fabricating():
    # No fabricated vendor limits: an uncalibrated model has no default bound,
    # and without a bound there is no reservation.
    acc = account_request(
        _body([{"role": "user", "content": "hi"}], model="mystery"), MODEL_DEFAULTS
    )
    assert acc is None


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"",
        b"[]",
        json.dumps({}).encode(),
        json.dumps({"model": "m", "messages": []}).encode(),
        json.dumps({"model": "m", "messages": "oops"}).encode(),
        json.dumps({"model": "", "messages": [{"role": "user", "content": "x"}]}).encode(),
        json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode(),
    ],
)
def test_unparsable_or_incomplete_body_refuses(body):
    assert account_request(body, MODEL_DEFAULTS) is None


@pytest.mark.parametrize(
    "message",
    [
        {"role": [], "content": "x"},
        {"role": "user", "content": None},
        {"role": "assistant", "tool_calls": []},
        {"role": "tool", "tool_call_id": "c1"},
        {},  # review finding: [{}] is not shape-complete
        {"content": "x"},  # no role
        {"role": ""},  # empty role
        {"role": "user"},  # no payload-bearing key
        {"role": "user", "content": 7},  # non str/list/null content
        {"role": "user", "content": [7]},  # list with non str|dict part
    ],
)
def test_incomplete_message_shape_refuses(message):
    body = json.dumps({"model": "mimo-v2.6-pro", "messages": [message]}).encode()
    assert account_request(body, MODEL_DEFAULTS) is None


def test_shape_complete_message_variants_are_accepted():
    for message in (
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
        {"role": "user", "content": [{"type": "text", "text": "t"}]},
    ):
        body = json.dumps(
            {"model": "mimo-v2.6-pro", "messages": [message], "max_tokens": 1}
        ).encode()
        assert account_request(body, MODEL_DEFAULTS) is not None


@pytest.mark.parametrize("bad", [0, -5, True, 2.5, "128"])
def test_invalid_max_tokens_refuses(bad):
    obj = {"model": "mimo-v2.6-pro", "messages": [{"role": "user", "content": "x"}]}
    body = json.dumps({**obj, "max_tokens": bad}).encode()
    assert account_request(body, MODEL_DEFAULTS) is None


@pytest.mark.parametrize(
    "literal",
    [b"NaN", b"Infinity", b"-Infinity"],
)
def test_nonfinite_json_constants_refuse_anywhere(literal):
    # Review finding: Python's json accepts NaN/Infinity; the accounting must
    # refuse them anywhere in the body, not only in max_tokens.
    body = (
        b'{"model": "mimo-v2.6-pro", "messages": [{"role": "user", "content": '
        + literal
        + b'}], "max_tokens": 8}'
    )
    assert account_request(body, MODEL_DEFAULTS) is None
    body2 = (
        b'{"model": "mimo-v2.6-pro", "messages": [{"role": "user", "content": "x"}], "max_tokens": '
        + literal
        + b"}"
    )
    assert account_request(body2, MODEL_DEFAULTS) is None


def test_overflowing_json_number_cannot_be_counted_as_finite_input():
    body = (
        b'{"model":"mimo-v2.6-pro","messages":[{"role":"user","content":"x"}],"temperature":1e999}'
    )
    assert account_request(body, MODEL_DEFAULTS) is None
