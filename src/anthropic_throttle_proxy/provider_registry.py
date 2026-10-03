"""THRTL-19: bounded, removal-only registry for existing ingress lanes.

Membership cannot create a lane, a model pin, privacy permission or capacity.
The caller publishes a validated snapshot atomically and owns last-good state.
"""

from __future__ import annotations

import json
import os
import stat

MAX_BYTES = 16 * 1024


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("registry-duplicate-field")
        result[name] = value
    return result


def load(path: str, configured: frozenset[str]) -> frozenset[str]:
    """Read one complete policy or raise; errors never include file contents."""
    # Check the opened descriptor, not a racy path stat. NONBLOCK means a
    # mistaken FIFO cannot strand the existing health-poll thread forever.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("registry-not-regular")
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("registry-too-large")
    try:
        policy = json.loads(raw, object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError) as exc:
        raise ValueError("registry-invalid-json") from exc
    if (
        not isinstance(policy, dict)
        or set(policy) != {"version", "lanes"}
        or type(policy["version"]) is not int
        or policy["version"] != 1
        or not isinstance(policy["lanes"], list)
    ):
        raise ValueError("registry-invalid-schema")
    ids = policy["lanes"]
    if any(not isinstance(lane_id, str) or lane_id not in configured for lane_id in ids):
        raise ValueError("registry-unknown-lane")
    if len(set(ids)) != len(ids):
        raise ValueError("registry-duplicate-lane")
    return frozenset(ids)
