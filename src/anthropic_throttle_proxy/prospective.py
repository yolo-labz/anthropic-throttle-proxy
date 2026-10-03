"""Default-off text/tool demand estimator for spec245 T001.

No forwarding path calls this helper. It estimates the final serialized input
with UTF-8 bytes / 4, rounded up, and counts tool schemas without cache discounts.
This is not calibrated vendor usage and cannot support strict admission yet.
Unsupported media are refused: URL bytes cannot bound image/audio token cost.

A valid explicit max_tokens supplies the output bound; only absence permits an
operator-provided model default. Missing bounds, incomplete supported shapes
and non-finite JSON return None, meaning no accounting is available. T005 must
establish the estimator and scope before any enforcement is enabled.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

_BYTES_PER_TOKEN = 4  # deterministic estimate; calibration belongs to T005
_CONTROL_KEYS = frozenset({"model", "max_tokens"})


@dataclass(frozen=True)
class TokenAccounting:
    """What one dispatch would reserve: validated input estimate + output bound."""

    input_tokens: int
    output_bound: int
    model: str


def _reject_constant(name: str) -> None:
    """Refuse NaN/Infinity/-Infinity JSON literals anywhere in the body."""
    raise ValueError(f"non-finite JSON constant: {name}")


def _text_content_ok(content: object) -> bool:
    if isinstance(content, str):
        return True
    return (
        isinstance(content, list)
        and bool(content)
        and all(
            isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            for part in content
        )
    )


def _function_call_ok(call: object) -> bool:
    if not isinstance(call, dict) or call.get("type") != "function":
        return False
    function = call.get("function")
    return (
        isinstance(function, dict)
        and isinstance(function.get("name"), str)
        and isinstance(function.get("arguments"), str)
    )


def _message_shape_ok(message: dict) -> bool:
    """Support text and explicit function calls; media need separate accounting."""
    if message.get("role") not in ("user", "assistant", "system", "developer", "tool"):
        return False
    calls = message.get("tool_calls")
    if calls is not None:
        if message["role"] != "assistant" or not isinstance(calls, list) or not calls:
            return False
        if not all(_function_call_ok(call) for call in calls):
            return False
        if message.get("content") is None:
            return True
    if message["role"] == "tool" and not isinstance(message.get("tool_call_id"), str):
        return False
    return _text_content_ok(message.get("content"))


def _input_payload_bytes(obj: dict) -> int | None:
    """UTF-8 byte length of the INPUT side, or None if it cannot be serialized.

    Everything except the two control fields counts at full weight: messages,
    system and tool definitions. Unsupported media are refused before counting;
    their serialized URL/metadata cannot bound the actual provider token cost.
    """
    payload = {key: value for key, value in obj.items() if key not in _CONTROL_KEYS}
    try:
        return len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        return None


def _function_tool_ok(tool: object) -> bool:
    """Accept only explicit text function definitions, not provider-hosted tools."""
    if not isinstance(tool, dict) or tool.get("type") != "function":
        return False
    function = tool.get("function")
    return (
        isinstance(function, dict)
        and isinstance(function.get("name"), str)
        and bool(function["name"])
        and isinstance(function.get("parameters"), dict)
    )


def _request_shape_ok(obj: dict) -> bool:
    """Keep supported endpoint and text/tool shape checks together."""
    # An image URL's bytes cannot bound image tokens. Other endpoint/output
    # formats require their own validated accounting before this can accept them.
    if any(key in obj for key in ("input", "audio", "max_completion_tokens", "max_output_tokens")):
        return False
    if "modalities" in obj and obj["modalities"] != ["text"]:
        return False
    if "system" in obj and not _text_content_ok(obj["system"]):
        return False
    if "tools" in obj and not (
        isinstance(obj["tools"], list) and all(_function_tool_ok(tool) for tool in obj["tools"])
    ):
        return False
    messages = obj.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    if not all(isinstance(message, dict) and _message_shape_ok(message) for message in messages):
        return False
    return True


def account_request(body: bytes, model_defaults: Mapping[str, int]) -> TokenAccounting | None:
    """Validated final input tokens + output bound, or None to refuse reservation.

    ``model_defaults`` is operator configuration (model -> default output
    bound); its absence is a refusal, never a fabricated vendor limit. The body
    must be the FINAL bytes a dispatch site is about to send.
    """
    if not isinstance(body, bytes | bytearray) or not body:
        return None
    try:
        obj = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    if not _request_shape_ok(obj):
        return None
    model = obj.get("model")
    if not isinstance(model, str) or not model:
        return None
    nbytes = _input_payload_bytes(obj)
    if nbytes is None:
        return None
    input_tokens = max(1, -(-nbytes // _BYTES_PER_TOKEN))
    if "max_tokens" in obj:
        # PRESENT means it must be valid on its own: explicit null is not the
        # same as absence and may not fall back to the model default.
        bound = obj["max_tokens"]
    else:
        bound = model_defaults.get(model)
    if type(bound) is not int or bound <= 0:
        return None
    return TokenAccounting(input_tokens=input_tokens, output_bound=bound, model=model)
