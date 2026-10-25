"""Per-project 5 h window share: attribution, pace state, advisory ordering.

Design: experiments/ai-stack-solver/research/allocator-ideation.md §3/§6/§9.
The properties pinned here are the ones the design says must never change:
GREEN is byte-for-byte the old dispatch, nothing is ever refused, ``_direct``
(Claude Code) is never reordered, and unknown numbers are never invented.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from anthropic_throttle_proxy import config, pricing, project_share, proxy
from anthropic_throttle_proxy.limiter import FairBearerLimiter
from anthropic_throttle_proxy.project_share import DIRECT, ProjectShare

T0 = 1_800_000_000.0
RESET = T0 + 3600.0  # window started at RESET - 5 h, i.e. 4 h before T0


def _share(tmp_path=None, *, started: float = T0 - 6 * 3600) -> ProjectShare:
    share = ProjectShare(weights_path=(tmp_path / "w.json") if tmp_path else None)
    share._started = started  # the process saw the whole window
    return share


def _meter(share: ProjectShare, t: float, u: float, *, reset: float = RESET, **week) -> None:
    unified = {"util_5h": u, "reset_5h": reset, **week}
    share.observe_meter(unified, now=t)


# --- attribution --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "slug"),
    [
        ("pi-upstream", "pi-upstream"),
        ("  NixOS ", "nixos"),
        ("home/notroot/Documents/Code/pi upstream", "home-notroot-documents-code-pi-upstream"),
        ("_direct", "direct"),  # a client cannot claim the exempt bucket
        ("", DIRECT),
        ("///", DIRECT),
        (None, DIRECT),
        ("x" * 200, "x" * 64),
    ],
)
def test_sanitize(raw, slug) -> None:
    assert project_share.sanitize(raw) == slug


def test_absent_header_is_direct() -> None:
    assert project_share.project_of({}) == DIRECT
    assert project_share.project_of({"x-anthropic-throttle-project": "nixos"}) == "nixos"


def test_project_header_is_never_forwarded_upstream() -> None:
    request = make_mocked_request(
        "POST",
        "/v1/messages",
        headers={"X-Anthropic-Throttle-Project": "nixos", "anthropic-version": "2023-06-01"},
    )
    forwarded = {k.lower() for k in proxy._forward_headers(request)}
    assert "x-anthropic-throttle-project" not in forwarded
    assert "anthropic-version" in forwarded


def test_record_usage_attributes_spend_to_the_project(monkeypatch) -> None:
    share = _share()
    monkeypatch.setattr(proxy, "_PROJECT_SHARE", share)
    body = json.dumps(
        {"usage": {"input_tokens": 1_000_000, "output_tokens": 0}, "model": "claude-opus-5-5"}
    ).encode()
    proxy._record_usage(
        "claude-opus-5-5", "claude-opus-5-5", bytearray(body), "v1/messages", project="nixos"
    )
    proxy._record_usage("claude-opus-5-5", "claude-opus-5-5", bytearray(body), "v1/messages")
    spent = {p: usd for _t, p, usd in share._spend}
    assert spent == {"nixos": pytest.approx(4.0)}  # no project kwarg -> not attributed


# --- pricing ------------------------------------------------------------------


def test_current_models_are_priced_and_prefixes_do_not_shadow() -> None:
    assert pricing._pricing_for("claude-opus-5-5")["input"] == 4.0
    assert pricing._pricing_for("claude-opus-5")["input"] == 5.0
    assert pricing._pricing_for("claude-sonnet-5-5")["cache_read"] == 0.10
    assert pricing._pricing_for("claude-sonnet-5")["cache_read"] == 0.20
    assert pricing._pricing_for("claude-fable-5-1")["cache_read"] == 0.25
    assert pricing._pricing_for("claude-haiku-5-5")["output"] == 0.50


def test_haiku_tier_applies_to_the_whole_request_above_100k_input() -> None:
    small = {"input": 50_000, "output": 10, "cache_read": 50_000, "cache_creation": 0}
    big = {"input": 50_000, "output": 10, "cache_read": 50_001, "cache_creation": 0}
    assert pricing.rates_for_usage("claude-haiku-5-5", small)["input"] == 0.10
    assert pricing.rates_for_usage("claude-haiku-5-5", big)["input"] == 0.50
    assert pricing.rates_for_usage("claude-opus-5-5", big)["input"] == 4.0  # no tier
    assert pricing.rates_for_usage("glm-5.3", big) is None


# --- pace state ---------------------------------------------------------------


def test_projection_needs_two_samples_a_minute_apart() -> None:
    share = _share()
    _meter(share, T0, 0.20)
    view = share.view(now=T0)
    assert view.util_5h == 0.20
    assert view.projected is None  # never a guess
    assert view.state == "GREEN"  # classified on u alone, honestly labelled û —


def test_projection_and_states() -> None:
    share = _share()
    _meter(share, T0, 0.40)
    _meter(share, T0 + 600, 0.50)  # +0.10 per 10 min, 50 min to reset → û = 1.0
    view = share.view(now=T0 + 600)
    assert view.projected == pytest.approx(0.50 + 0.10 / 600 * 3000)
    assert view.state == "RED"
    assert project_share._state(0.5, 0.75) == "AMBER"
    assert project_share._state(0.5, 0.70) == "GREEN"
    assert project_share._state(0.91, None) == "RED"
    assert project_share._state(None, None) == "UNKNOWN"


def test_falling_meter_does_not_project_backwards() -> None:
    share = _share()
    _meter(share, T0, 0.60)
    _meter(share, T0 + 600, 0.05)
    assert share.view(now=T0 + 600).projected == pytest.approx(0.05)


# --- shares -------------------------------------------------------------------


def test_fair_share_is_weighted_max_min_over_active_projects() -> None:
    share = _share()
    share.set_weight("nixos", 2)
    _meter(share, T0, 0.10)
    share.record_spend("nixos", 30.0, now=T0)
    share.record_spend("pi-upstream", 20.0, now=T0)
    share.record_spend(DIRECT, 0.0, now=T0)  # active, no spend
    view = share.view(now=T0)
    rows = {r.project: r for r in view.rows}
    assert view.capacity_usd == pytest.approx(500.0)  # 50 / 0.10
    assert rows["nixos"].entitlement == pytest.approx(0.9 * 2 / 4)
    assert rows["pi-upstream"].entitlement == pytest.approx(0.9 * 1 / 4)
    assert rows["nixos"].used == pytest.approx(0.06)
    assert not any(r.over for r in view.rows)


def test_no_capacity_without_having_seen_the_whole_window() -> None:
    share = _share(started=T0 - 600)  # restarted mid-window
    _meter(share, T0, 0.30)
    share.record_spend("nixos", 30.0, now=T0)
    view = share.view(now=T0)
    assert view.capacity_usd is None
    assert view.rows[0].used is None and not view.rows[0].over


def test_spend_before_the_window_does_not_count() -> None:
    share = _share()
    _meter(share, T0, 0.10)
    share.record_spend("nixos", 99.0, now=RESET - 5 * 3600 - 1)
    share.record_spend("nixos", 1.0, now=T0)
    assert share.view(now=T0).rows[0].usd == pytest.approx(1.0)


# --- advisory ordering --------------------------------------------------------


def _hot(share: ProjectShare) -> None:
    """AMBER/RED with nixos far over share and pi-upstream under."""
    share.record_spend("nixos", 80.0, now=T0)
    share.record_spend("pi-upstream", 1.0, now=T0)
    _meter(share, T0, 0.80)
    _meter(share, T0 + 600, 0.85)


def test_green_never_defers() -> None:
    share = _share()
    share.record_spend("nixos", 80.0, now=T0)
    share.record_spend("pi-upstream", 1.0, now=T0)
    # flat 0.65: GREEN (û 0.65 ≤ 0.70), yet nixos holds 0.64 of the window > g 0.45
    _meter(share, T0, 0.65)
    _meter(share, T0 + 600, 0.65)
    view = share.view(now=T0 + 600)
    assert view.state == "GREEN"
    assert [r.project for r in view.rows if r.over] == ["nixos"]
    assert not share.defer_active()
    assert not share.defer("nixos")


def test_hot_window_defers_only_over_share_and_never_direct() -> None:
    share = _share()
    share.record_spend(DIRECT, 50.0, now=T0)  # Claude Code, also over its share
    _hot(share)
    rows = {r.project: r for r in share.view(now=T0 + 600).rows}
    assert rows[DIRECT].over and rows["nixos"].over and not rows["pi-upstream"].over
    assert share.defer_active()
    assert share.defer("nixos")
    assert not share.defer("pi-upstream")
    assert not share.defer(DIRECT)
    assert not share.defer(None)


async def _dispatch_order(share: ProjectShare, monkeypatch) -> list[str]:
    from anthropic_throttle_proxy import limiter as limiter_mod

    monkeypatch.setattr(limiter_mod, "_PROJECT_SHARE", share)
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 0)
    lim = FairBearerLimiter(max_concurrent=1, queue_mode="fair", bearer_id="t")
    order: list[str] = []
    gate = asyncio.Event()

    async def hold() -> None:
        async with lim.slot("holder", project="pi-upstream"):
            await gate.wait()

    async def run(cid: str, project: str) -> None:
        async with lim.slot(cid, project=project):
            order.append(cid)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    waiters = [
        asyncio.create_task(run("over-1", "nixos")),
        asyncio.create_task(run("over-2", "nixos")),
        asyncio.create_task(run("under", "pi-upstream")),
    ]
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(asyncio.gather(holder, *waiters), timeout=5)
    return order


async def test_green_dispatch_is_plain_round_robin(monkeypatch) -> None:
    share = _share()
    _meter(share, T0, 0.10)
    assert await _dispatch_order(share, monkeypatch) == ["over-1", "over-2", "under"]


async def test_hot_dispatch_lets_under_share_go_first_and_refuses_nothing(monkeypatch) -> None:
    share = _share()
    _hot(share)
    order = await _dispatch_order(share, monkeypatch)
    assert order[0] == "under"
    assert sorted(order) == ["over-1", "over-2", "under"]  # every waiter served


# --- the only knob ------------------------------------------------------------


def test_weights_validate_and_persist(tmp_path) -> None:
    share = _share(tmp_path)
    assert share.weight("nixos") == 1
    share.set_weight("NixOS", 3)
    assert json.loads((tmp_path / "w.json").read_text()) == {"nixos": 3}
    assert _share(tmp_path).weight("nixos") == 3  # survives a restart
    share.set_weight("nixos", 1)  # default removes the entry
    assert json.loads((tmp_path / "w.json").read_text()) == {}
    with pytest.raises(ValueError):
        share.set_weight("nixos", 5)
    with pytest.raises(ValueError):
        share.set_weight("", 2)


# --- weekly row -----------------------------------------------------------------


def test_week_capacity_is_unknown_when_the_meter_falls_inside_one_epoch() -> None:
    share = _share(started=T0 - 8 * 86400)
    share.record_spend("nixos", 100.0, now=T0)
    week = {"util_7d": 0.61, "reset_7d": T0 + 86400}
    _meter(share, T0, 0.10, **week)
    assert share.view(now=T0).week_capacity_usd == pytest.approx(100.0 / 0.61)
    _meter(share, T0 + 60, 0.10, util_7d=0.05, reset_7d=T0 + 86400)
    view = share.view(now=T0 + 60)
    assert view.week_meter_regressed
    assert view.week_capacity_usd is None


# --- the card -------------------------------------------------------------------


async def _client(monkeypatch, share: ProjectShare) -> TestClient:
    from anthropic_throttle_proxy.ui import routes

    monkeypatch.setattr(routes._project_share, "SHARE", share)
    app = web.Application()
    routes.attach_ui(app)
    app.on_startup.clear()
    app.on_cleanup.clear()
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_card_renders_dashes_and_the_uncalibrated_label_with_no_data(monkeypatch) -> None:
    client = await _client(monkeypatch, _share())
    try:
        html = await (await client.get("/ui/projects")).text()
    finally:
        await client.close()
    assert "UNKNOWN" in html
    assert "capacity unknown — meter uncalibrated" in html
    assert "no Claude traffic seen in this window yet" in html
    assert "NaN" not in html and "None" not in html


async def test_card_post_sets_a_weight(monkeypatch, tmp_path) -> None:
    share = _share(tmp_path)
    _meter(share, T0, 0.2)
    share.record_spend("nixos", 5.0)
    client = await _client(monkeypatch, share)
    try:
        resp = await client.post("/ui/projects/weight", data={"project": "nixos", "weight": "4"})
        html = await resp.text()
        bad = await (
            await client.post("/ui/projects/weight", data={"project": "nixos", "weight": "9"})
        ).text()
    finally:
        await client.close()
    assert resp.status == 200
    assert "nixos → weight 4" in html
    assert share.weight("nixos") == 4
    assert "error" in bad and share.weight("nixos") == 4
