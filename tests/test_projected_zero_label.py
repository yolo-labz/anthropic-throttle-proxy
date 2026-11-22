"""B3: the zero shown after a recorded reset is a projection, never a measurement."""

from anthropic_throttle_proxy import accounts
from anthropic_throttle_proxy.routing import unified_live_view
from anthropic_throttle_proxy.ui import presentation, routes

NOW = 1_000_000.0
PAST = int(NOW - 100)  # reset recorded, no new sample since
FUTURE = int(NOW + 7200)


def test_window_view_after_reset_is_labelled_projected() -> None:
    w = accounts._window_view(0.97, PAST, "rejected", NOW)
    assert w["pct"] == 0
    assert w["projected"] is True
    assert w["measured_or_projected"] == "projected"


def test_window_view_live_is_labelled_measured() -> None:
    w = accounts._window_view(0.42, FUTURE, "allowed", NOW)
    assert w["projected"] is False
    assert w["measured_or_projected"] == "measured"


def test_window_meter_after_reset_carries_no_measured_percentage() -> None:
    meter = routes._window_meter("5h", accounts._window_view(0.97, PAST, "rejected", NOW))
    assert meter["projected"] is True
    assert meter["projected_pct"] == 0.0
    assert meter["pct"] is None  # nothing downstream may read the zero as measured
    assert "projected" in meter["note"]


def _row_with(window: dict) -> dict:
    return {"status": "ok", "meters": [routes._window_meter("5h", window)]}


def test_no_consumer_reads_projected_zero_as_measured() -> None:
    row = _row_with(accounts._window_view(0.97, PAST, "rejected", NOW))
    assert presentation.row_capacity_class(row) == "unknown"  # not "usable"
    assert routes._measured_headroom(row) is None  # not "0% headroom"
    assert routes._row_binding_pct(row) == -1.0  # not a 0% reading
    measured = _row_with(accounts._window_view(0.42, FUTURE, "allowed", NOW))
    assert presentation.row_capacity_class(measured) == "usable"
    assert routes._measured_headroom(measured) == 42


def test_unified_live_view_emits_no_zero_after_reset() -> None:
    live = unified_live_view({"util_5h": 0.97, "reset_5h": PAST, "status_5h": "rejected"}, now=NOW)
    assert "util_5h" not in live and "status_5h" not in live  # absent = unknown, never 0.0
