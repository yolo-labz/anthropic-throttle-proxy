"""Synthetic manifests and caller context, never producer admission."""

import hashlib
import json
from dataclasses import replace

import pytest

from anthropic_throttle_proxy.ui.observatory_evidence import Generation, Scope, selection_relation
from anthropic_throttle_proxy.ui.observatory_generation import AuthorizationContext, bind_generation

SCOPE = Scope("gateway_requests_utc:all")
GRANT_DIGEST = "d" * 64


def manifest(**fields):
    return json.dumps(
        {"schema_version": 1, "complete": True, "ok": True, "source_snapshot_id": "synthetic.A"}
        | fields
    ).encode()


def generation(body):
    return Generation("synthetic-owner", "synthetic.A", hashlib.sha256(body).hexdigest())


def context(expected):
    return AuthorizationContext(
        generation=expected,
        grant_sha256=GRANT_DIGEST,
        authenticated_consumer="synthetic-app",
        intended_consumer="synthetic-app",
        scope=SCOPE,
        exposure="aggregate-only",
    )


def test_bind_exact_owned_bytes_without_claiming_coverage_or_a_measure():
    body = manifest()
    expected = generation(body)
    result = bind_generation(context(expected), expected, body)

    assert result.status == "observed"
    assert result.reason is None
    assert result.value.generation == expected
    assert result.value.grant_sha256 == GRANT_DIGEST
    assert result.evidence.source.generation == expected
    assert result.evidence.source.loaded_sha256 == hashlib.sha256(body).hexdigest()
    assert result.evidence.scope == SCOPE
    assert result.evidence.coverage.state == "unknown"
    assert result.evidence.coverage.refs == ()
    assert result.evidence.window is result.evidence.measure is result.evidence.unit is None
    assert selection_relation(result.evidence, SCOPE).value == "matched"


@pytest.mark.parametrize("body", [None, b"{}", manifest()])
def test_unknown_authorization_never_exposes_an_input_reference(body):
    result = bind_generation(None, generation(manifest()), body)

    assert result.status == "unknown"
    assert result.reason == "authorization_unavailable"
    assert result.value is result.evidence.source is result.evidence.scope is None


def test_manifest_absence_is_not_an_authority_or_digest_mismatch():
    expected = generation(manifest())
    result = bind_generation(context(expected), expected, None)

    assert result.status == "missing"
    assert result.reason == "manifest_unavailable"
    assert result.value is result.evidence.source is result.evidence.scope is None


@pytest.mark.parametrize(
    "changes",
    [
        {"intended_consumer": "synthetic.other"},
        {"authenticated_consumer": ""},
        {"authenticated_consumer": " "},
        {"intended_consumer": None},
        {"exposure": "native"},
        {"scope": Scope("native_calls_utc:all")},
        {"scope": Scope("gateway_requests_utc:all", (("provider", "synthetic"),))},
        {"scope": Scope("gateway_requests_utc:all", identity_namespace="synthetic")},
        {"grant_sha256": "not-a-pin"},
    ],
)
def test_reject_cross_consumer_exposure_scope_and_invalid_grant(changes):
    body = manifest()
    expected = generation(body)
    result = bind_generation(replace(context(expected), **changes), expected, body)

    assert result.status == "rejected"
    assert result.reason == "authorization_scope_mismatch"
    assert result.value is result.evidence.source is result.evidence.scope is None
    assert selection_relation(result.evidence, SCOPE).value == "unsupported"


@pytest.mark.parametrize(
    "changes",
    [{"authority": "synthetic-other"}, {"id": "synthetic.B"}, {"manifest_sha256": "b" * 64}],
)
def test_reject_generation_scope_mismatch_even_with_correct_bytes(changes):
    body = manifest()
    expected = generation(body)
    auth = context(replace(expected, **changes))
    assert bind_generation(auth, expected, body).reason == "generation_mismatch"


def test_reject_reencoding_of_same_json_without_reading_it_again():
    body = manifest()
    expected = generation(body)
    assert json.loads(body) == json.loads(body + b" ")
    result = bind_generation(context(expected), expected, body + b" ")

    assert result.status == "rejected"
    assert result.reason == "manifest_digest_mismatch"
    assert result.value is result.evidence.source is None


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"not JSON",
        b"\xff",
        manifest()[:-1] + b', "ok": true}',
        manifest()[:-1] + b', "n": NaN}',
        b" " * 65_537,
        manifest(ok=False),
        manifest(complete=False),
        manifest(complete=1),
        manifest(schema_version=True),
        manifest(schema_version=2),
    ],
)
def test_exact_digest_of_invalid_manifest_is_not_accepted(body):
    expected = generation(body)
    result = bind_generation(context(expected), expected, body)

    assert result.status == "rejected"
    assert result.reason == "invalid_manifest"
    assert result.value is result.evidence.source is None


def test_matching_digest_cannot_substitute_for_manifest_generation():
    body = manifest(source_snapshot_id="synthetic.B")
    expected = generation(body)
    result = bind_generation(context(expected), expected, body)

    assert result.status == "rejected"
    assert result.reason == "generation_mismatch"
    assert result.value is None


def test_moving_alias_is_not_an_exact_generation_even_with_a_pin():
    body = manifest(source_snapshot_id="latest")
    expected = replace(generation(body), id="latest")
    result = bind_generation(context(expected), expected, body)

    assert result.status == "rejected"
    assert result.reason == "exact_generation_required"
    assert result.value is None


@pytest.mark.parametrize("body", ["synthetic", bytearray(b"synthetic"), memoryview(b"synthetic")])
def test_only_exact_immutable_bytes_are_consumed(body):
    expected = generation(manifest())
    assert bind_generation(context(expected), expected, body).reason == "invalid_manifest"


@pytest.mark.parametrize("authorization", [True, {"consumer": "synthetic-app"}])
def test_generic_boolean_or_mapping_is_not_an_authorization_context(authorization):
    body = manifest()
    expected = generation(body)
    result = bind_generation(authorization, expected, body)

    assert result.status == "rejected"
    assert result.reason == "authorization_scope_mismatch"
    assert result.value is None
