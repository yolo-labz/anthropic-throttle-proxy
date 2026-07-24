"""Spec 093 S2 — role inference from the request model.

Acceptance (S2): per-tier tests for generate / judge / bulk, and the subagent
slot (``claude-sonnet-4-6``) routes to bulk. Covers the model-tier table in
``routing.py`` plus the ``#1281`` drift (deployed subagent slot is
``claude-opus-4-8[1m]`` → ``generate``, documented for S7 reconciliation).
"""

from __future__ import annotations

import json
import time

import pytest

from anthropic_throttle_proxy.routing import (
    ROLE_CHAINS,
    ROLES,
    LaneState,
    bearer_usable,
    infer_role,
    infer_role_from_body,
    lane_usable,
    remap_body_model,
    select_lane,
    session_key_from_body,
)


@pytest.mark.parametrize(
    "model",
    [
        "claude-fable-5",
        "claude-opus-4-8",
        "claude-opus-4-8[1m]",  # 1M-context suffix stripped, still premium
        "Claude-Opus-4-8[1M]",
        "fable",
        "opus-4-8",
    ],
)
def test_infer_role_generate(model: str) -> None:
    assert infer_role(model) == "generate"


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-5",
        "claude-sonnet-5-20250929",
        "sonnet-5",
        "claude-sonnet 5.1",
        "sonnet5",  # compact form — the optional separator matches empty
    ],
)
def test_infer_role_judge(model: str) -> None:
    assert infer_role(model) == "judge"


def test_infer_role_strips_anthropic_prefix() -> None:
    """Defensive: an ``anthropic-`` prefix is normalized like ``claude-``."""
    assert infer_role("anthropic-opus-4-8") == "generate"
    assert infer_role("anthropic-sonnet-5") == "judge"


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-8[1m]",
        "claude-opus-4-8 [1m]",  # whitespace before suffix — .strip() handles it
        "  claude-opus-4-8[1m]  ",  # surrounding whitespace
        "claude-opus-4-8[1M]",  # case-insensitive suffix
    ],
)
def test_infer_role_context_suffix_stripped_without_redos_regex(model: str) -> None:
    """The context-suffix regex carries no ``\\s*`` anchor (ReDoS guard); the
    surrounding ``.strip()`` still normalizes whitespace."""
    assert infer_role(model) == "generate"


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-6",  # Spec 093 subagent slot (PR #1183)
        "claude-sonnet-4-6[1m]",
        "claude-haiku-4-5",
        "haiku-3",
    ],
)
def test_infer_role_bulk(model: str) -> None:
    assert infer_role(model) == "bulk"


@pytest.mark.parametrize("model", [None, "", "   ", "claude-[1m]"])
def test_infer_role_empty_or_absent_defaults_to_generate(model) -> None:
    """Inference never fails a request — unknown/absent → premium-default."""
    assert infer_role(model) == "generate"


def test_infer_role_unknown_model_defaults_to_generate() -> None:
    """An unrecognized id is conservatively treated as premium-generate
    (S3 then gauge-selects; S5 holds rather than silently downgrades)."""
    assert infer_role("claude-mythos-9") == "generate"


def test_infer_role_egress_models_never_client_sent() -> None:
    """glm/kimi ids are egress targets (S4 remaps TO them), never client-sent;
    if one ever reaches inference it defaults to generate (conservative)."""
    assert infer_role("glm-5.2") == "generate"
    assert infer_role("kimi-k3") == "generate"


def test_roles_are_exactly_the_three_spec_roles() -> None:
    """Spec 093 defines exactly three roles; guard against silent drift."""
    assert ROLES == ("generate", "judge", "bulk")


def test_drift_deployed_subagent_slot_is_generate_not_bulk() -> None:
    """NixOS #1330 / Issue #1281: the deployed subagent slot is
    ``claude-opus-4-8[1m]`` (NOT ``claude-sonnet-4-6`` as Spec 093 / PR #1183
    assume). Model-tier inference correctly maps it to ``generate``. Bulk
    routing for opus-slot subagents is a deployment-config reconciliation item
    for S7 (re-pin the subagent model OR add a role header), not an inference
    fix — this test pins the current behavior so the reconciliation is explicit.
    """
    assert infer_role("claude-opus-4-8[1m]") == "generate"


# --- infer_role_from_body: the request-body entry point ---


