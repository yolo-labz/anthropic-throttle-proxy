"""Request-body shrinking: keep POST /v1/messages under Anthropic's 32MB cap.

Anthropic rejects any POST whose body exceeds 32 MiB with the famous
"Request too large (max 32MB)" error. Claude Code accumulates conversation
history + tool_result payloads in every turn body; a single Read of a large
file, an Agent that returns a big transcript, or a WebFetch on a noisy page
can push one turn over the cliff. The local context window is fine — the
TUI keeps everything — but the HTTP request itself is over the limit and the
turn is unrecoverable from the user's seat ("Double press esc to go back and
try with a smaller file").

This module intercepts oversize bodies at the proxy layer and replaces the
oldest ``tool_result`` content blocks with terse breadcrumb stubs until the
body fits under the cap, preserving:

* The system prompt and the tool catalog (untouched).
* The last ``BODY_SHRINK_KEEP_TURNS`` messages (so the model still sees the
  recent task context it is mid-flight on).
* The block envelope (``tool_use_id``, ``type``) so the model can correlate
  the stub with the tool call it remembers issuing.

What the stub looks like for the model::

    [throttle-proxy trimmed this tool_result: was 4321567 bytes from <tool>;
     full content preserved in client TUI; re-run the tool if you need it again]

Trade-offs documented in PR #15:

* Anthropic prompt cache: the prefix hash changes as soon as we touch any
  cached message, so the first trim of a session is a guaranteed cache miss.
  Subsequent turns re-cache normally. Cache miss is much cheaper than 32MB
  rejection (which costs the entire turn).
* Model awareness: the breadcrumb stub tells the model what was trimmed, so
  it can re-issue the tool call if it still needs the content. Without the
  stub the model would silently lose context and may hallucinate.
* Hard floor: if even trimming all but the last ``KEEP_TURNS`` cannot get
  under the cap (e.g., a single 40MB Read in the most recent turn), the body
  is forwarded as-is and Anthropic returns the 32MB error. This is by design
  — a single attachment that big must be split client-side; the proxy can't
  invent a chunking protocol the model doesn't know about.

Knobs (env vars, all optional):

* ``THROTTLE_BODY_SHRINK_CAP_BYTES`` — soft cap. Default ``29360128``
  (= 28 MiB). Anthropic's hard limit is 32 MiB but we leave 4 MiB of
  headroom for response prefill, multipart envelope overhead, and the
  inevitable serialization delta from re-encoding JSON.
* ``THROTTLE_BODY_SHRINK_KEEP_TURNS`` — minimum trailing messages to leave
  intact. Default ``4`` (last user + assistant + user + assistant).
* ``THROTTLE_BODY_SHRINK_MIN_BLOCK_BYTES`` — only trim ``tool_result``
  blocks whose serialized size is above this threshold; tiny stubs aren't
  worth touching (they yield almost no bytes back and lose context). Default
  ``2048`` (2 KiB).

Set ``THROTTLE_BODY_SHRINK_CAP_BYTES=0`` to disable the whole feature.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .config import log

# Soft cap with 4 MiB headroom under Anthropic's 32 MiB hard limit.
CAP_BYTES = int(os.environ.get("THROTTLE_BODY_SHRINK_CAP_BYTES", str(28 * 1024 * 1024)))
KEEP_TURNS = max(2, int(os.environ.get("THROTTLE_BODY_SHRINK_KEEP_TURNS", "4")))
MIN_BLOCK_BYTES = int(os.environ.get("THROTTLE_BODY_SHRINK_MIN_BLOCK_BYTES", "2048"))


def _stub_for(original_block: dict[str, Any]) -> dict[str, Any]:
    """Replace a tool_result content payload with a breadcrumb stub.

    Preserves ``tool_use_id`` and ``type`` so the model still sees a valid
    tool_result correlated with the assistant's prior tool_use. The ``content``
    field is rewritten to a single text block describing what was trimmed.
    """
    try:
        original_bytes = len(json.dumps(original_block.get("content", ""), ensure_ascii=False))
    except (TypeError, ValueError):
        original_bytes = -1
    tool_use_id = original_block.get("tool_use_id", "<unknown>")
    stub_text = (
        f"[throttle-proxy trimmed this tool_result: was {original_bytes} bytes, "
        f"tool_use_id={tool_use_id}; full content preserved in client TUI; "
        f"re-run the tool if you need the content]"
    )
    new_block = dict(original_block)
    new_block["content"] = [{"type": "text", "text": stub_text}]
    # Drop cache_control if present — a trimmed block must not anchor a cache
    # breakpoint, since the hash it computes over has changed.
    new_block.pop("cache_control", None)
    return new_block


def _iter_trimmable_blocks(messages: list[dict[str, Any]], keep_turns: int):
    """Yield (msg_index, block_index, block) for tool_result blocks eligible
    to trim, oldest first, skipping the last ``keep_turns`` messages.

    Only ``tool_result`` blocks are eligible: text blocks may be load-bearing
    instructions the model is mid-task on, and ``tool_use`` blocks (assistant
    side) describe what the assistant *did* — trimming them confuses the
    model about its own history.
    """
    cutoff = max(0, len(messages) - keep_turns)
    for mi in range(cutoff):
        msg = messages[mi]
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            yield mi, bi, block


def _block_size(block: dict[str, Any]) -> int:
    try:
        return len(json.dumps(block, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def shrink_body(body: bytes, path: str) -> tuple[bytes, dict[str, Any]]:
    """Return ``(possibly_shrunk_body, metrics)``.

    The metrics dict always carries ``original_bytes``; on a trim it also
    carries ``final_bytes``, ``blocks_trimmed``, ``bytes_saved``, and
    ``still_oversize``. A pass-through (no trim needed) returns
    ``{"trimmed": False, "original_bytes": N}``.

    Failure modes (malformed JSON, non-/v1/messages path, disabled feature)
    return the body unchanged with ``{"trimmed": False, "reason": "..."}`` so
    the caller can log without crashing the request.
    """
    original_bytes = len(body)
    if CAP_BYTES <= 0:
        return body, {"trimmed": False, "reason": "disabled", "original_bytes": original_bytes}
    if "/v1/messages" not in path:
        return body, {
            "trimmed": False,
            "reason": "non-messages-path",
            "original_bytes": original_bytes,
        }
    if original_bytes <= CAP_BYTES:
        return body, {"trimmed": False, "original_bytes": original_bytes}

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log(f"body_shrink: cannot parse body as JSON ({exc!r}); forwarding unchanged")
        return body, {
            "trimmed": False,
            "reason": "non-json",
            "original_bytes": original_bytes,
        }

    messages = data.get("messages")
    if not isinstance(messages, list):
        return body, {
            "trimmed": False,
            "reason": "no-messages-array",
            "original_bytes": original_bytes,
        }

    # Walk oldest tool_results first, replace with stub, re-serialize after
    # each replacement and stop the moment we're under the cap. Skip blocks
    # below the minimum-worthwhile threshold — replacing them costs cache
    # without buying meaningful bytes.
    blocks_trimmed = 0
    for mi, bi, block in _iter_trimmable_blocks(messages, KEEP_TURNS):
        if _block_size(block) < MIN_BLOCK_BYTES:
            continue
        messages[mi]["content"][bi] = _stub_for(block)
        blocks_trimmed += 1
        candidate = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(candidate) <= CAP_BYTES:
            return candidate, {
                "trimmed": True,
                "original_bytes": original_bytes,
                "final_bytes": len(candidate),
                "blocks_trimmed": blocks_trimmed,
                "bytes_saved": original_bytes - len(candidate),
                "still_oversize": False,
            }

    # Could not get under the cap — return whatever we managed; upstream will
    # 400 with 32MB if still too big, but at minimum the trimming is visible
    # in metrics so the operator knows to chase a client-side fix.
    final = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return final, {
        "trimmed": blocks_trimmed > 0,
        "original_bytes": original_bytes,
        "final_bytes": len(final),
        "blocks_trimmed": blocks_trimmed,
        "bytes_saved": original_bytes - len(final),
        "still_oversize": len(final) > CAP_BYTES,
    }
