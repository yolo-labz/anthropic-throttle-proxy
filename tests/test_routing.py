"""Spec 093 S2 — role inference from the request model.

Acceptance (S2): per-tier tests for generate / judge / bulk, and the subagent
slot (``claude-sonnet-4-6``) routes to bulk. Covers the model-tier table in
``routing.py`` plus the ``#1281`` drift (deployed subagent slot is
``claude-opus-4-8[1m]`` → ``generate``, documented for S7 reconciliation).
"""

from __future__ import annotations

import json

import pytest

from anthropic_throttle_proxy.routing import ROLES, infer_role, infer_role_from_body


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
