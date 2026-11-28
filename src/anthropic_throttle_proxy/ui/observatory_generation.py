"""Bind caller-supplied manifest bytes to an already authorized generation.

The caller authenticates the principals, verifies the exact grant digest against
owner evidence, and checks current authorization before constructing the context.
This module does not admit a source, resolve a generation, or read any data.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

from .observatory_evidence import Coverage, Evidence, Generation, Scope, SourceRef

# Match the existing P3 bounded-JSON ceiling; this is not a new wire contract.
_MAX_MANIFEST_BYTES = 65_536
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GATEWAY_SCOPE = Scope("gateway_requests_utc:all")


@dataclass(frozen=True)
class AuthorizationContext:
    """Current, independently verified, exact-consumer authorization from the caller.

    Construction is not verification. A decoded grant, untrusted request principal,
    or historical receipt is not a substitute for this caller precondition.
    """

    generation: Generation
    grant_sha256: str
    authenticated_consumer: str
    intended_consumer: str
    scope: Scope
    exposure: str


@dataclass(frozen=True)
class GenerationRef:
    generation: Generation
    grant_sha256: str


@dataclass(frozen=True)
class GenerationBinding:
    status: Literal["observed", "missing", "unknown", "rejected"]
    evidence: Evidence
    value: GenerationRef | None = None
    reason: str | None = None
    message: str | None = None


def _absent(
    status: Literal["missing", "unknown", "rejected"], reason: str, message: str
) -> GenerationBinding:
    return GenerationBinding(
        status,
        Evidence(None, None, None, None, None, Coverage(status, (reason,))),
        reason=reason,
        message=message,
    )


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate manifest fields")
    return fields


def _non_json_number(_literal: str) -> None:
    raise ValueError("non-JSON manifest number")


def _completed_manifest_id(body: bytes) -> str | None:
    try:
        doc = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_fields, parse_constant=_non_json_number
        )
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    version, snapshot_id = doc.get("schema_version"), doc.get("source_snapshot_id")
    if (
        type(version) is int
        and version == 1
        and doc.get("complete") is True
        and doc.get("ok") is True
        and isinstance(snapshot_id, str)
        and snapshot_id
    ):
        return snapshot_id
    return None


def _scope_matches(authorization: AuthorizationContext) -> bool:
    consumer = authorization.authenticated_consumer
    return (
        isinstance(consumer, str)
        and bool(consumer.strip())
        and isinstance(authorization.intended_consumer, str)
        and consumer == authorization.intended_consumer
        and authorization.exposure == "aggregate-only"
        and authorization.scope == _GATEWAY_SCOPE
        and isinstance(authorization.grant_sha256, str)
        and _SHA256.fullmatch(authorization.grant_sha256) is not None
    )


def bind_generation(
    authorization: AuthorizationContext | None,
    expected_generation: Generation,
    manifest_bytes: bytes | None,
) -> GenerationBinding:
    """Bind a complete v1 manifest's exact bytes to the expected N14 generation.

    The expected digest is part of the owner-namespaced generation. Use None for
    unavailable current authorization or absent bytes; no identifier is resolved.
    Manifest completeness is not coverage. Member and interval checks stay separate.
    """
    if authorization is None:
        return _absent(
            "unknown", "authorization_unavailable", "Current authorization is unavailable."
        )
    if not isinstance(authorization, AuthorizationContext) or not _scope_matches(authorization):
        return _absent(
            "rejected",
            "authorization_scope_mismatch",
            "Authorization does not match consumer or scope.",
        )
    if not isinstance(expected_generation, Generation) or expected_generation.id == "latest":
        return _absent(
            "rejected", "exact_generation_required", "An exact immutable generation is required."
        )
    if authorization.generation != expected_generation:
        return _absent(
            "rejected", "generation_mismatch", "Authorization pins a different generation."
        )
    if manifest_bytes is None:
        return _absent("missing", "manifest_unavailable", "The pinned manifest is unavailable.")
    if type(manifest_bytes) is not bytes or len(manifest_bytes) > _MAX_MANIFEST_BYTES:
        return _absent("rejected", "invalid_manifest", "Expected bounded immutable manifest bytes.")
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    if digest != expected_generation.manifest_sha256:
        return _absent(
            "rejected", "manifest_digest_mismatch", "Manifest bytes do not match the pin."
        )
    snapshot_id = _completed_manifest_id(manifest_bytes)
    if snapshot_id is None:
        return _absent("rejected", "invalid_manifest", "Expected a complete version-1 manifest.")
    if snapshot_id != expected_generation.id:
        return _absent("rejected", "generation_mismatch", "Manifest names a different generation.")
    evidence = Evidence(
        SourceRef("dp08_manifest", digest, expected_generation),
        None,
        authorization.scope,
        None,
        None,
        Coverage("unknown", ("continuity_unverified",)),
    )
    return GenerationBinding(
        "observed", evidence, GenerationRef(expected_generation, authorization.grant_sha256)
    )
