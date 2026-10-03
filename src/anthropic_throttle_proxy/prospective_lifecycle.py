"""One app runtime, initialized before producers and drained after them.

The configuration is fixed at startup. Off ignores even an invalid optional
manifest. No periodic watcher, inferred vendor allowance or hot mode switch.
"""

from __future__ import annotations

import asyncio
import io
import os

from aiohttp import web

from .prospective_admission import OwnerFaulted
from .prospective_config import acquire_custody, runtime_settings
from .prospective_runtime import RUNTIME_KEY, ProspectiveRuntime

_CUSTODY_KEY = web.AppKey("prospective_file_custody", io.IOBase)
_UNDRAINED_CUSTODY: list[io.IOBase] = []


async def prospective_context(app: web.Application):
    """Install FIRST: aiohttp tears cleanup contexts down in reverse order."""
    mode = os.environ.get("THROTTLE_PROSPECTIVE_MODE", "off")
    if mode == "off":
        yield
        return
    if mode not in {"observe", "strict"}:
        raise ValueError("THROTTLE_PROSPECTIVE_MODE must be off, observe or strict")
    if RUNTIME_KEY in app:
        raise ValueError("prospective runtime already attached")
    path = os.environ.get("THROTTLE_PROSPECTIVE_CONFIG", "")
    if not path:
        raise ValueError("enabled prospective admission requires an explicit manifest")
    settings, directory = await asyncio.to_thread(runtime_settings, path, mode)
    runtime = ProspectiveRuntime(**settings)
    # Pre-listener, nonblocking lock acquisition. No live request performs file
    # operations here; retained app custody also survives a cancelled cleanup.
    handle = acquire_custody(directory, settings["scopes"])
    app[_CUSTODY_KEY] = handle
    app[RUNTIME_KEY] = runtime
    startup_error = None
    try:
        try:
            await runtime.start()
        except BaseException as error:
            startup_error = error
            raise
        yield
    finally:
        try:
            await runtime.aclose()
        except OwnerFaulted as error:
            # PersistenceOwner guarantees this exception only AFTER its drain.
            handle.close()
            if startup_error is not None:
                raise startup_error from error
            raise
        except BaseException as error:
            # Keep custody even if the failed app is garbage-collected. No
            # in-process recovery is offered; process exit releases this lock.
            _UNDRAINED_CUSTODY.append(handle)
            if startup_error is not None:
                raise startup_error from error
            raise
        else:
            handle.close()
        # Timeout/cancellation/unknown failure deliberately keeps the app's lock
        # open. It is NOT proof the worker stopped or permission for a new owner.
