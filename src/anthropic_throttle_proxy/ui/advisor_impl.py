"""Anthropic Haiku advisor — cheap-AI control plane.

Reads a snapshot of the proxy state and proposes knob tweaks in natural
language. Off by default (`ADVISOR_ENABLED=false`). Strictly out of the hot
path: this module is only imported when `/ui/advisor` is invoked.

Model defaults to Haiku 4.5 (cheap + fast). Override via `ADVISOR_MODEL`.
"""

from __future__ import annotations

import os

from anthropic import AsyncAnthropic

_SYSTEM = """\
You are an expert SRE advisor for `anthropic-throttle-proxy`, a reverse-
proxy in front of api.anthropic.com that enforces a single per-bearer
concurrent-stream cap across a fleet of Claude Code / opencode / codex
clients.

Knobs you can recommend (do NOT touch other knobs):
  CLAUDE_API_THROTTLE_MAX        — per-bearer concurrent ceiling (int, 1..64).
  THROTTLE_QUEUE_MODE            — one of: off, observe, fair, reactive.
  THROTTLE_MIN_DISPATCH_GAP_MS   — minimum ms between upstream dispatches
                                   (int, 0..500). Smooths burst without
                                   capping throughput.

Anthropic's actual per-bearer concurrent cap on the Max tier is ~5. Going
above that produces 429s that the proxy retries internally (visible as
high `retries`). If retries are high but in-flight is low, the bottleneck
is per-bearer concurrency, not throughput — lower MAX.

If retries are low but in-flight peaks burst-y (e.g. 6 in the same instant),
raise `THROTTLE_MIN_DISPATCH_GAP_MS` from 0 to 50–100 to smooth the
millisecond-scale dogpile WITHOUT capping concurrency.

Output format:
  - One short paragraph diagnosing the bottleneck.
  - A bullet list of specific knob changes (env var = value).
  - A one-line note on the trade-off.

Be terse. No filler. Cite the numbers from the snapshot.
"""


async def recommend(snapshot: dict) -> str:
    """Send the snapshot to Haiku and return the model's recommendation."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    model = os.environ.get("ADVISOR_MODEL", "claude-haiku-4-5-20251001")
    client = AsyncAnthropic(api_key=api_key)
    msg = await client.messages.create(
        model=model,
        max_tokens=600,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    "Here is the current proxy snapshot. Diagnose + recommend.\n\n"
                    f"```json\n{snapshot!r}\n```"
                ),
            }
        ],
    )
    # Concatenate text blocks (Haiku returns a list of content blocks).
    parts = [block.text for block in msg.content if getattr(block, "type", "") == "text"]
    return "\n".join(parts).strip() or "(advisor returned no text)"
