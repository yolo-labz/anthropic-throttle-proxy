"""Fresh read-only process/config binding; not permission to retry a model call."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from . import __build__

PATH = "/__throttle/pre-egress-capability"
_MODULE_NAMES = frozenset({"proxy.py", "forwarding.py", "capability.py"})
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_INVOCATION = re.compile(r"[0-9a-f]{32}\Z")
_STORE_PATH = re.compile(r"/nix/store/[0-9abcdfghijklmnpqrsvwxyz]{32}-[^/]+/.+")
_CONFIG_METADATA = frozenset({"EDITABLE_KNOBS", "ENV_DEFAULTS", "RUNTIME_OVERRIDES"})
_TRANSPORT_ENV = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)
_PID = os.getpid()
_INVOCATION_ID = os.environ.get("INVOCATION_ID", "")
_BUILD = __build__
_LOADED: dict[str, str | None] = {}


def record_loaded_module(filename: str) -> None:
    """Capture once DURING that module's import; never rehash files on GET."""
    path = Path(filename).resolve()
    digest = None
    if path.name not in _MODULE_NAMES:
        raise ValueError("unsupported capability module")
    if _STORE_PATH.fullmatch(str(path)) and str(path.parent) == _BUILD:
        try:
            with path.open("rb") as handle:
                data = handle.read(1024 * 1024 + 1)
            if len(data) <= 1024 * 1024:
                digest = hashlib.sha256(data).hexdigest()
        except OSError:
            digest = None  # Unsupported capability must not break the data plane.
    if path.name in _LOADED and _LOADED[path.name] != digest:
        digest = None  # A mixed/reloaded generation cannot attest old loaded bytes.
    _LOADED[path.name] = digest


def _routing_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.startswith(("THROTTLE_", "INGRESS_", "CLAUDE_API_")) or key in _TRANSPORT_ENV
    }


_STARTUP_ENV = _routing_environment()
record_loaded_module(__file__)


def _canonical(value, depth: int = 0):
    if depth > 8:
        raise ValueError("configuration nesting exceeds limit")
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _canonical(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_canonical(item, depth + 1) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    raise ValueError("unsupported effective configuration value")


def config_sha256(config, proxy) -> str:
    """Hash effective values, not override metadata or external file contents."""
    fields = {
        key: value
        for key, value in vars(config).items()
        if key.isupper() and not key.startswith("_") and key not in _CONFIG_METADATA
    }
    # Historical proxy-local tunables/re-exports are separate effective values.
    proxy_fields = {
        key: value
        for key, value in vars(proxy).items()
        if key.isupper()
        and not key.startswith(("_", "M_"))
        and key not in {"REGISTRY", "TYPE_CHECKING"}
    }
    knobs = {key: spec["getter"]() for key, spec in config.EDITABLE_KNOBS.items()}
    payload = _canonical(
        {
            "config": fields,
            "proxy": proxy_fields,
            "editable": knobs,
            "startupRoutingEnvironment": _STARTUP_ENV,
            "routingEnvironment": _routing_environment(),
        }
    )
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()
    if len(raw) > 65536:
        raise ValueError("effective configuration exceeds capability bound")
    return hashlib.sha256(raw).hexdigest()


def _response(status: int, data: dict) -> web.Response:
    return web.json_response(
        data, status=status, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
    )


async def pre_egress_capability(request: web.Request) -> web.Response:
    """Fixed local control plane, including while admission is genuinely closed."""
    if request.method != "GET":
        response = _response(405, {"error": "method_not_allowed"})
        response.headers["Allow"] = "GET"
        return response
    if list(request.query) != ["nonce"] or len(request.query.getall("nonce", [])) != 1:
        return _response(400, {"error": "invalid_nonce"})
    nonce = request.query["nonce"]
    if not _HEX64.fullmatch(nonce):
        return _response(400, {"error": "invalid_nonce"})
    if (
        os.getpid() != _PID
        or not _INVOCATION.fullmatch(_INVOCATION_ID)
        or os.environ.get("INVOCATION_ID", "") != _INVOCATION_ID
        or set(_LOADED) != _MODULE_NAMES
        or not all(_LOADED.values())
    ):
        return _response(503, {"error": "unsupported_generation"})
    from . import config, proxy

    try:
        central = config.CENTRAL_URL
        parsed = urlsplit(central)
        # Exact centralUrl cannot safely publish credential-bearing URL data.
        if (
            not isinstance(central, str)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("unsupported central URL")
        fingerprint = config_sha256(config, proxy)
    except (ValueError, TypeError, KeyError, AttributeError):
        return _response(503, {"error": "unsupported_configuration"})
    return _response(
        200,
        {
            "schemaVersion": 1,
            "kind": "gateway-pre-egress-capability",
            "nonce": nonce,
            "pid": _PID,
            "invocationId": _INVOCATION_ID,
            "build": _BUILD,
            "modules": dict(_LOADED),
            "configSha256": fingerprint,
            "centralUrl": central,
        },
    )


def attach_capability(app: web.Application) -> None:
    # Reserve ALL methods at this literal path: none may hit the catch-all relay.
    app.router.add_route("*", PATH, pre_egress_capability)
