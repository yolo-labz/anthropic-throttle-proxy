"""T003 foundation — pure scope authority (spec 245). Synthetic operator config only.

Every case is hermetic: already-parsed mapping rows, no files, no network, no
runtime flags. What is pinned here is the AUTHORITY boundary — only trusted
operator configuration may bind a dispatch to a canonical ``(upstream, account,
model)`` scope, and everything else answers a distinct UnknownScope — plus the
rejection classes the contract requires (ambiguous duplicates, inconsistent
budgets for a shared scope, invalid types/bools/nonfinite values, unknown
source/endpoint/model). Strict/observe/off handling belongs to a future caller.
"""

from __future__ import annotations

import pytest

from anthropic_throttle_proxy import prospective_scope as ps
from anthropic_throttle_proxy.ledger import Budgets

SOURCES = ("team-b", "plan")
ENDPOINTS = ("http://127.0.0.1:8773", "http://127.0.0.1:8774")
MODELS = ("mimo-v2.6", "mimo-v2.6-pro", "mimo-v2.5-pro")


def _row(**over) -> dict:
    row = {
        "source": "team-b",
        "endpoint": "http://127.0.0.1:8773",
        "alias": "mimo-v2.6",
        "upstream": "mimo",
        "account": "team-b",
        "model": "mimo-v2.6-pro",
        "budgets": {"max_requests": 2, "max_tokens": 100},
        "output_default": 4096,
    }
    row.update(over)
    return row


def _resolver(*rows, sources=SOURCES, endpoints=ENDPOINTS, models=MODELS):
    return ps.ScopeResolver(rows, sources=sources, endpoints=endpoints, models=models)


# ── positive: exact configured triples bind to canonical scopes ──────────────


def test_resolves_trusted_triple_to_canonical_scope():
    match = _resolver(_row()).resolve("team-b", "http://127.0.0.1:8773", "mimo-v2.6")
    assert isinstance(match, ps.ScopeMatch)
    assert match.scope == ps.Scope("mimo", "team-b", "mimo-v2.6-pro")
    assert match.scope.key == ("mimo", "team-b", "mimo-v2.6-pro")
    assert match.budgets == Budgets(2, 100)
    assert match.output_default == 4096


def test_alias_and_canonical_id_resolve_to_the_same_configured_default():
    """Two spellings of one model share ONE configured default — the alias must
    never resolve to a different budget or output bound than its canonical id."""
    resolver = _resolver(
        _row(),
        _row(alias="mimo-v2.6-pro"),
    )
    via_alias = resolver.resolve("team-b", "http://127.0.0.1:8773", "mimo-v2.6")
    via_canonical = resolver.resolve("team-b", "http://127.0.0.1:8773", "mimo-v2.6-pro")
    assert via_alias == via_canonical


def test_output_default_is_optional():
    row = _row()
    del row["output_default"]
    match = _resolver(row).resolve("team-b", "http://127.0.0.1:8773", "mimo-v2.6")
    assert isinstance(match, ps.ScopeMatch)
    assert match.output_default is None


def test_budgets_instance_is_reused_verbatim():
    budgets = Budgets(3, 250)
    match = _resolver(_row(budgets=budgets)).resolve("team-b", "http://127.0.0.1:8773", "mimo-v2.6")
    assert isinstance(match, ps.ScopeMatch)
    assert match.budgets is budgets


# ── unmatched dispatch answers UnknownScope, distinctly ─────────────────────


def test_unknown_scope_is_distinct_for_unmatched_dispatch():
    resolver = _resolver(_row())
    # A bearer hash is not authority: it identifies a credential, not an account.
    assert resolver.resolve("b144f62f", "http://127.0.0.1:8773", "mimo-v2.6") == (
        ps.UnknownScope("unknown-source")
    )
    # Exact endpoints only: a trailing slash is a different endpoint.
    assert resolver.resolve("team-b", "http://127.0.0.1:8773/", "mimo-v2.6") == (
        ps.UnknownScope("unknown-endpoint")
    )
    assert resolver.resolve("team-b", "http://127.0.0.1:8773", "absent-model") == (
        ps.UnknownScope("unknown-model")
    )
    # Everything known, but no configured mapping: still unknown, never guessed.
    other = _resolver(_row(), _row(source="plan", alias="mimo-v2.5-pro", model="mimo-v2.5-pro"))
    assert other.resolve("plan", "http://127.0.0.1:8773", "mimo-v2.6") == ps.UnknownScope(
        "unmapped"
    )
    for answer in (
        resolver.resolve("b144f62f", "http://127.0.0.1:8773", "mimo-v2.6"),
        resolver.resolve("team-b", "http://127.0.0.1:8773", "mimo-v2.5-pro"),
    ):
        assert isinstance(answer, ps.UnknownScope)
        assert not isinstance(answer, ps.ScopeMatch)


