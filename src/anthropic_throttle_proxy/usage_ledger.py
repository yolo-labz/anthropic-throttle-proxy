"""Per-request quota ledger: one JSONL row per ``/v1/messages`` attempt.

Measurement only. ``THROTTLE_USAGE_LEDGER=off|observe`` (default ``off``,
configured through ``config.USAGE_LEDGER_MODE``). Observation never changes
wire bytes, headers, limiters or admission decisions.

Why: the minute meter gives 1-point steps per account and no request-level
attribution, so a utilisation-per-token model cannot be fitted. Each row ties
the upstream ``usage`` counts to the bearer hash, model and project that sent
the request.

Row shape (never prompt content, never a token)::

    {"epoch", "bearer", "model", "project", "status", "session"?, "usage_complete"?,
     "input", "output", "cacheRead", "cacheWrite"?}

* Observed token counts can include aborted, possibly billed work. They are
  lower bounds, not useful native completions or a billing guarantee.
* ``response_id_hash`` is SHA256 of the actual upstream message ID, never a
  client identity, authorization or idempotency key. Central relay accounts
  remain unknown; only actual direct-egress auth identifies the selected account.
* ``settlement`` separates terminal+EOF from errors and incomplete evidence;
  a native normal terminal must still be joined independently by response ID.
* ``output`` is the LAST cumulative ``message_delta`` value, not a sum.
  ``usage_complete`` is False when the terminal ``message_delta`` was not in
  the captured bytes (capture is capped at 1 MiB); the counts are then lower
  bounds.

Off the hot path: ``record`` only parses the already-captured bytes and calls
``put_nowait``. A daemon thread does the file I/O, so the event loop never
waits on disk. A full queue or a write error drops the row and is counted; it
never reaches the response.

ponytail: one size cap with one ``.1`` generation, as in meter_recorder.
Upgrade path: dated rotation if more than two generations are needed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from pathlib import Path

from aiohttp import web

from . import config
from .ratelimit import _bearer_id, inflate_capture

log = logging.getLogger(__name__)

MODES = ("off", "observe")
MAX_BYTES = 64 * 1024 * 1024
_QUEUE_MAX = 4096
_BATCH = 256
CAPTURE_KEY = web.RequestKey("usage_ledger.upstream_capture", dict)


def save_capture(request, upstream, captured: bytearray, tail: bytearray, error: bool) -> None:
    """Keep existing bounded upstream buffers across aborts; never touch wire bytes."""
    try:
        url = str(getattr(upstream, "url", ""))
        central, direct = config.CENTRAL_URL.rstrip("/"), config.UPSTREAM.rstrip("/")
        via = "unknown"
        if central and url.startswith(central + "/"):
            via = "central"
        elif direct and url.startswith(direct + "/"):
            via = "direct"
        request[CAPTURE_KEY] = {
            "captured": captured,
            "tail": tail.decode("utf-8", "replace"),
            "eof": getattr(upstream.content, "at_eof", lambda: None)(),
            "error": error,
            "via": via,
            # Actual server-sent auth after routing, never caller identity headers.
            "bearer": _bearer_id(upstream.request_info.headers) if via == "direct" else None,
        }
    except Exception:  # observation must never change response/error behavior
        _stats["dropped"] += 1


def _events(text: str):
    for frame in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:")
        )
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _settlement(captured: bytes, tail: str | None) -> dict:
    raw = inflate_capture(captured)
    text = raw.decode("utf-8", "replace")
    identities = set()
    terminal, error = False, False
    if text.lstrip().startswith("{"):
        try:
            message = json.loads(text)
        except ValueError:
            message = {}
        message = message if isinstance(message, dict) else {}
        events = (
            [{"type": "message_start", "message": message}]
            if message.get("type") == "message"
            else []
        )
        terminal = bool(message.get("type") == "message" and message.get("stop_reason"))
    else:
        # Compressed tails start mid-member; never interpret them as plaintext.
        events = _events(text + ("\n\n" + tail if raw is captured and tail else ""))
    for event in events:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            identity = message.get("id") if isinstance(message, dict) else None
            if isinstance(identity, str) and 0 < len(identity) <= 256:
                identities.add(hashlib.sha256(identity.encode()).hexdigest())
        terminal = terminal or kind == "message_stop"
        error = error or kind == "error"
    return {
        "response_id_hash": next(iter(identities)) if len(identities) == 1 else None,
        "terminal_seen": terminal,
        "stream_error_seen": error,
    }


_queue: queue.Queue | None = None
_writer: threading.Thread | None = None
_start_lock = threading.Lock()
_stats = {"queued": 0, "written": 0, "dropped": 0, "write_errors": 0}


def parse_mode(raw: str | None) -> str:
    """``off`` unless the value is exactly ``observe``. Unknown values fail closed."""
    value = (raw or "").strip().lower()
    if value in ("", "off"):
        return "off"
    if value == "observe":
        return "observe"
    log.warning("THROTTLE_USAGE_LEDGER=%r is not off|observe; using off", raw)
    return "off"


def mode() -> str:
    return parse_mode(getattr(config, "USAGE_LEDGER_MODE", "off"))


def default_path() -> Path:
    return config._state_dir() / "usage-ledger.jsonl"


def _usage_from_message_obj(obj: dict) -> dict | None:
    usage = obj.get("usage") if isinstance(obj, dict) else None
    return usage if isinstance(usage, dict) else None


def parse_usage(captured: bytes, tail: str | None = None) -> tuple[dict | None, bool]:
    """Return ``(counts, complete)`` from one captured 2xx body. Never raises.

    ``counts`` has ``input``, ``output``, ``cacheRead``, ``cacheWrite`` or is
    ``None`` when no usage block was found. ``complete`` is True when a
    terminal ``message_delta`` (or a non-stream JSON body) was seen.
    """
    raw = inflate_capture(captured)
    if raw is not captured:
        # The tail is the last bytes of the COMPRESSED stream: not decodable on
        # its own, so the decoded head is the only evidence.
        tail = None
    text = raw.decode("utf-8", "replace")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError:
            return None, False
        usage = _usage_from_message_obj(obj)
        if usage is None:
            return None, False
        return _counts(usage, usage.get("output_tokens")), True

    # Pick, never sum: message_start comes from the tail when the tail has one
    # (it cannot, but a stream that fits the head then has tail == head tail),
    # and the terminal message_delta comes from the tail when present. Taking
    # the last value makes an overlap between head and tail harmless.
    start, head_delta = _sse_events(text)
    tail_start, tail_delta = _sse_events(tail or "")
    if tail_start is not None:
        start = tail_start
    delta = tail_delta if tail_delta is not None else head_delta
    if start is None:
        return None, False
    output = delta if delta is not None else start.get("output_tokens")
    return _counts(start, output), delta is not None


def _sse_events(text: str) -> tuple[dict | None, int | None]:
    """``(message_start usage, last message_delta output_tokens)`` in one SSE text."""
    start: dict | None = None
    delta: int | None = None
    for event in _events(text):
        kind = event.get("type")
        if kind == "message_start":
            usage = _usage_from_message_obj(event.get("message") or {})
            if usage is not None:
                start = usage
        elif kind == "message_delta":
            usage = _usage_from_message_obj(event)
            if usage is not None and usage.get("output_tokens") is not None:
                delta = int(usage["output_tokens"] or 0)
    return start, delta


def _counts(usage: dict, output) -> dict:
    return {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(output or 0),
        "cacheRead": int(usage.get("cache_read_input_tokens") or 0),
        "cacheWrite": int(usage.get("cache_creation_input_tokens") or 0),
    }


def record(
    *,
    bearer: str | None,
    model: str,
    project: str | None,
    status: int | None,
    captured: bytes | bytearray | None,
    tail: str | None = None,
    now: float | None = None,
    session: str | None = None,
    eof: bool | None = None,
    stream_error: bool | None = None,
    via: str = "direct",
) -> None:
    """Queue one row for this attempt. No-op when the ledger is off. Never raises."""
    if mode() == "off":
        return
    try:
        authoritative = via == "direct" and isinstance(bearer, str) and bearer not in {"", "_anon"}
        row: dict = {
            "epoch": int(time.time() if now is None else now),
            "bearer": bearer if authoritative else None,
            "account_authoritative": authoritative,
            "account_scope": "selected_upstream" if authoritative else "unknown_" + via,
            "model": model,
            "project": project or "_direct",
            "status": status,
        }
        if session:
            row["session"] = session  # hashed session key (session_affinity.derive_key)
        metadata = _settlement(bytes(captured or b""), tail)
        row.update(metadata, upstream_eof=eof)
        failed = bool(stream_error or metadata["stream_error_seen"] or (status and status >= 400))
        normal = bool(
            metadata["response_id_hash"]
            and metadata["terminal_seen"]
            and eof is True
            and stream_error is False
            and status
            and 200 <= status < 300
        )
        row["settlement"] = "error" if failed else "normal" if normal else "unknown"
        # Aborted work can still consume quota. Parsed counts are observed lower
        # bounds, never a billing guarantee or proof of a useful native terminal.
        if captured and (metadata["response_id_hash"] or (status and 200 <= status < 300)):
            counts, complete = parse_usage(bytes(captured), tail)
            if counts is not None:
                row.update(counts)
                row["usage_complete"] = complete
        _enqueue(row)
    except Exception:  # the ledger must never break a response
        _stats["dropped"] += 1
        log.debug("usage ledger record failed", exc_info=True)


def _enqueue(row: dict) -> None:
    global _queue, _writer
    with _start_lock:
        if _queue is None:
            _queue = queue.Queue(maxsize=_QUEUE_MAX)
            _writer = threading.Thread(target=_drain, name="usage-ledger", daemon=True)
            _writer.start()
    try:
        _queue.put_nowait(row)
        _stats["queued"] += 1
    except queue.Full:
        _stats["dropped"] += 1


def _drain() -> None:
    assert _queue is not None
    while True:
        batch = [_queue.get()]
        while len(batch) < _BATCH:
            try:
                batch.append(_queue.get_nowait())
            except queue.Empty:
                break
        try:
            write_rows(default_path(), batch)
            _stats["written"] += len(batch)
        except Exception:
            _stats["write_errors"] += 1
            log.warning("usage ledger write failed; %d rows dropped", len(batch), exc_info=True)


def write_rows(path: Path, rows: list[dict]) -> None:
    """Append rows as JSONL, rotating one generation when the cap is exceeded."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_BYTES:
        path.replace(path.with_name(path.name + ".1"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


def stats() -> dict:
    return dict(_stats)


__all__ = ["MODES", "mode", "parse_mode", "parse_usage", "record", "write_rows", "stats"]
