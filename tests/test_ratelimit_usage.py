"""OpenAI prompt/cache conservation (contract specs/304-openai-conservation).

The frozen rule: normalized OpenAI ``input`` and ``cache_read`` are DISJOINT
and CONSERVE ``prompt_tokens`` — their sum equals the parser's prompt meaning,
with no double counting and no silent drop. Anthropic fields are already
disjoint and must not move. The chain leg proven here is
``parser -> (record_usage's sum) -> history``; the gauge leg is pinned in
``tests/test_tps_gauge.py``.

The accounting bug these pin: OpenAI reports ``prompt_tokens`` INCLUSIVE of
``prompt_tokens_details.cached_tokens``. Counting both raw double-counted the
cache — one 880K-prompt request with 873K cached tokens entered a 10 s history
bucket as ~1.75M "input" tokens (the dashboard's broken ``input 88286.8/s``
readout, measured on :8773 04/10/2026).
"""

from __future__ import annotations

from anthropic_throttle_proxy import history
from anthropic_throttle_proxy.ratelimit import _parse_sse_usage


def setup_function(_fn) -> None:
    history.reset()


def _openai_buf(prompt: int, cached: int, completion: int) -> bytes:
    return (
        b'data: {"id":"1","choices":[{"delta":{}}],'
        b'"usage":{"prompt_tokens":'
        + str(prompt).encode()
        + b',"completion_tokens":'
        + str(completion).encode()
        + b',"total_tokens":'
        + str(prompt + completion).encode()
        + b',"prompt_tokens_details":{"cached_tokens":'
        + str(cached).encode()
        + b"}}}}\n\n"
        + b"data: [DONE]\n"
    )


def test_openai_input_and_cache_read_are_disjoint_and_conserve_prompt_tokens():
    """The live-shape regression: 880K prompt, 873K cached, 95 completion."""
    usage = _parse_sse_usage(_openai_buf(880_000, 873_000, 95))
    assert usage == {
        "input": 7_000,
        "output": 95,
        "cache_read": 873_000,
        "cache_creation": 0,
    }
    # Conservation: the two prompt-side kinds sum EXACTLY to prompt_tokens.
    assert usage["input"] + usage["cache_read"] == 880_000


def test_openai_zero_cached_tokens_is_unchanged():
    usage = _parse_sse_usage(_openai_buf(14, 0, 24))
    assert usage == {"input": 14, "output": 24, "cache_read": 0, "cache_creation": 0}


def test_openai_cached_exceeding_prompt_clamps_and_still_conserves():
    """Inconsistent payloads (cached > prompt) clamp to the authoritative
    prompt_tokens: conservation holds and nothing is double counted. The
    behaviour is explicit and pinned, not silent."""
    usage = _parse_sse_usage(_openai_buf(10, 999, 5))
    assert usage == {"input": 0, "output": 5, "cache_read": 10, "cache_creation": 0}
    assert usage["input"] + usage["cache_read"] == 10


def test_anthropic_fields_stay_disjoint_and_unchanged():
    """Anthropic reports disjoint fields; this contract must not move them."""
    buf = (
        b'event: message_start\ndata: {"message":{"usage":{"input_tokens":10,'
        b'"cache_read_input_tokens":5,"cache_creation_input_tokens":2,"output_tokens":1}}}\n\n'
        b'event: message_delta\ndata: {"delta":{"stop_reason":"end_turn"},'
        b'"usage":{"output_tokens":99}}\n\n'
    )
    usage = _parse_sse_usage(buf)
    assert usage == {"input": 10, "output": 100, "cache_read": 5, "cache_creation": 2}
    assert usage["input"] + usage["cache_read"] == 15


def test_mixed_blocks_conserve_the_sum():
    """Two OpenAI blocks: conservation holds across the aggregate."""
    buf = _openai_buf(880_000, 873_000, 95) + _openai_buf(1_000, 400, 7)
    usage = _parse_sse_usage(buf)
    assert usage == {
        "input": 7_000 + 600,
        "output": 102,
        "cache_read": 873_000 + 400,
        "cache_creation": 0,
    }
    assert usage["input"] + usage["cache_read"] == 881_000


def test_record_usage_sum_conserves_through_the_chain():
    """The exact expression record_usage feeds history (proxy.py:4095, a
    RESERVED file this contract does not edit) carries conservation through:
    input + cache_read + cache_creation == prompt_tokens + cache_creation."""
    usage = _parse_sse_usage(_openai_buf(880_000, 873_000, 95))
    in_ = usage["input"] + usage["cache_read"] + usage["cache_creation"]
    assert in_ == 880_000 + usage["cache_creation"]
    history.observe_tokens(out=usage["output"], in_=in_)
    point = history.record(queued=0, inflight=0, cap=1)
    assert point.tok_in == 880_000
    assert point.tok_out == 95
