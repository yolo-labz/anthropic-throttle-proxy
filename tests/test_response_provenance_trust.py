"""Response provenance trust — raw upstream cannot mint local verdicts (audit 04/10).

Falsifiers from the read-only audit of b5a08ee0:
- F1: a raw upstream response carrying local refusal provenance must be stripped
  before relaying; a marker-stamped sibling may relay its own verdict.
- F2a/F2b: a header-only spoof (queue-timeout / entitlement stamp WITHOUT the
  sibling marker) must not suppress ingress retry/spill decisions.
"""

from anthropic_throttle_proxy import forwarding, ingress
from anthropic_throttle_proxy.config import MARKER_HEADER

PROVENANCE = {
    "x-throttle-refusal-source": "local",
    "x-throttle-prospective-refusal": "1",
    "x-throttle-prospective-budget": "mimo-team-b",
    "x-throttle-prospective-reason": "exhausted",
}


def test_raw_upstream_response_provenance_is_stripped():
    drop = forwarding._untrusted_response_drop_headers({})
    for name in PROVENANCE:
        assert name in drop, name
    assert "x-anthropic-throttle-queue-timeout" in drop
    assert "x-anthropic-throttle-oauth-entitlement" in drop


def test_marker_stamped_sibling_relays_its_own_verdicts():
    drop = forwarding._untrusted_response_drop_headers({MARKER_HEADER: "1"})
    for name in PROVENANCE:
        assert name not in drop, name
    assert "x-anthropic-throttle-queue-timeout" not in drop


class _Upstream:
    def __init__(self, status, headers):
        self.status = status
        self.headers = headers


def test_ingress_queue_timeout_requires_sibling_marker():
    spoof = _Upstream(503, {"x-anthropic-throttle-queue-timeout": "1"})
    assert ingress._queue_timeout_503(spoof) is False
    real = _Upstream(503, {MARKER_HEADER: "1", "x-anthropic-throttle-queue-timeout": "1"})
    assert ingress._queue_timeout_503(real) is True


def test_ingress_entitlement_requires_sibling_marker():
    spoof = _Upstream(429, {"x-anthropic-throttle-oauth-entitlement": "1"})
    assert ingress._entitlement_refusal(spoof) is False
    real = _Upstream(429, {MARKER_HEADER: "1", "x-anthropic-throttle-oauth-entitlement": "1"})
    assert ingress._entitlement_refusal(real) is True


def test_ingress_relay_strips_provenance_from_any_upstream():
    # Truth-authoring rule: even a marker-stamped sibling's provenance stamps
    # are stripped on the ingress relay — the ingress authors refusal truth.
    for headers in ({**PROVENANCE}, {MARKER_HEADER: "1", **PROVENANCE}):
        kept = {
            k: v
            for k, v in headers.items()
            if k.lower() not in ingress._RESERVED_CREDENTIAL_RESPONSE_HEADERS
            and k.lower() not in ingress._RESERVED_LOCAL_PROVENANCE_HEADERS
        }
        for name in PROVENANCE:
            assert name not in kept, name
