"""Points observe mode (pace-option-draft §4.2): inert, published, never acting.

Drives the real proxy app against two credential-file accounts. The same scripted
traffic runs with ``POINTS_MODE=off`` and ``observe``; every observable answer must be
identical, even while the bucket says "would defer".
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
import test_seat_capacity_acceptance as seat
from prometheus_client import generate_latest
from test_seat_capacity_acceptance import _open_seat, _post

from anthropic_throttle_proxy import config, points, proxy
from anthropic_throttle_proxy.metrics import REGISTRY

_TAGS = ("t1", "t2", "t3", "t4")


pool = seat.pool  # the two-account harness fixture, registered here under its own name


@pytest.fixture
def lines(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(proxy, "log", seen.append)
    monkeypatch.setattr(proxy, "_points_book", points.Book())
    return seen


def _meter(pool, util: float) -> None:
    now = time.time()
    for bid in pool.bids.values():
        config.bearer_state.setdefault(bid, {}).update(
            unified={
                "status": "allowed",
                "util_5h": 0.1,
                "util_7d": util,
                "reset_7d": now + 3600.0,
            },
            unified_at=now,
        )


async def _script(pool):
    out = []
    for tag in _TAGS:
        status, body = await _post(pool.client, tag=tag)
        payload = json.loads(body)
        payload.pop("id", None)
        out.append((status, payload))
    return out


async def _run(pool, monkeypatch, mode: str):
    monkeypatch.setattr(config, "POINTS_MODE", mode)
    monkeypatch.setattr(config, "WEEKLY_PACING_MODE", "off")
    await _open_seat(pool, "A", live=3)
    await _open_seat(pool, "B", live=3)
    _meter(pool, 0.9999)  # 1 hour left, nothing left: every account is in debt
    proxy._points_book.bucket(pool.bids["A"]).credit = -5.0
    proxy._points_book.bucket(pool.bids["B"]).credit = -5.0
    return await _script(pool), dict(pool.upstream.count), list(pool.upstream.order)


async def test_observe_never_changes_an_answer_even_when_it_would_defer(
    pool,
    monkeypatch,
    lines,
):
    off = await _run(pool, monkeypatch, "off")
    assert proxy._points_book.view({}, time.time())["would_defer"] is False
    assert not [line for line in lines if line.startswith("points-")]

    pool.upstream.count.clear()
    pool.upstream.order.clear()
    observe = await _run(pool, monkeypatch, "observe")

    assert all(status == 200 for status, _ in observe[0])
    assert observe[0] == off[0]  # statuses and bodies, byte for byte in meaning
    assert observe[1] == off[1]  # the same upstream count per account
    assert [tag for _seat, tag in observe[2]] == [tag for _seat, tag in off[2]]  # same order
    view = proxy._points_book.view({}, time.time())
    assert view["would_defer"] is True  # the verdict was computed ...
    assert view["settled_total"] > 0  # ... and usage was debited ...
    transitions = [line for line in lines if line.startswith("points-would-defer")]
    assert (
        len(transitions) == 1 and "state=on" in transitions[0]
    )  # ... logged once, not per request
    assert "observe: nothing refused" in transitions[0]


async def test_observe_publishes_points_under_admission_and_metrics(pool, monkeypatch, lines):
    await _run(pool, monkeypatch, "observe")
    view = proxy._points_view(time.time())
    assert view["mode"] == "observe" and view["regime"] == "A"
    assert set(view["accounts"]) <= set(pool.bids.values())
    assert view["would_defer_total"] == {"all_in_debt": 1}
    assert view["unpriced_total"] == 0
    assert sum(view["settled_60m_by_model"].values()) == pytest.approx(view["settled_total"])
    text = generate_latest(REGISTRY).decode()
    assert "anthropic_points_settled_total" in text
    assert "anthropic_points_would_defer_total" in text


async def test_off_leaves_the_book_untouched(pool, monkeypatch, lines):
    await _run(pool, monkeypatch, "off")
    view = proxy._points_book.view({}, time.time())
    assert view["settled_total"] == 0 and view["would_defer"] is False


async def test_observe_releases_every_reservation_when_requests_end(pool, monkeypatch, lines):
    monkeypatch.setattr(config, "POINTS_MODE", "observe")
    await _open_seat(pool, "A", live=3)
    await _open_seat(pool, "B", live=3)
    _meter(pool, 0.2)  # healthy: plenty of credit, reservations are taken
    for tag in _TAGS:
        assert (await _post(pool.client, tag=tag))[0] == 200
    for _ in range(50):  # handler()'s finally runs a loop turn after the client has the body
        if all(b.reserved == 0.0 for b in proxy._points_book.buckets.values()):
            break
        await asyncio.sleep(0.02)
    assert all(b.reserved == 0.0 for b in proxy._points_book.buckets.values())
    assert proxy._points_book.view({}, time.time())["would_defer"] is False
    assert not any(line.startswith("points-would-defer") for line in lines)


async def test_no_meter_means_no_verdict_and_no_log(pool, monkeypatch, lines):
    monkeypatch.setattr(config, "POINTS_MODE", "observe")
    await _open_seat(pool, "A", live=3)
    await _open_seat(pool, "B", live=3)  # no unified reading at all
    assert (await _post(pool.client, tag="t1"))[0] == 200
    assert proxy._points_book.view({}, time.time())["would_defer"] is False
    assert not any(line.startswith("points-would-defer") for line in lines)


def test_points_mode_is_a_hot_tunable_off_observe_choice(monkeypatch):
    monkeypatch.setattr(config, "POINTS_MODE", "off")
    monkeypatch.setattr(config, "OVERRIDES_FILE", config.OVERRIDES_FILE.parent / "x-points.json")
    assert config.EDITABLE_KNOBS["points_mode"]["choices"] == ["off", "observe"]
    try:
        assert config.set_override("points_mode", " Observe ") == "observe"
        assert config.POINTS_MODE == "observe"
        with pytest.raises(ValueError):
            config.set_override("points_mode", "enforce")
        assert config.POINTS_MODE == "observe"
    finally:
        config.reset_override("points_mode")