def test_infer_role_from_body_reads_model() -> None:
    body = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 8}).encode()
    assert infer_role_from_body(body) == "bulk"


def test_infer_role_from_body_missing_model_defaults_to_generate() -> None:
    body = json.dumps({"max_tokens": 8}).encode()
    assert infer_role_from_body(body) == "generate"


def test_infer_role_from_body_invalid_json_defaults_to_generate() -> None:
    assert infer_role_from_body(b"not json") == "generate"


def test_infer_role_from_body_empty_defaults_to_generate() -> None:
    assert infer_role_from_body(b"") == "generate"


def test_infer_role_from_body_non_object_defaults_to_generate() -> None:
    assert infer_role_from_body(b"[1, 2, 3]") == "generate"


# ─── Spec 093 S3 — gauge-driven lane selection ──────────────────────────────


def _st(open_: bool) -> LaneState:
    return LaneState(open_, time.time(), "test")


def test_select_lane_generate_prefers_anthropic_then_kimi_then_glm() -> None:
    """generate chain = anthropic→kimi→glm; first open wins."""
    assert (
        select_lane("generate", {"anthropic": _st(True), "kimi": _st(True), "glm": _st(True)})
        == "anthropic"
    )
    # anthropic closed → kimi
    assert (
        select_lane("generate", {"anthropic": _st(False), "kimi": _st(True), "glm": _st(True)})
        == "kimi"
    )
    # anthropic+kimi closed → glm (the floor)
    assert (
        select_lane("generate", {"anthropic": _st(False), "kimi": _st(False), "glm": _st(True)})
        == "glm"
    )


def test_select_lane_bulk_never_returns_anthropic_invariant_2() -> None:
    """Invariant 2: a bulk request NEVER routes to Anthropic, even when Anthropic
    is the only open lane (it's structurally absent from the bulk chain)."""
    # Anthropic wide open, Kimi/GLM closed → bulk still refuses Anthropic → None.
    assert (
        select_lane("bulk", {"anthropic": _st(True), "kimi": _st(False), "glm": _st(False)}) is None
    )
    # bulk prefers Kimi, then GLM
    assert select_lane("bulk", {"kimi": _st(True), "glm": _st(True)}) == "kimi"
    assert select_lane("bulk", {"kimi": _st(False), "glm": _st(True)}) == "glm"


def test_select_lane_lock_skips_to_next_open_invariant_3() -> None:
    """Invariant 3: a lane going closed mid-fleet → the next request auto-advances."""
    state = {"anthropic": _st(True), "kimi": _st(True), "glm": _st(True)}
    assert select_lane("generate", state) == "anthropic"
    # Anthropic account locks (all bearers capped) → next generate advances to Kimi
    state["anthropic"] = _st(False)
    assert select_lane("generate", state) == "kimi"


def test_select_lane_all_capped_returns_none_hold() -> None:
    """Every lane for the role closed → None (the caller HOLDs / 503s)."""
    assert (
        select_lane("generate", {"anthropic": _st(False), "kimi": _st(False), "glm": _st(False)})
        is None
    )
    assert select_lane("bulk", {"kimi": _st(False), "glm": _st(False)}) is None


def test_select_lane_unknown_role_uses_generate_chain() -> None:
    assert select_lane("???", {"anthropic": _st(True)}) == "anthropic"


def test_select_lane_missing_state_entry_is_skipped() -> None:
    """A lane never polled (absent from state) is skipped, not assumed open."""
    assert select_lane("generate", {"kimi": _st(True)}) == "kimi"
    assert select_lane("generate", {}) is None


def test_role_chains_anthropic_absent_from_bulk() -> None:
    """Structural guard: Anthropic must not appear in the bulk chain; GLM (the
    floor) is present in every chain so there's always a cheap fallback."""
    assert "anthropic" not in ROLE_CHAINS["bulk"]
    assert "anthropic" in ROLE_CHAINS["generate"]
    assert "anthropic" in ROLE_CHAINS["judge"]
    for role in ROLES:
        assert "glm" in ROLE_CHAINS[role]


# ─── gauge logic: bearer_usable / lane_usable ───────────────────────────────


def test_bearer_usable_clean_bearer_is_usable() -> None:
    assert bearer_usable(
        {
            "limiter": {"retry_after_until": 0},
            "unified": {"status_5h": "allowed", "status_7d": "allowed"},
        }
    )


