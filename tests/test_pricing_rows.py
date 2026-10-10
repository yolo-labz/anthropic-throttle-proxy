"""S2a — provider rate rows and the no-guess cost contract.

The delivered MiMo USD card (vault: "Active Routing S2 — MiMo PAYG key labels,
rate card and metering calibration (2026-10-09)") plus the bug it uncovered:
``_pricing_for`` fell back to OPUS rates for unknown models, so :8774 reported
``mimo-v2.6-flash`` at 107.1x its real input cost and 267.9x its real output
cost. Unknown models must now return NO rates and ``_record_usage`` must record
tokens but no USD for them.
"""

from __future__ import annotations

import json

from anthropic_throttle_proxy import metrics, pricing, proxy


def test_mimo_flash_row_matches_official_card() -> None:
    rates = pricing._pricing_for("mimo-v2.6-flash")
    assert rates == {
        "input": 0.14,
        "output": 0.28,
        "cache_read": 0.0028,
        "cache_creation": 0.00,
    }


def test_mimo_pro_row_matches_official_card() -> None:
    assert pricing._pricing_for("mimo-v2.6-pro") == {
        "input": 0.435,
        "output": 0.87,
        "cache_read": 0.0036,
        "cache_creation": 0.00,
    }


def test_ultraspeed_wins_over_pro_prefix() -> None:
    """Prefix order is load-bearing: `mimo-v2.6-pro:…` must not claim ultraspeed."""
    ultra = pricing._pricing_for("mimo-v2.6-pro-ultraspeed")
    assert ultra is not None and ultra["input"] == 4.35 and ultra["output"] == 8.70
    pro = pricing._pricing_for("mimo-v2.6-pro:high")
    assert pro is not None and pro["input"] == 0.435


def test_provider_prefix_and_case_drift_resolve() -> None:
    assert pricing._pricing_for("xiaomi/mimo-v2.6-flash") == pricing._pricing_for("mimo-v2.6-flash")
    assert pricing._pricing_for("MiMo-V2.6-Flash") == pricing._pricing_for("mimo-v2.6-flash")


def test_unknown_models_return_none_not_opus() -> None:
    for model in ("glm-5.3-flash", "deepseek-chat", "mimo-v2.5", "mimov2.6-flash", "", None):
        assert pricing._pricing_for(model) is None, model


def test_anthropic_rows_unchanged() -> None:
    opus = pricing._pricing_for("claude-opus-4-7[1m]")
    assert opus == {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_creation": 18.75}
    assert pricing._pricing_for("claude-haiku-4-5")["output"] == 5.0


def _counter(model: str, name: str, labels: dict[str, str]) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def test_record_usage_unknown_model_counts_tokens_but_no_usd() -> None:
    """Unknown model: tokens land in M_TOKENS; M_COST/M_SPEND stay untouched."""
    captured = bytearray(
        json.dumps(
            {"usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}}
        ).encode()
    )
    model = "unknown-pricing-model-xyz"
    before_cost = _counter(model, "anthropic_cost_usd_total", {"model": model, "kind": "input"})
    before_spend = _counter(
        model,
        "anthropic_spend_usd_total",
        {"lane": "test-lane", "seat": "test-seat", "model": model},
    )

    proxy._record_usage(
        model, model, captured, "v1/chat/completions", seat="test-seat", lane="test-lane"
    )

    assert (
        _counter(model, "anthropic_cost_usd_total", {"model": model, "kind": "input"})
        == before_cost
    )
    assert (
        _counter(
            model,
            "anthropic_spend_usd_total",
            {"lane": "test-lane", "seat": "test-seat", "model": model},
        )
        == before_spend
    )
    tokens = metrics.REGISTRY.get_sample_value(
        "anthropic_tokens_total", {"model": model, "kind": "input"}
    )
    assert tokens and tokens >= 1000


def test_record_usage_mimo_model_prices_from_the_card() -> None:
    """A priced MiMo model lands real (small) USD in M_COST and M_SPEND."""
    captured = bytearray(
        json.dumps({"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}}).encode()
    )
    model = "mimo-v2.6-flash-cost-contract"
    proxy._record_usage(model, model, captured, "v1/chat/completions", seat="seat-1", lane="lane-1")
    input_cost = metrics.REGISTRY.get_sample_value(
        "anthropic_cost_usd_total", {"model": model, "kind": "input"}
    )
    output_cost = metrics.REGISTRY.get_sample_value(
        "anthropic_cost_usd_total", {"model": model, "kind": "output"}
    )
    spend = metrics.REGISTRY.get_sample_value(
        "anthropic_spend_usd_total", {"lane": "lane-1", "seat": "seat-1", "model": model}
    )
    assert input_cost is not None and abs(input_cost - 0.14) < 1e-9
    assert output_cost is not None and abs(output_cost - 0.28) < 1e-9
    assert spend is not None and abs(spend - 0.42) < 1e-9
