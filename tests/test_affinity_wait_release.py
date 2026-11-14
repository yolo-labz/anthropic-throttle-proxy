"""A full pinned account keeps its session unless its measured wait is long.

Throttler 11/11/2026 (row affinity-saturation-hops): at 20/20 on both accounts a
session hopped whenever its pinned account was momentarily full and the sibling
had one free slot, re-writing its prompt cache to dodge a median 3.3 s wait.
Drives the real routing seam with two offline credentials, like
``test_session_affinity``, whose fixture this module reuses.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import test_session_affinity as _sibling

from anthropic_throttle_proxy import config, proxy
from anthropic_throttle_proxy import limiter as _limiter
from anthropic_throttle_proxy import session_affinity as sa

pool = _sibling.pool
_body = _sibling._body
_route = _sibling._route


def _full_pinned(pool, monkeypatch: pytest.MonkeyPatch, *, samples: list[float]):
    """Pin a session, then fill its account (20/20) while the sibling stays idle."""
    body = _body()
    first = _route(pool, body)
    other = "B" if first == "A" else "A"
    monkeypatch.setattr(config, "MAX_CONCURRENT", 20)
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 240.0)  # the desktop gateway's bound
    lims = {}
    for label in "AB":
        lim = _limiter.FairBearerLimiter(20, "fair", bearer_id=pool[label])
        lim.max_concurrent = 20  # AIMD-grown live cap, as on the desktop gateway
        config.bearer_limiters[pool[label]] = lim
        _limiter.clear_retry_probe(pool[label])  # both accounts already serve traffic
        lims[label] = lim
    lims[first].inflight = 20
    lims[first]._samples.extend(samples)  # completed service times, seconds
    assert proxy._bearer_would_queue(pool[first])
    assert not proxy._bearer_would_queue(pool[other])
    return body, first, other, lims


def test_short_measured_wait_keeps_the_session_on_its_full_account(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    body, first, _other, lims = _full_pinned(pool, monkeypatch, samples=[2.0] * 3)
    estimate = lims[first].drain_estimate()
    assert estimate.evidenced and estimate.wait_s <= 60.0
    assert _route(pool, body) == first
    assert sa.stats()["migrated"] == 0


def test_long_measured_wait_releases_the_pin(pool, monkeypatch: pytest.MonkeyPatch) -> None:
    body, first, other, lims = _full_pinned(pool, monkeypatch, samples=[300.0] * 3)
    assert lims[first].drain_estimate().wait_s > 60.0
    assert _route(pool, body) == other
    assert sa.stats()["migrated"] == 1


def test_a_guessed_wait_keeps_release_on_queue(pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cold limiter has no completions: its wait is a guess and must not hold a session."""
    body, first, other, lims = _full_pinned(pool, monkeypatch, samples=[])
    assert not lims[first].drain_estimate().evidenced
    assert _route(pool, body) == other


@pytest.mark.parametrize(("wait_s", "hops"), [(59.9, False), (60.0, False), (60.1, True)])
def test_only_a_wait_over_sixty_seconds_releases(
    pool, monkeypatch: pytest.MonkeyPatch, wait_s: float, hops: bool
) -> None:
    body, first, other, lims = _full_pinned(pool, monkeypatch, samples=[2.0] * 3)
    estimate = SimpleNamespace(evidenced=True, rejects=False, wait_s=wait_s)
    monkeypatch.setattr(lims[first], "drain_estimate", lambda *a, **k: estimate)
    assert _route(pool, body) == (other if hops else first)


