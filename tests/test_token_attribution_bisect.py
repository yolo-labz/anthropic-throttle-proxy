"""Bisect: does the real forwarding path attribute streamed usage to the request model?

This is a measurement, not a fix. It drives the production proxy app (the
``test_proxy_app`` harness: real ``handler``/``_finalize`` path, synthetic
upstream) against a realistic SSE response — ``message_start`` carrying
``usage.input_tokens``, ``message_delta`` carrying ``usage.output_tokens``,
``message_stop`` — and then reads the Prometheus exposition.

The request body carries a model name no other test uses, so a matching
``anthropic_tokens_total`` sample can only have been created by this call. If
that sample is absent while the unlabelled counter exists, the defect is in the
model label, not in usage parsing.
"""

from __future__ import annotations

from test_proxy_app import _post_and_settle, client

__all__ = ["client"]

MODEL = "claude-bisect-4-5"


def _token_lines(metrics_text: str) -> str:
    """Every anthropic_tokens_total sample line, for an honest failure message."""
    lines = [
        line for line in metrics_text.splitlines() if line.startswith("anthropic_tokens_total")
    ]
    return "\n".join(lines) or "<no anthropic_tokens_total samples at all>"


async def test_token_metric_carries_the_request_model(client) -> None:
    status, _ = await _post_and_settle(
        client,
        path_suffix="/api/v1/messages",
        data=f'{{"model":"{MODEL}"}}'.encode(),
        headers={"Authorization": "Bearer bisect-probe"},
    )
    assert status == 200

    metrics_text = await (await client.get("/metrics")).text()
    samples = _token_lines(metrics_text)

    assert f'anthropic_tokens_total{{kind="input",model="{MODEL}"}}' in metrics_text, samples
    assert f'anthropic_tokens_total{{kind="output",model="{MODEL}"}}' in metrics_text, samples