def test_bearer_usable_retry_after_in_future_is_locked() -> None:
    future = time.time() + 600
    assert not bearer_usable({"limiter": {"retry_after_until": future}}, now=time.time())


def test_bearer_usable_rejected_window_is_locked() -> None:
    """A unified ``rejected`` window (5h or 7d) = budget exhausted = locked."""
    assert not bearer_usable({"limiter": {}, "unified": {"status_7d": "rejected"}})
    assert not bearer_usable({"limiter": {}, "unified": {"status_5h": "rejected"}})


def test_bearer_usable_lanes_without_unified_gauges_default_usable() -> None:
    """Kimi/GLM bearers carry no Anthropic unified gauges; absent ≠ locked."""
    assert bearer_usable({"limiter": {"retry_after_until": 0}})
    assert bearer_usable({})


def test_bearer_usable_malformed_retry_after_does_not_crash() -> None:
    """A non-numeric ``retry_after_until`` must not raise (gate MINOR): treat as
    not-set so a malformed lane health body never aborts request handling."""
    assert bearer_usable({"limiter": {"retry_after_until": "none"}})
    assert bearer_usable({"limiter": {"retry_after_until": [1, 2, 3]}})


def test_lane_usable_requires_upstream_egress_ok() -> None:
    ok_bearer = {"limiter": {"retry_after_until": 0}, "unified": {"status_7d": "allowed"}}
    assert lane_usable({"upstream_egress_ok": True, "bearers": {"b": ok_bearer}})[0]
    open_, detail = lane_usable({"upstream_egress_ok": False, "bearers": {"b": ok_bearer}})
    assert open_ is False and detail == "upstream-egress-down"


def test_lane_usable_no_bearers_is_closed() -> None:
    open_, detail = lane_usable({"upstream_egress_ok": True, "bearers": {}})
    assert open_ is False and detail == "no-bearers"


def test_lane_usable_all_bearers_locked_is_closed() -> None:
    """Invariant 3 detection: every bearer retry-aftered/rejected → lane closed."""
    future = time.time() + 600
    health = {
        "upstream_egress_ok": True,
        "bearers": {
            "a": {"limiter": {"retry_after_until": future}},
            "b": {"limiter": {}, "unified": {"status_7d": "rejected"}},
        },
    }
    open_, detail = lane_usable(health)
    assert open_ is False and detail == "no-usable-bearer"


# ─── Spec 093 S4 — model-remap + session key ────────────────────────────────


def test_remap_body_model_rewrites_model_field() -> None:
    """Invariant 4: egress body model == the lane's mapped id."""
    out = remap_body_model(b'{"model":"claude-sonnet-4-6","messages":[1]}', "kimi-k2.6")
    assert json.loads(out)["model"] == "kimi-k2.6"
    # other fields preserved
    assert json.loads(out)["messages"] == [1]


def test_remap_body_model_preserves_rest_of_body() -> None:
    out = remap_body_model(b'{"model":"x","max_tokens":8,"metadata":{"user_id":"u"}}', "glm-5.2")
    parsed = json.loads(out)
    assert parsed["model"] == "glm-5.2"
    assert parsed["max_tokens"] == 8
    assert parsed["metadata"]["user_id"] == "u"


def test_remap_body_model_invalid_json_returns_unchanged() -> None:
    """Remap never breaks a forward: unparseable body passes through verbatim."""
    raw = b"not json"
    assert remap_body_model(raw, "kimi-k2.6") == raw


def test_remap_body_model_empty_model_or_body_is_noop() -> None:
    assert remap_body_model(b"", "kimi-k2.6") == b""
    assert remap_body_model(b'{"model":"x"}', "") == b'{"model":"x"}'


def test_session_key_from_body_reads_metadata_user_id() -> None:
    assert session_key_from_body(b'{"metadata":{"user_id":"user-42"}}') == "user-42"


def test_session_key_from_body_absent_returns_none() -> None:
    assert session_key_from_body(b'{"model":"x"}') is None
    assert session_key_from_body(b'{"metadata":{}}') is None
    assert session_key_from_body(b"") is None
    assert session_key_from_body(b"not json") is None


def test_session_key_from_body_non_string_user_id_returns_none() -> None:
    assert session_key_from_body(b'{"metadata":{"user_id":123}}') is None
