"""Observe-only numeric pairing; no content exports, ledger access or policy.

ponytail: reuse first-1-MiB captures. Cap hits are non-comparable, never evidence
about long-response accuracy; a future bounded streaming collector is separate.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext

from .metrics import M_CALIBRATION_INPUT_RATIO, M_CALIBRATION_SAMPLES, M_CALIBRATION_TOKENS

CAPTURE_LIMIT = 1024 * 1024
_MAX_COUNT = 2**53 - 1  # exact finite Prometheus numbers, NOT a provider budget
_CHAT_FINISH = frozenset({"stop", "length", "tool_calls", "function_call", "content_filter"})
_MESSAGE_FINISH = frozenset({"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn"})


def _integer(value):
    if type(value) is not int or not 0 <= value <= _MAX_COUNT:
        raise ValueError("invalid count")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _json(raw):
    obj = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(obj, dict):
        raise ValueError("invalid envelope")
    return obj


def _frames(body, content_type):
    if content_type == "application/json":
        yield _json(body)
        return
    if content_type != "text/event-stream":
        raise ValueError("unsupported envelope")
    text = body.decode("utf-8").replace("\r\n", "\n")
    if text and not text.endswith("\n\n"):
        raise ValueError("incomplete SSE frame")
    for frame in text.split("\n\n"):
        data = "\n".join(
            line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:")
        )
        if data and data != "[DONE]":
            yield _json(data)


class _Usage:
    def __init__(self, protocol):
        self.protocol = protocol
        self.counts = {}
        self.finished = False
        self.stopped = False
        self.malformed = False

    def _update(self, usage, fields):
        if not isinstance(usage, dict):
            raise ValueError("invalid usage")
        for name in fields:
            if name not in usage:
                continue
            value = _integer(usage[name])
            if value < self.counts.get(name, 0):
                raise ValueError("decreasing cumulative usage")
            self.counts[name] = value

    def _chat_usage(self, usage):
        if any(name in usage for name in ("input_tokens", "output_tokens")):
            raise ValueError("mixed usage vocabulary")
        self._update(usage, ("prompt_tokens", "completion_tokens"))
        if "total_tokens" in usage and _integer(usage["total_tokens"]) != sum(self.counts.values()):
            raise ValueError("inconsistent total")
        self._chat_cache(usage)
        for detail, subset, total in (
            ("prompt_tokens_details", "cached_tokens", "prompt_tokens"),
            ("completion_tokens_details", "reasoning_tokens", "completion_tokens"),
        ):
            if detail not in usage:
                continue
            details = usage[detail]
            if not isinstance(details, dict):
                raise ValueError("invalid token details")
            if subset in details and _integer(details[subset]) > self.counts.get(total, -1):
                raise ValueError("subset exceeds total")

    def _chat_cache(self, usage):
        hit, miss = "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"
        if hit not in usage and miss not in usage:
            return
        if hit not in usage or miss not in usage:
            raise ValueError("incomplete cache partition")
        cached, fresh = _integer(usage[hit]), _integer(usage[miss])
        if cached + fresh != self.counts.get("prompt_tokens"):
            raise ValueError("cache partition does not conserve input")
        detail = usage.get("prompt_tokens_details")
        if isinstance(detail, dict) and detail.get("cached_tokens", cached) != cached:
            raise ValueError("conflicting cache counts")

    def chat(self, obj):
        choices = obj.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("only single-choice calibration supported")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise ValueError("invalid choice")
            reason = choice.get("finish_reason")
            if isinstance(reason, str) and reason in _CHAT_FINISH:
                self.finished = True
        usage = obj.get("usage")
        if usage is not None:
            self._chat_usage(usage)

    def messages(self, obj):
        kind = obj.get("type")
        if kind == "message_start":
            obj = obj.get("message", {})
        if not isinstance(obj, dict):
            raise ValueError("invalid message")
        delta = obj.get("delta", obj)
        if isinstance(delta, dict):
            reason = delta.get("stop_reason")
            if isinstance(reason, str) and reason in _MESSAGE_FINISH:
                self.finished = True
        if kind in {"message", "message_stop"}:
            self.stopped = True
        usage = obj.get("usage")
        if usage is not None:
            if not isinstance(usage, dict) or any(
                name in usage for name in ("prompt_tokens", "completion_tokens")
            ):
                raise ValueError("invalid message usage")
            self._update(
                usage,
                (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                ),
            )

    def consume(self, obj):
        try:
            if "error" in obj or obj.get("type") == "error":
                raise ValueError("error envelope")
            if self.protocol == "chat":
                self.chat(obj)
            else:
                self.messages(obj)
        except (TypeError, ValueError):
            self.malformed = True

    def result(self):
        fields = (
            ("prompt_tokens", "completion_tokens")
            if self.protocol == "chat"
            else (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )
        if self.malformed:
            return "malformed_usage", None
        if any(name not in self.counts for name in fields):
            return "missing_usage", None
        input_count = self.counts[fields[0]]
        if self.protocol == "messages":
            input_count += self.counts[fields[2]] + self.counts[fields[3]]
        return "comparable", (_integer(input_count), self.counts[fields[1]])


def _parse(body, content_type, path):
    protocol = {"/v1/chat/completions": "chat", "/v1/messages": "messages"}.get(path)
    if protocol is None:
        return "unsupported_protocol", "eof_unverified", None
    usage = _Usage(protocol)
    try:
        for frame in _frames(body, content_type):
            usage.consume(frame)
        outcome, counts = usage.result()
    except (ValueError, TypeError, RecursionError):
        return "malformed_usage", "eof_unverified", None
    finished = usage.finished and (protocol == "chat" or usage.stopped)
    terminal = "eof_with_finish" if finished else "eof_without_finish"
    if not finished and outcome == "comparable":
        return "missing_finish", terminal, None
    return outcome, terminal, counts


class CalibrationAttempt:
    """One observe permit's numeric context; completion cannot affect forwarding."""

    def __init__(self, label, cost, single_scope):
        self.label = label
        self.cost = cost
        self.single_scope = single_scope
        self.sent = False
        self._done = False
        self._outcome = "transport_error"
        self._terminal = "transport_error"
        self._counts = None

    def __enter__(self):
        return self

    def transport_error(self):
        self._outcome, self._terminal, self._counts = "transport_error", "transport_error", None

    def response(self, captured, status, headers, path, error=None):
        if error is not None:
            self.transport_error()
            return
        # Missing capture is different from a captured empty EOF. Never decode
        # compressed captures with an unbounded decompressor on the hot path.
        if captured is None:
            self._outcome, self._terminal = "missing_capture", "eof_unverified"
        elif len(captured) >= CAPTURE_LIMIT:
            self._outcome, self._terminal = "capture_cap", "eof_unverified"
        elif headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            self._outcome, self._terminal = "encoded", "eof_unverified"
        else:
            content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            self._outcome, self._terminal, self._counts = _parse(captured, content_type, path)
        if not 200 <= status < 300:
            self._outcome, self._counts = "upstream_error", None

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._done or not self.sent:
            return
        self._done = True
        if exc_type is not None:
            self._terminal = (
                "cancelled" if issubclass(exc_type, asyncio.CancelledError) else "transport_error"
            )
            self._outcome, self._counts = self._terminal, None
        if not self.single_scope:
            self._outcome, self._counts = "ambiguous_scope", None
        try:
            self._record()
        except Exception:
            # Instrumentation must not turn a completed response into a retry
            # or mask the original transport/cancellation exception. No payload log.
            return

    def _record(self):
        M_CALIBRATION_SAMPLES.labels(
            scope=self.label, outcome=self._outcome, terminal=self._terminal
        ).inc()
        if self._outcome != "comparable":
            return
        reported_input, reported_output = self._counts
        for kind, value in (
            ("estimated_input", self.cost.input_tokens),
            ("output_bound", self.cost.output_bound),
            ("reported_input", reported_input),
            ("reported_output", reported_output),
        ):
            M_CALIBRATION_TOKENS.labels(scope=self.label, kind=kind).inc(value)
        M_CALIBRATION_INPUT_RATIO.labels(scope=self.label).observe(
            reported_input / self.cost.input_tokens
        )


class _OffCalibration:
    __slots__ = ()

    def transport_error(self):
        pass  # preserve forwarding policy without creating samples

    def response(self, *_args, **_kwargs):
        pass  # no capture parsing, labels, sample state or metric access when off


_OFF_CONTEXT = nullcontext(_OffCalibration())


def calibration_attempt(permit):
    return getattr(permit, "calibration", None) or _OFF_CONTEXT