def test_invalid_dispatch_input_is_unknown_not_an_exception():
    resolver = _resolver(_row())
    assert resolver.resolve(5, "http://127.0.0.1:8773", "mimo-v2.6") == ps.UnknownScope(
        "invalid-input"
    )
    assert resolver.resolve("team-b", True, "mimo-v2.6") == ps.UnknownScope("invalid-input")
    assert resolver.resolve("team-b", "http://127.0.0.1:8773", "") == ps.UnknownScope(
        "invalid-input"
    )


# ── constructor rejections ──────────────────────────────────────────────────


def test_duplicate_triple_mapping_is_rejected_as_ambiguous():
    with pytest.raises(ValueError, match="ambiguous duplicate"):
        _resolver(_row(), _row(model="mimo-v2.5-pro"))


def test_inconsistent_budgets_for_shared_scope_are_rejected():
    with pytest.raises(ValueError, match="inconsistent budgets"):
        _resolver(_row(), _row(alias="mimo-v2.6-pro", budgets={"max_requests": 9, "max_tokens": 1}))


def test_inconsistent_output_default_for_shared_scope_is_rejected():
    with pytest.raises(ValueError, match="inconsistent budgets"):
        _resolver(_row(), _row(alias="mimo-v2.6-pro", output_default=8192))


def test_same_alias_at_distinct_provider_endpoints_is_unambiguous():
    resolver = _resolver(
        _row(alias="default"),
        _row(
            source="plan",
            endpoint=ENDPOINTS[1],
            alias="default",
            upstream="other-provider",
            account="plan",
            model="mimo-v2.5-pro",
        ),
        models=(*MODELS, "default"),
    )
    first = resolver.resolve("team-b", ENDPOINTS[0], "default")
    second = resolver.resolve("plan", ENDPOINTS[1], "default")
    assert first.scope.key == ("mimo", "team-b", "mimo-v2.6-pro")
    assert second.scope.key == ("other-provider", "plan", "mimo-v2.5-pro")
    assert resolver.resolve("plan", ENDPOINTS[0], "default") == ps.UnknownScope("unmapped")


@pytest.mark.parametrize(
    "row_over",
    [
        {"source": 5},
        {"alias": True},
        {"endpoint": ""},
        {"upstream": None},
        {"account": 3.5},
        {"model": ""},
    ],
)
def test_invalid_field_types_are_rejected(row_over):
    with pytest.raises(ValueError, match="non-empty strings"):
        _resolver(_row(**row_over))


@pytest.mark.parametrize(
    "budgets",
    [
        {"max_requests": True, "max_tokens": 100},
        {"max_requests": 2, "max_tokens": float("nan")},
        {"max_requests": 2, "max_tokens": float("inf")},
        {"max_requests": 2.5, "max_tokens": 100},
        {"max_requests": 0, "max_tokens": 100},
        {"max_requests": -1, "max_tokens": 100},
        {"max_requests": 2},
        {"max_requests": 2, "max_tokens": 100, "extra": 1},
        "not-a-mapping",
    ],
)
def test_invalid_budgets_are_rejected(budgets):
    with pytest.raises(ValueError, match="budgets"):
        _resolver(_row(budgets=budgets))


@pytest.mark.parametrize("output_default", [True, 0, -5, 2.5, float("nan"), float("inf")])
def test_invalid_output_default_is_rejected(output_default):
    with pytest.raises(ValueError, match="output_default"):
        _resolver(_row(output_default=output_default))


@pytest.mark.parametrize(
    "row_over",
    [
        {"source": "ghost"},
        {"endpoint": "http://127.0.0.1:9999"},
        {"model": "mimo-v9-pro"},
        {"alias": "mimo-v9"},
    ],
)
def test_unknown_source_endpoint_or_model_is_rejected_at_build(row_over):
    with pytest.raises(ValueError, match="unknown"):
        _resolver(_row(**row_over))


def test_non_mapping_row_is_rejected():
    with pytest.raises(ValueError, match="mapping"):
        _resolver("team-b=mimo:team-b")


@pytest.mark.parametrize("model", ["mimo-v2.5-pro", "mimo-v2.6"])
def test_canonical_target_cannot_be_remapped_to_a_chain_or_cycle(model):
    with pytest.raises(ValueError, match="canonical"):
        _resolver(_row(), _row(alias="mimo-v2.6-pro", model=model))


def test_aliases_cannot_split_one_selected_account_and_model_budget():
    with pytest.raises(ValueError, match="canonical"):
        _resolver(_row(), _row(alias="mimo-v2.6-pro", account="other"))


@pytest.mark.parametrize("field", ["upstream", "account", "model"])
def test_blank_canonical_fields_never_reach_the_ledger(field):
    with pytest.raises(ValueError, match="non-empty"):
        _resolver(_row(**{field: " \t "}), models=(*MODELS, " \t "))
