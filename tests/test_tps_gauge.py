"""Tokens/s gauge plumbing: usage parsing (both vocabularies), history token
buckets, and the arc-gauge arithmetic.

The load-bearing regression these guard: the z.ai/MiMo lanes speak
OpenAI-compatible `chat/completions`, whose usage blocks nest
`prompt_tokens_details` inside `usage`. The old flat regex stopped at the
first `}` of that nested object, the fragment failed JSON decode, and the
lanes recorded ZERO tokens forever — measured live on :8766 on 29/09/2026.
"""

from __future__ import annotations

from anthropic_throttle_proxy import history
from anthropic_throttle_proxy.ratelimit import _parse_sse_usage
from anthropic_throttle_proxy.ui import signals


def setup_function(_fn) -> None:
    history.reset()


def test_parse_openai_usage_with_nested_details():
    """The z.ai/MiMo shape: nested prompt_tokens_details must survive."""
    buf = (
        b'data: {"id":"1","choices":[{"delta":{}}],'
        b'"usage":{"prompt_tokens":14,"completion_tokens":24,"total_tokens":38,'
        b'"prompt_tokens_details":{"cached_tokens":9},'
        b'"completion_tokens_details":{"reasoning_tokens":22}}}\n\n'
        b"data: [DONE]\n"
    )
    assert _parse_sse_usage(buf) == {
        "input": 14,
        "output": 24,
        "cache_read": 9,
        "cache_creation": 0,
    }


def test_parse_anthropic_usage_still_sums_start_and_delta():
    """Anthropic streams emit usage in message_start AND message_delta."""
    buf = (
        b'event: message_start\ndata: {"message":{"usage":{"input_tokens":10,'
        b'"cache_read_input_tokens":5,"cache_creation_input_tokens":2,"output_tokens":1}}}\n\n'
        b'event: message_delta\ndata: {"delta":{"stop_reason":"end_turn"},'
        b'"usage":{"output_tokens":99}}\n\n'
    )
    assert _parse_sse_usage(buf) == {
        "input": 10,
        "output": 100,
        "cache_read": 5,
        "cache_creation": 2,
    }


def test_parse_malformed_usage_fragment_is_skipped():
    buf = b'data: {"usage":{"prompt_tokens":not-a-number}}\n\n'
    assert _parse_sse_usage(buf) == {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}


def test_history_token_buckets_close_on_record():
    history.observe_tokens(out=500, in_=1500)
    history.observe(200, 0.5)
    point = history.record(queued=0, inflight=1, cap=4)
    assert point.tok_out == 500
    assert point.tok_in == 1500
    # The open bucket resets after closing.
    assert history.record(queued=0, inflight=0, cap=4).tok_out == 0


def test_gauge_value_is_trailing_window_mean():
    # 6 buckets of 100 tok/s each → value 100, peak 100, scale ≥ 100.
    for _ in range(6):
        history.observe_tokens(out=1000)  # 1000 tok / 10 s = 100 tok/s
        history.record(queued=0, inflight=2, cap=12)
    g = signals.tps_gauge()
    assert g.value == 100.0
    assert g.peak == 100.0
    assert g.seen is True
    assert 0.0 < g.frac <= 1.0
    assert g.scale >= g.peak
    # Arc geometry: 5 ticks, marker inside the viewBox.
    assert len(g.ticks) == 5
    assert all(
        0 <= x <= 220 and 0 <= y <= 120
        for tick in g.ticks
        for x, y in zip(tick[::2], tick[1::2], strict=True)
    )


def test_gauge_empty_ring_is_zero_and_unseen():
    g = signals.tps_gauge()
    assert g.value == 0.0
    assert g.seen is False
    assert g.frac == 0.0
    assert g.scale >= signals._TPS_FLOOR


def test_gauge_counts_only_output_tokens():
    """Input/cache tokens feed tok_in_now, never the output dial."""
    for _ in range(3):
        history.observe_tokens(out=100, in_=9000)
        history.record(queued=0, inflight=1, cap=4)
    g = signals.tps_gauge()
    assert g.value == 10.0  # 100 tok / 10 s
    assert g.tok_in_now == 900.0  # 9000 tok / 10 s


def test_nice_scale_uses_round_numbers():
    assert signals._nice_scale(0, 100) == 100
    assert signals._nice_scale(73, 100) == 100
    assert signals._nice_scale(140, 100) == 200
    assert signals._nice_scale(480, 100) == 500
    assert signals._nice_scale(1600, 100) == 2000
