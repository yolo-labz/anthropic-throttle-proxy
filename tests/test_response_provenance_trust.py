"""Ingress response provenance trust — the ingress authors refusal truth.

Scope narrowed 04/10 03:5x after hosted run 37183161372 evidence: merged tests
PIN the trust topology that the first cut contradicted —
- configured-lane stamps are honored WITHOUT a marker header
  (tests/test_ingress.py saturation/queue-wait fixtures, test_ingress_adr6a),
- the forwarding relay crosses response provenance verbatim
  (tests/test_prospective_forwarding_live.py:218).
Those are contracts, not defects; this file pins only the part that is NOT
pinned anywhere: local refusal provenance is stripped from ANY upstream
response on the INGRESS relay (same truth-authoring rule as the existing
credential-header strip), so a lane/provider cannot forge a locally-minted
verdict toward ingress clients.
"""

from anthropic_throttle_proxy import ingress
from anthropic_throttle_proxy.config import MARKER_HEADER

PROVENANCE = {
    "x-throttle-refusal-source": "local",
    "x-throttle-prospective-refusal": "1",
    "x-throttle-prospective-budget": "mimo-team-b",
    "x-throttle-prospective-reason": "exhausted",
}


def test_ingress_relay_strips_provenance_from_any_upstream():
    # Even a marker-stamped sibling's provenance stamps are stripped on the
    # ingress relay: the ingress authors refusal truth (precedent: the
    # credential-mode response headers above the same filter).
    for headers in ({**PROVENANCE}, {MARKER_HEADER: "1", **PROVENANCE}):
        kept = {
            k: v
            for k, v in headers.items()
            if k.lower() not in ingress._RESERVED_CREDENTIAL_RESPONSE_HEADERS
            and k.lower() not in ingress._RESERVED_LOCAL_PROVENANCE_HEADERS
        }
        for name in PROVENANCE:
            assert name not in kept, name


def test_ingress_predicates_trust_configured_lane_stamps():
    # Reconciled contract (merged fixtures pin it): ingress upstreams are
    # CONFIGURED lanes; their queue-timeout/entitlement stamps are honored
    # without a marker. Pinned here so nobody re-applies the raw-upstream
    # marker rule to the lane predicates (raw providers never reach ingress).

    class _Upstream:
        def __init__(self, status, headers):
            self.status = status
            self.headers = headers

    assert ingress._queue_timeout_503(_Upstream(503, {"x-anthropic-throttle-queue-timeout": "1"}))
    assert ingress._entitlement_refusal(
        _Upstream(429, {"x-anthropic-throttle-oauth-entitlement": "1"})
    )
