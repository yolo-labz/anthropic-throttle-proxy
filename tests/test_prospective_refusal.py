"""T004 refusal plan tests: typed local refusal payload + bounded 503 factory.

No network, no server: aiohttp ``web.Response`` values are constructed and
inspected directly, and the payload contract is asserted as plain data. The
suite pins: the exact 503 response, reason/Retry-After/budget bounds and types,
CRLF and spoof stripping, and the immutability of the typed payload.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from anthropic_throttle_proxy.prospective_refusal import (
    ERROR_TYPE,
    PROVENANCE_BUDGET_HEADER,
    PROVENANCE_CLASS_HEADER,
    PROVENANCE_HEADERS,
    PROVENANCE_LOCAL_VALUE,
    PROVENANCE_REASON_HEADER,
    PROVENANCE_SOURCE_HEADER,
    RETRY_AFTER_MAX_S,
    RETRY_AFTER_MIN_S,
    ProspectiveRefusal,
    RefusalReason,
    local_refusal_response,
    strip_incoming_provenance,
)


def _refusal(**over) -> ProspectiveRefusal:
    base = {
        "reason": RefusalReason.EXHAUSTED,
        "retry_after_s": 30,
        "budget_label": "mimo-team-b",
    }
    base.update(over)
    return ProspectiveRefusal(**base)


# ---------------------------------------------------------------- payload


def test_error_payload_is_the_exact_envelope():
    payload = _refusal().error_payload()
    assert payload == {
        "type": "error",
        "error": {
            "type": ERROR_TYPE,
            "message": "local policy: prospective admission refused; budget is exhausted"
            " (mimo-team-b)",
            "reason": "exhausted",
            "budget": "mimo-team-b",
            "retry_after_s": 30,
        },
    }


def test_payload_is_typed_and_frozen():
    refusal = _refusal()
    assert isinstance(refusal.reason, RefusalReason)
    assert isinstance(refusal.retry_after_s, int)
    with pytest.raises(dataclasses.FrozenInstanceError):
        refusal.retry_after_s = 0


def test_every_allowlisted_reason_round_trips():
    for reason in RefusalReason:
        payload = _refusal(reason=reason).error_payload()
        assert payload["error"]["reason"] == reason.value
        assert ERROR_TYPE == payload["error"]["type"]


def test_reason_is_a_finite_allowlist():
    with pytest.raises(ValueError):
        _refusal(reason="totally-new-reason")
    with pytest.raises(ValueError):
        _refusal(reason=None)


# ---------------------------------------------------------- bounds / types


@pytest.mark.parametrize("bad", [0, -1, RETRY_AFTER_MAX_S + 1, True, False, 1.5, "30"])
def test_retry_after_rejects_out_of_bounds_and_wrong_types(bad):
    with pytest.raises((TypeError, ValueError)):
        _refusal(retry_after_s=bad)


def test_retry_after_bounds_are_inclusive():
    assert _refusal(retry_after_s=RETRY_AFTER_MIN_S).retry_after_s == 1
    assert _refusal(retry_after_s=RETRY_AFTER_MAX_S).retry_after_s == RETRY_AFTER_MAX_S


# ------------------------------------------------------- label validation


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "plan\r\nSet-Cookie: pwned",
        "plan\nX-Injected: 1",
        "plan\rX",
        "../../etc/passwd",
        "accounts/secret-bucket",
        "sk-ant-oat01-LEAK",
        "has space",
        'quote"d',
        "colon:mimo",
        "equals=1",
        "UPPER",
        "a" * 64,
        ".leading-dot",
    ],
)
def test_budget_label_rejects_crlf_paths_quotes_and_bad_charset(bad):
    with pytest.raises(ValueError):
        _refusal(budget_label=bad)


@pytest.mark.parametrize("ok", ["plan", "mimo-team-b", "five_hour", "a", "a.b_c-d", "z9"])
def test_budget_label_accepts_public_tokens(ok):
    assert _refusal(budget_label=ok).budget_label == ok


def test_message_never_echoes_raw_input_beyond_validated_label():
    refusal = _refusal(budget_label="mimo-team-b")
    assert "\r" not in refusal.message and "\n" not in refusal.message


# ------------------------------------------------------------- 503 factory


def test_local_refusal_response_is_exact_503():
    response = local_refusal_response(_refusal(retry_after_s=42))
    assert response.status == 503
    assert response.headers["retry-after"] == "42"
    assert response.headers[PROVENANCE_SOURCE_HEADER] == PROVENANCE_LOCAL_VALUE
    assert response.headers[PROVENANCE_CLASS_HEADER] == "1"
    assert response.headers[PROVENANCE_BUDGET_HEADER] == "mimo-team-b"
    assert response.headers[PROVENANCE_REASON_HEADER] == "exhausted"
    assert "x-throttle-meter-refusal" not in response.headers
    body = json.loads(response.text)
    assert body["type"] == "error"
    assert body["error"]["type"] == ERROR_TYPE
    assert body["error"]["retry_after_s"] == 42


def test_provenance_headers_are_module_constants_not_request_derived():
    # Local authority is minted here; the factory has no request/header input at
    # all, so nothing incoming can influence provenance.
    assert PROVENANCE_HEADERS == {
        "x-throttle-refusal-source": "local",
        "x-throttle-prospective-refusal": "1",
    }
    assert local_refusal_response(_refusal()).headers[PROVENANCE_SOURCE_HEADER] == "local"


# --------------------------------------------------- spoof stripping (future)


def test_strip_incoming_provenance_is_case_insensitive_and_total():
    incoming = {
        "X-Throttle-Refusal-Source": "local",  # spoofed claim
        "x-throttle-prospective-refusal": "1",
        "X-THROTTLE-PROSPECTIVE-REFUSAL": "1",
        "X-Throttle-Prospective-Budget": "spoofed-private-budget",
        "X-THROTTLE-PROSPECTIVE-REASON": "spoofed-reason",
        "content-type": "application/json",
        "authorization": "Bearer kept",
    }
    cleaned = strip_incoming_provenance(incoming)
    assert cleaned == {"content-type": "application/json", "authorization": "Bearer kept"}


def test_strip_incoming_provenance_drops_crlf_laden_spoof_rows():
    incoming = {"X-Throttle-Refusal-Source": "local\r\nX-Injected: 1", "x-ok": "y"}
    cleaned = strip_incoming_provenance(incoming)
    assert cleaned == {"x-ok": "y"}
    for value in cleaned.values():
        assert "\r" not in value and "\n" not in value


def test_strip_incoming_provenance_preserves_unrelated_headers_unchanged():
    incoming = {"a": "1", "B": "2", "x-custom": "3"}
    assert strip_incoming_provenance(incoming) == incoming