def test_a_wait_admission_would_refuse_moves_the_session(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the default 30 s queue bound a 45 s wait is a 503, not a wait: hop instead."""
    body, first, other, lims = _full_pinned(pool, monkeypatch, samples=[45.0] * 3)
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 30.0)
    estimate = lims[first].drain_estimate()
    assert estimate.rejects and estimate.wait_s <= 60.0
    assert _route(pool, body) == other


def test_a_sibling_that_would_queue_too_never_takes_the_session(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    body, first, other, lims = _full_pinned(pool, monkeypatch, samples=[300.0] * 3)
    lims[other].inflight = 20
    assert _route(pool, body) == first


@pytest.mark.parametrize("reason", ["retry_after", "dead", "warn"])
def test_strict_gate_and_retry_after_still_release_on_a_short_wait(
    pool, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    body, first, other, _lims = _full_pinned(pool, monkeypatch, samples=[2.0] * 3)
    bad = pool[first]
    if reason == "retry_after":
        monkeypatch.setattr(
            proxy, "_bearer_retry_after_remaining", lambda bid: 60.0 if bid == bad else 0.0
        )
    elif reason == "dead":
        monkeypatch.setattr(proxy, "_bearer_credential_dead", lambda bid: bid == bad)
    else:
        config.bearer_state[bad] = {
            "unified": {"status": "allowed_warning", "util_5h": 0.9, "reset_5h": 9e12}
        }
    assert _route(pool, body) == other
    assert sa.stats()["migrated"] == 1


def test_pin_is_not_kept_for_a_request_admission_will_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1 (p2S adversarial read of PR #56): judge the wait against THIS request's budget.

    The configured bound admits a 20 s wait, but a request that reaches this tier
    with 5 s of inherited budget is refused pre-queue on the pinned account, and a
    refused request is answered 503 with no re-route: the pin must not be kept.
    """
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 30.0)
    lim = _limiter.FairBearerLimiter(1, "fair")
    lim.max_concurrent = 1
    lim.inflight = 1  # the pinned account is full, so a new request queues
    lim._samples.extend([20.0, 20.0, 20.0])  # evidenced: about 20 s per request
    monkeypatch.setitem(config.bearer_limiters, "pinnedbid", lim)
    configured = lim.drain_estimate()
    assert configured.evidenced and not configured.rejects and configured.wait_s <= 60.0
    assert lim.drain_estimate(5.0).rejects
    assert not proxy._pinned_wait_worth_a_hop("pinnedbid")  # the configured bound: keep
    assert proxy._pinned_wait_worth_a_hop("pinnedbid", max_wait=5.0)  # a 5 s budget: hop
    assert not proxy._pinned_wait_worth_a_hop("pinnedbid", max_wait=30.0)


def test_the_routing_seam_hops_on_a_tight_request_budget(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget reaches the pin verdict through ``_route_account_if_enabled``."""
    body, first, other, _lims = _full_pinned(pool, monkeypatch, samples=[20.0] * 3)
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 30.0)
    assert _route(pool, body) == first  # no budget: today's PR #56 behaviour
    sa.begin_request(body)
    _bid, label = proxy._route_account_if_enabled(
        {"authorization": "Bearer sk-ant-oat01-SIM-A"},
        pool["A"],
        method="POST",
        path="v1/messages",
        max_tokens=64,
        wait_budget_s=5.0,
    )
    assert label == other
    assert sa.stats()["migrated"] == 1


def test_claim_route_reads_the_inherited_wait_budget_header(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_claim_route`` derives the budget from the request's own wait-budget header."""
    from aiohttp.test_utils import make_mocked_request

    body, first, other, _lims = _full_pinned(pool, monkeypatch, samples=[20.0] * 3)
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 30.0)

    def claim(budget_ms: str | None) -> str:
        headers = {} if budget_ms is None else {config.WAIT_BUDGET_HEADER: budget_ms}
        request = make_mocked_request("POST", "/v1/messages", headers=headers)
        sa.begin_request(body)
        _bid, _base, _lease = proxy._claim_route(
            request,
            "v1/messages",
            {"authorization": "Bearer sk-ant-oat01-SIM-A"},
            pool["A"],
            "",
            64,
            "direct",
        )
        return next(label for label, bid in pool.items() if bid == _bid)

    assert claim(None) == first
    assert claim("5000") == other
