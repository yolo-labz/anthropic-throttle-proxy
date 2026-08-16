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

from anthropic_throttle_proxy import routing
from anthropic_throttle_proxy.routing import (
    CODE_MAX_BODY_BYTES,
    CODE_MAX_TOKENS,
    ROLE_CHAINS,
    ROLES,
    LaneState,
    bearer_usable,
    body_has_tools,
    default_lanes,
    effective_chain,
    infer_role,
    infer_role_from_body,
    is_small_agentic_code,
    lane_usable,
    remap_body_model,
    role_from_header,
    select_lane,
    session_key_from_body,
    unified_live_view,
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


def test_roles_are_exactly_the_spec_roles() -> None:
    """Spec 093 defines generate/judge/bulk; #182 adds code. Guard against drift."""
    assert ROLES == ("generate", "judge", "bulk", "code")


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
    """generate chain = anthropic→kimi→glm; first open wins. Requires overflow ON
    (post-kimi-k3 GA) — pre-GA, generate is Anthropic-only (see S5 tests)."""
    assert (
        select_lane(
            "generate", {"anthropic": _st(True), "kimi": _st(True), "glm": _st(True)}, overflow=True
        )
        == "anthropic"
    )
    # anthropic closed → kimi
    assert (
        select_lane(
            "generate",
            {"anthropic": _st(False), "kimi": _st(True), "glm": _st(True)},
            overflow=True,
        )
        == "kimi"
    )
    # anthropic+kimi closed → glm (the floor)
    assert (
        select_lane(
            "generate",
            {"anthropic": _st(False), "kimi": _st(False), "glm": _st(True)},
            overflow=True,
        )
        == "glm"
    )


def test_select_lane_bulk_never_returns_anthropic_invariant_2() -> None:
    """Invariant 2: a bulk request NEVER routes to Anthropic, even when Anthropic
    is the only open lane (it's structurally absent from the bulk chain)."""
    # Anthropic wide open, Kimi/GLM closed → bulk still refuses Anthropic → None.
    assert (
        select_lane("bulk", {"anthropic": _st(True), "kimi": _st(False), "glm": _st(False)}) is None
    )
    # bulk prefers Kimi, then GLM (deepseek absent from state → skipped)
    assert select_lane("bulk", {"kimi": _st(True), "glm": _st(True)}) == "kimi"
    assert select_lane("bulk", {"kimi": _st(False), "glm": _st(True)}) == "glm"


def test_select_lane_bulk_prefers_deepseek_over_kimi_and_glm() -> None:
    """#169: DeepSeek V4-Flash leads the bulk chain — the funded, reachable lane
    (Kimi/GLM stay as unretired fallback but neither answers today)."""
    assert (
        select_lane("bulk", {"deepseek": _st(True), "kimi": _st(True), "glm": _st(True)})
        == "deepseek"
    )
    # deepseek closed → falls through to kimi, then glm
    assert (
        select_lane("bulk", {"deepseek": _st(False), "kimi": _st(True), "glm": _st(True)}) == "kimi"
    )


def test_select_lane_lock_skips_to_next_open_invariant_3() -> None:
    """Invariant 3: a lane going closed mid-fleet → the next request auto-advances
    (overflow ON; bulk/judge always advance, generate only post-kimi-k3)."""
    state = {"anthropic": _st(True), "kimi": _st(True), "glm": _st(True)}
    assert select_lane("generate", state, overflow=True) == "anthropic"
    # Anthropic account locks → next generate advances to Kimi
    state["anthropic"] = _st(False)
    assert select_lane("generate", state, overflow=True) == "kimi"


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
    assert select_lane("generate", {"kimi": _st(True)}, overflow=True) == "kimi"
    assert select_lane("generate", {}) is None


def test_role_chains_anthropic_absent_from_bulk() -> None:
    """Structural guard: Anthropic must not appear in the bulk chain; GLM (the
    floor) is present in every TIER-based chain so there's always a cheap
    fallback. "code" is a class-based offload (#182), not a cost floor — its
    chain (codex -> anthropic -> deepseek) deliberately has no GLM leg."""
    assert "anthropic" not in ROLE_CHAINS["bulk"]
    assert "anthropic" in ROLE_CHAINS["generate"]
    assert "anthropic" in ROLE_CHAINS["judge"]
    for role in ("generate", "judge", "bulk"):
        assert "glm" in ROLE_CHAINS[role]
    assert "glm" not in ROLE_CHAINS["code"]
    assert ROLE_CHAINS["code"][0] == "codex"


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


# ---------------------------------------------------------------------------
# 31/07/2026 frozen-snapshot bearer lockout (PR #154)
# A bearer's unified gauges refresh ONLY from its own response headers, so a
# bearer that a gate turns off stops producing the samples that would turn it
# back on. Measured: one bearer sat status_5h=rejected for 33 min on a snapshot
# whose reset_5h had already passed; when finally re-probed its real util was
# 0.0. A reading past its own reset epoch describes a window that no longer
# exists and must not gate anything.
# ---------------------------------------------------------------------------


def test_bearer_usable_expired_rejected_window_is_not_locked() -> None:
    """``rejected`` + reset epoch in the PAST = stale reading, not a lock."""
    past = time.time() - 600
    assert bearer_usable({"limiter": {}, "unified": {"status_5h": "rejected", "reset_5h": past}})
    assert bearer_usable({"limiter": {}, "unified": {"status_7d": "rejected", "reset_7d": past}})


def test_bearer_usable_unexpired_rejected_window_still_locked() -> None:
    """The falsifier: a live window must keep gating. Only staleness unlocks."""
    future = time.time() + 600
    assert not bearer_usable(
        {"limiter": {}, "unified": {"status_5h": "rejected", "reset_5h": future}}
    )


def test_unified_live_view_drops_only_the_expired_window() -> None:
    """Per-window, not all-or-nothing: an expired 5h must not clear a live 7d."""
    now = time.time()
    live = unified_live_view(
        {
            "status_5h": "rejected",
            "util_5h": 1.0,
            "reset_5h": now - 600,
            "status_7d": "rejected",
            "util_7d": 1.0,
            "reset_7d": now + 600,
        },
        now=now,
    )
    assert "status_5h" not in live and "util_5h" not in live and "reset_5h" not in live
    assert live == {"status_7d": "rejected", "util_7d": 1.0, "reset_7d": now + 600}


def test_unified_live_view_keeps_windows_with_no_reset_epoch() -> None:
    """No reset epoch = nothing to expire against; never drop on a guess."""
    unified = {"status_5h": "rejected", "util_5h": 1.0}
    assert unified_live_view(unified, now=time.time()) == unified


def test_unified_live_view_does_not_mutate_the_caller_snapshot() -> None:
    """The argument is live shared state (``config.bearer_state``) — copy it."""
    now = time.time()
    unified = {"status_5h": "rejected", "reset_5h": now - 1}
    unified_live_view(unified, now=now)
    assert unified == {"status_5h": "rejected", "reset_5h": now - 1}


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


def test_lane_usable_no_bearers_proxy_owns_key_is_open() -> None:
    """A proxy-owns-key lane (Kimi :8767) with no per-client bearers is OPEN —
    it injects its own credential. Returning closed here had falsely skipped
    Kimi, forcing bulk onto GLM (which needs a client key the tabs lack)."""
    open_, detail = lane_usable({"upstream_egress_ok": True, "bearers": {}}, proxy_owns_key=True)
    assert open_ is True and detail == "no-bearers-proxy-owns-key"


def test_lane_usable_no_bearers_client_key_is_closed() -> None:
    """A client-provides-key lane (GLM :8766) with no bearers is CLOSED — no
    client token is routed through it, so traffic would 401. The targeted fix
    must NOT over-open client-key lanes (gate BLOCK on the first attempt)."""
    open_, detail = lane_usable({"upstream_egress_ok": True, "bearers": {}}, proxy_owns_key=False)
    assert open_ is False and detail == "no-bearers"
    # ...but a lane that can't reach its upstream stays closed regardless.
    open_, detail = lane_usable({"upstream_egress_ok": False, "bearers": {}}, proxy_owns_key=True)
    assert open_ is False and detail == "upstream-egress-down"


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


# ─── Spec 093 S5 — never-hard-fail / don't-silently-downgrade ───────────────


def test_effective_chain_generate_overflow_off_is_anthropic_only() -> None:
    """Invariant 6 (default, pre-kimi-k3 GA): generate must NOT spill to the cheap
    lanes — it would silently serve kimi-k2.6/GLM as Opus. Anthropic-only."""
    assert effective_chain("generate", overflow=False) == ("anthropic",)


def test_effective_chain_generate_overflow_on_is_full_chain() -> None:
    """Post-kimi-k3 GA (INGRESS_GENERATE_OVERFLOW=true): generate spills."""
    assert effective_chain("generate", overflow=True) == ("anthropic", "deepseek", "kimi", "glm")


def test_select_lane_generate_overflow_prefers_deepseek_over_kimi_glm() -> None:
    """#181: DeepSeek is verified tools-capable (unlike Kimi/GLM), so it leads
    the generate overflow chain right after Anthropic."""
    state = {
        "anthropic": _st(False),
        "deepseek": _st(True),
        "kimi": _st(True),
        "glm": _st(True),
    }
    assert select_lane("generate", state, overflow=True) == "deepseek"
    # deepseek closed too -> falls through to kimi
    state["deepseek"] = _st(False)
    assert select_lane("generate", state, overflow=True) == "kimi"


def test_effective_chain_bulk_judge_unaffected_by_overflow_flag() -> None:
    """Bulk/judge already run on the cheap lanes; the overflow flag is generate-only."""
    assert effective_chain("bulk", overflow=False) == ("deepseek", "kimi", "glm")
    assert effective_chain("judge", overflow=False) == ("anthropic", "glm", "kimi")


def test_select_lane_generate_overflow_off_holds_when_anthropic_capped() -> None:
    """Default (overflow off): Anthropic capped + Kimi/GLM wide open → generate
    HOLDs (None) rather than downgrading — invariant 6."""
    state = {"anthropic": _st(False), "kimi": _st(True), "glm": _st(True)}
    assert select_lane("generate", state, overflow=False) is None


def test_select_lane_bulk_still_served_when_anthropic_capped_invariant_1() -> None:
    """Invariant 1: bulk is served via Kimi/GLM even when Anthropic is capped —
    the overflow flag doesn't gate the cheap lanes."""
    state = {"anthropic": _st(False), "kimi": _st(True), "glm": _st(True)}
    assert select_lane("bulk", state, overflow=False) == "kimi"


def test_select_lane_judge_served_via_glm_when_anthropic_capped() -> None:
    """Judge spills Anthropic→GLM (overflow flag is generate-only; judge always spills)."""
    state = {"anthropic": _st(False), "kimi": _st(False), "glm": _st(True)}
    assert select_lane("judge", state, overflow=False) == "glm"


def test_role_from_header_spill_eligible_overrides() -> None:
    """Only the two spill-eligible roles are honored (downgrade-only)."""
    assert role_from_header("bulk") == "bulk"
    assert role_from_header("judge") == "judge"


def test_role_from_header_generate_claim_rejected_privilege_escalation() -> None:
    """GLM finding A: the header must NOT be able to CLAIM the premium generate
    lane — a client-set ``generate`` falls through to inference (None), so no
    local process can pin traffic onto the reserved Anthropic lane."""
    assert role_from_header("generate") is None


def test_role_from_header_case_and_whitespace_tolerant() -> None:
    assert role_from_header("  BULK ") == "bulk"


def test_role_from_header_absent_or_unknown_falls_back_to_none() -> None:
    """Absent/blank/unknown/lane-id → None so the caller falls back to model
    inference; the header can at worst downgrade its OWN request to a cheap lane."""
    assert role_from_header(None) is None
    assert role_from_header("") is None
    assert role_from_header("frontier") is None
    assert role_from_header("kimi") is None  # a lane id is not a role


def test_role_from_header_only_spill_eligible_roles() -> None:
    for r in ROLES:
        expected = r if r in {"bulk", "judge"} else None
        assert role_from_header(r) == expected


def test_role_header_bulk_routes_opus_id_to_kimi_1281_drift() -> None:
    """The #1281 case end-to-end at the routing layer: the fleet sends the
    opus-4-8[1m] id for a subagent-bulk request. infer_role maps it to generate
    (no spill), but the bulk header overrides → bulk chain → Kimi even with
    Anthropic capped and generate-overflow OFF."""
    assert infer_role("claude-opus-4-8[1m]") == "generate"  # inference alone
    role = role_from_header("bulk") or infer_role("claude-opus-4-8[1m]")
    assert role == "bulk"
    state = {"anthropic": _st(False), "kimi": _st(True), "glm": _st(True)}
    assert select_lane(role, state, overflow=False) == "kimi"


# ─── Agentic guard (nix w1W:p4 pre-flip gate) ───────────────────────────────


def test_body_has_tools_detects_tool_use() -> None:
    body = json.dumps(
        {
            "model": "claude-sonnet-4-6",
            "tools": [{"name": "get_time", "input_schema": {"type": "object"}}],
            "messages": [],
        }
    ).encode()
    assert body_has_tools(body) is True


def test_body_has_tools_false_for_simple_request() -> None:
    body = json.dumps({"model": "claude-sonnet-4-6", "messages": []}).encode()
    assert body_has_tools(body) is False


def test_body_has_tools_empty_tools_array_is_not_agentic() -> None:
    body = json.dumps({"model": "claude-sonnet-4-6", "tools": [], "messages": []}).encode()
    assert body_has_tools(body) is False


def test_body_has_tools_invalid_json_returns_false() -> None:
    assert body_has_tools(b"not json") is False
    assert body_has_tools(b"") is False


# ─── #182 "code" role — is_small_agentic_code classifier ────────────────────────────


def _agentic_body(*, max_tokens: int | None, tools: bool = True, filler: str = "") -> bytes:
    obj: dict = {"model": "claude-opus-5", "messages": [{"role": "user", "content": filler}]}
    if tools:
        obj["tools"] = [{"name": "get_time", "input_schema": {"type": "object"}}]
    if max_tokens is not None:
        obj["max_tokens"] = max_tokens
    return json.dumps(obj).encode()


def test_is_small_agentic_code_tools_small_max_tokens_is_code() -> None:
    assert is_small_agentic_code(_agentic_body(max_tokens=1024)) is True
    assert is_small_agentic_code(_agentic_body(max_tokens=CODE_MAX_TOKENS)) is True


def test_is_small_agentic_code_tools_large_max_tokens_is_not_code() -> None:
    """Tools + big max_tokens = a real generation, not a quick agentic turn —
    stays on the existing generate floor."""
    assert is_small_agentic_code(_agentic_body(max_tokens=CODE_MAX_TOKENS + 1)) is False
    assert is_small_agentic_code(_agentic_body(max_tokens=64_000)) is False


def test_is_small_agentic_code_no_tools_is_not_code() -> None:
    assert is_small_agentic_code(_agentic_body(max_tokens=1024, tools=False)) is False


def test_is_small_agentic_code_large_body_is_not_code() -> None:
    """A big body (large tool schema / long history) stays off the code lane
    even with a small max_tokens — CODE_MAX_BODY_BYTES is the other half of
    "small"."""
    big = _agentic_body(max_tokens=64, filler="x" * (CODE_MAX_BODY_BYTES + 1))
    assert len(big) > CODE_MAX_BODY_BYTES
    assert is_small_agentic_code(big) is False


def test_is_small_agentic_code_missing_or_invalid_max_tokens_is_not_code() -> None:
    assert is_small_agentic_code(_agentic_body(max_tokens=None)) is False
    assert is_small_agentic_code(_agentic_body(max_tokens=0)) is False
    assert is_small_agentic_code(_agentic_body(max_tokens=-1)) is False


def test_is_small_agentic_code_invalid_json_or_empty_is_not_code() -> None:
    assert is_small_agentic_code(b"not json") is False
    assert is_small_agentic_code(b"") is False


def test_code_role_chain_leads_with_codex() -> None:
    """#182: the code chain is class-based offload, not saturation spill —
    codex first (idle meter), anthropic/deepseek as fallback if codex is down.
    Not gated behind GENERATE_OVERFLOW (role != "generate")."""
    assert ROLE_CHAINS["code"] == ("codex", "anthropic", "deepseek")
    assert effective_chain("code", overflow=False) == ("codex", "anthropic", "deepseek")
    assert effective_chain("code", overflow=True) == ("codex", "anthropic", "deepseek")


def test_codex_lane_health_url_defaults_to_ccp_healthz() -> None:
    """06/08 health-404 finding: the CCP sidecar does not serve
    /__throttle/health; the codex lane must probe /healthz instead, or the
    lane never opens and code-role traffic never reaches it."""
    lanes = default_lanes()
    assert lanes["codex"].health_url == "http://127.0.0.1:8769/healthz"
    assert lanes["anthropic"].health_url == "http://127.0.0.1:8765/__throttle/health"
    assert lanes["deepseek"].health_url == "http://127.0.0.1:8768/__throttle/health"


def test_codex_code_model_unset_keeps_verbatim_passthrough(monkeypatch) -> None:
    """Default MUST be a no-op: the CCP sidecar's own claude-* -> gpt-* alias
    table decides. An accidental default here would silently redirect every
    code-role turn to a small tier."""
    monkeypatch.delenv("INGRESS_CODEX_CODE_MODEL", raising=False)
    assert routing.default_lanes()["codex"].models == {}


def test_codex_code_model_pins_the_egress_id(monkeypatch) -> None:
    """Spending the under-used Spark window (22 %/12 % at pace 0.56x/0.43x on
    15/08) requires pinning an egress id for the code role."""
    monkeypatch.setenv("INGRESS_CODEX_CODE_MODEL", "gpt-5.3-codex-spark")
    assert routing.default_lanes()["codex"].models == {"code": "gpt-5.3-codex-spark"}


def test_codex_code_model_blank_is_treated_as_unset(monkeypatch) -> None:
    """A blank/whitespace value must not remap to an empty model id, which the
    upstream would reject — same fail-safe shape as the lane-URL retirement."""
    monkeypatch.setenv("INGRESS_CODEX_CODE_MODEL", "")
    assert routing.default_lanes()["codex"].models == {}


def test_codex_pin_never_covers_judge_or_generate(monkeypatch) -> None:
    """Spark hallucinates on open-ended work: it may serve the bounded ``code``
    shape only, never eval/judge/done-state. No other role may be pinned here."""
    monkeypatch.setenv("INGRESS_CODEX_CODE_MODEL", "gpt-5.3-codex-spark")
    models = routing.default_lanes()["codex"].models
    assert set(models) == {"code"}
    assert "judge" not in models and "generate" not in models and "bulk" not in models


def test_select_lane_code_prefers_codex_then_anthropic_then_deepseek() -> None:
    state = {
        "codex": _st(True),
        "anthropic": _st(True),
        "deepseek": _st(True),
    }
    assert select_lane("code", state) == "codex"
    state["codex"] = _st(False)
    assert select_lane("code", state) == "anthropic"
    state["anthropic"] = _st(False)
    assert select_lane("code", state) == "deepseek"


def test_empty_lane_url_retires_the_lane(monkeypatch):
    """A cancelled subscription leaves the router by URL, not by code edit.

    z.ai was cancelled 31/07/2026 and Moonshot suspended, yet both lanes kept
    being probed every few seconds, rendered on the dashboard, and offered as
    spill targets that answer 401.
    """
    monkeypatch.setenv("INGRESS_KIMI_LANE_URL", "")
    monkeypatch.setenv("INGRESS_GLM_LANE_URL", "   ")
    lanes = routing.default_lanes()
    assert set(lanes) == {"anthropic", "deepseek", "codex"}


def test_unset_lane_url_keeps_the_default(monkeypatch):
    monkeypatch.delenv("INGRESS_KIMI_LANE_URL", raising=False)
    monkeypatch.delenv("INGRESS_GLM_LANE_URL", raising=False)
    monkeypatch.delenv("INGRESS_DEEPSEEK_LANE_URL", raising=False)
    monkeypatch.delenv("INGRESS_CODEX_LANE_URL", raising=False)
    assert set(routing.default_lanes()) == {"anthropic", "kimi", "glm", "deepseek", "codex"}


def test_a_retired_lane_can_never_be_selected(monkeypatch):
    """select_lane walks the chain against STATE; a retired lane has none."""
    monkeypatch.setenv("INGRESS_KIMI_LANE_URL", "")
    monkeypatch.setenv("INGRESS_GLM_LANE_URL", "")
    monkeypatch.setenv("INGRESS_DEEPSEEK_LANE_URL", "")
    lanes = routing.default_lanes()
    state = {name: routing.LaneState(True, 0.0) for name in lanes}  # every CONFIGURED lane open
    assert routing.select_lane("bulk", state) is None  # whole bulk chain retired
    assert routing.select_lane("generate", state, overflow=False) == "anthropic"


# ─── #204 code-role rejection reason (observability for the envelope) ───────────


def test_code_role_rejection_reason_none_when_it_qualifies() -> None:
    assert routing.code_role_rejection_reason(_agentic_body(max_tokens=1024)) is None


def test_code_role_rejection_reason_reports_the_live_failure_shape() -> None:
    """The measured 15/08 cause: 3483 of 3840 requests declared max_tokens=64000,
    15.6x the 4096 ceiling. That must be nameable, not journal-archaeology."""
    reason = routing.code_role_rejection_reason(_agentic_body(max_tokens=64000))
    assert reason == routing.CODE_REJECT_MAX_TOKENS_TOO_HIGH


def test_code_role_rejection_reason_covers_each_gate() -> None:
    r = routing.code_role_rejection_reason
    assert r(b"") == routing.CODE_REJECT_EMPTY
    assert r(_agentic_body(max_tokens=1024, tools=False)) == routing.CODE_REJECT_NO_TOOLS
    assert r(_agentic_body(max_tokens=None)) == routing.CODE_REJECT_MAX_TOKENS_ABSENT
    big = _agentic_body(max_tokens=1024, filler="x" * (routing.CODE_MAX_BODY_BYTES + 1))
    assert r(big) == routing.CODE_REJECT_BODY_TOO_LARGE


def test_code_role_rejection_reason_never_disagrees_with_the_predicate() -> None:
    """The predicate is DEFINED as `reason is None`; a second copy of the ladder
    would drift and the metric would explain a decision never made."""
    cases = [
        b"",
        b"{not json",
        _agentic_body(max_tokens=1024),
        _agentic_body(max_tokens=64000),
        _agentic_body(max_tokens=None),
        _agentic_body(max_tokens=1024, tools=False),
        _agentic_body(max_tokens=0),
        _agentic_body(max_tokens=routing.CODE_MAX_TOKENS),
        _agentic_body(max_tokens=1024, filler="x" * (routing.CODE_MAX_BODY_BYTES + 1)),
    ]
    for raw in cases:
        assert is_small_agentic_code(raw) is (routing.code_role_rejection_reason(raw) is None)


def test_code_role_rejection_labels_are_a_bounded_set() -> None:
    """Metric labels must never carry request content (unbounded cardinality)."""
    allowed = {
        routing.CODE_REJECT_EMPTY,
        routing.CODE_REJECT_BODY_TOO_LARGE,
        routing.CODE_REJECT_NO_TOOLS,
        routing.CODE_REJECT_UNPARSEABLE,
        routing.CODE_REJECT_MAX_TOKENS_ABSENT,
        routing.CODE_REJECT_MAX_TOKENS_TOO_HIGH,
    }
    probes = [
        b"",
        b"{not json",
        b"[]",
        _agentic_body(max_tokens=None),
        _agentic_body(max_tokens=64000),
        _agentic_body(max_tokens=-1),
        _agentic_body(max_tokens=True),
        _agentic_body(max_tokens=1024, tools=False),
    ]
    for raw in probes:
        reason = routing.code_role_rejection_reason(raw)
        assert reason is None or reason in allowed
