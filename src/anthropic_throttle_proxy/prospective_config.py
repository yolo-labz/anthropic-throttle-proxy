"""Explicit startup manifest for one prospective-admission process.

Off never reads this module's optional path. Enabled callers retain the custody
handle until their persistence owner has drained. Mode directories separate
partial observation samples from strict restart debt; nothing deletes history.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

from .prospective_admission import Scope
from .prospective_scope import ScopeResolver

_LIMIT = 1024 * 1024
_FIELDS = {
    "sources",
    "endpoints",
    "models",
    "entries",
    "state_directory",
    "allow_cold_start",
    "max_pending",
    "wait_timeout",
    "budget_label",
    "retry_after_s",
}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate prospective configuration key")
        value[key] = item
    return value


def _catalogs(data: dict) -> None:
    for key in ("sources", "endpoints", "models"):
        values = data[key]
        if not isinstance(values, list) or not values:
            raise ValueError("prospective catalogs must be nonempty string lists")
        if any(type(item) is not str or not item.strip() for item in values):
            raise ValueError("prospective catalog labels must be nonempty strings")
        if len(set(values)) != len(values):
            raise ValueError("duplicate prospective catalog labels")


def _manifest(path: str) -> dict:
    with open(path, "rb") as handle:
        raw = handle.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError("prospective manifest exceeds 1 MiB")
    data = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise ValueError("prospective manifest requires exactly the documented fields")
    if type(data["allow_cold_start"]) is not bool:
        raise ValueError("cold-start permission must be explicit boolean")
    _catalogs(data)
    if not isinstance(data["entries"], list) or not data["entries"]:
        raise ValueError("prospective entries must be a nonempty list")
    required = {"source", "endpoint", "alias", "upstream", "account", "model", "budgets"}
    for row in data["entries"]:
        if not isinstance(row, dict) or not required <= row.keys() <= required | {"output_default"}:
            raise ValueError("prospective scope entries contain missing or unknown fields")
    return data


def _unaliased_file(path: Path) -> None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
        raise ValueError("prospective custody file must not alias another owner")


def runtime_settings(path: str, mode: str) -> tuple[dict, Path]:
    """Parse on a startup worker; return arguments and exclusive state directory."""
    if mode not in {"observe", "strict"}:
        raise ValueError("enabled prospective mode must be observe or strict")
    data = _manifest(path)
    directory = data["state_directory"]
    if not isinstance(directory, str) or not os.path.isabs(directory):
        raise ValueError("prospective state_directory must be absolute")
    directory = Path(directory) / mode
    if directory.is_symlink():
        raise ValueError("prospective mode directory must not alias another mode")
    resolver = ScopeResolver(
        data["entries"], sources=data["sources"], endpoints=data["endpoints"], models=data["models"]
    )
    scopes = {}
    for row in data["entries"]:
        match = resolver.resolve(row["source"], row["endpoint"], row["alias"])
        key = match.scope.key
        filename = hashlib.sha256(json.dumps(key).encode()).hexdigest() + ".json"
        _unaliased_file(directory / filename)
        scopes[key] = Scope(key, match.budgets, str(directory / filename), data["allow_cold_start"])
    settings = {
        key: data[key] for key in ("max_pending", "wait_timeout", "budget_label", "retry_after_s")
    }
    return {
        "mode": mode,
        "resolver": resolver,
        "scopes": tuple(scopes.values()),
        **settings,
    }, directory


def acquire_custody(directory: Path, scopes=()):
    """Cooperating local writers only; trusted state paths must stay unchanged."""
    if directory.is_symlink():
        raise ValueError("prospective mode directory must not alias another mode")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = directory / ".owner.lock"
    _unaliased_file(lock)
    handle = os.fdopen(os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "r+b")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for scope in scopes:
            path = Path(scope.state_path)
            if path.parent != directory:
                raise ValueError("prospective ledger escaped its custody directory")
            _unaliased_file(path)
    except BaseException:
        handle.close()
        raise
    return handle
