"""Shared UI-only HTTP GET with safe JSON parsing.

``fleet`` and ``copilot`` both need "GET a URL, return (status, body|None),
never raise on a malformed response." That shape is a single clone-prone
helper, so it lives here once. The hot path never imports this — it is
UI-dashboard-only, same as the modules that call it.
"""

from __future__ import annotations

from typing import Any

import aiohttp


async def get_json(
    url: str, headers: dict[str, str] | None = None, timeout_s: float = 6.0
) -> tuple[int, Any]:
    """One GET → ``(status, body|None)``. ``0`` = transport error/timeout.

    A 200 with a non-JSON body returns ``(200, None)`` (caller treats as a bad
    body) rather than raising ``JSONDecodeError`` into the render path.
    """
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers) as resp,
        ):
            if resp.status != 200:
                return resp.status, None
            try:
                return 200, await resp.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                return 200, None
    except (TimeoutError, aiohttp.ClientError):
        return 0, None
