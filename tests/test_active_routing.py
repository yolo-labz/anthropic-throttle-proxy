"""Spec 337 — active routing decision logic (pure, no I/O)."""

from __future__ import annotations

import time

import pytest

from anthropic_throttle_proxy.active_routing import (
    OUTCOME_FATAL,
    OUTCOME_RETRYABLE,
    OUTCOME_SUCCESS,
    Candidate,
    CooldownTable,
    Gauge,
    RoutingPolicy,
    active_routing_enabled,
    plan,
    policy_from_env,
    rank,
    score,
    spend_row,
)

NOW = 1_760_000_000.0


def _c(lane: str, cred: str = "k", rank_: int = 0) -> Candidate:
    return Candidate(lane_id=lane, credential_id=cred, chain_rank=rank_)


class TestRanking:
    def test_capacity_dominates(self):
        policy = RoutingPolicy()
        a, b = _c("a", "k1"), _c("b", "k2")
        gauges = {
            ("a", "k1"): Gauge(util=0.9),
            ("b", "k2"): Gauge(util=0.1),
        }
        assert [c.lane_id for c in rank([a, b], gauges, policy, now=NOW)] == ["b", "a"]

    def test_cost_breaks_ties_only(self):
        policy = RoutingPolicy()
        cheap, pricey = _c("cheap", "k1"), _c("pricey", "k2")
        # Equal capacity + health → the cheaper candidate wins.
        gauges = {
            ("cheap", "k1"): Gauge(util=0.2, cost_per_mtok=10.0),
            ("pricey", "k2"): Gauge(util=0.2, cost_per_mtok=50.0),
        }
        assert rank([pricey, cheap], gauges, policy, now=NOW)[0].lane_id == "cheap"
        # ...but cost never overrules a hard capacity signal.
        gauges[("cheap", "k1")] = Gauge(util=0.95, cost_per_mtok=0.0)
        assert rank([pricey, cheap], gauges, policy, now=NOW)[0].lane_id == "pricey"

    def test_health_error_rate_demotes(self):
        policy = RoutingPolicy()
        healthy, sick = _c("healthy", "k1"), _c("sick", "k2")
        gauges = {
            ("healthy", "k1"): Gauge(util=0.5, error_rate=0.0),
            ("sick", "k2"): Gauge(util=0.0, error_rate=0.9),
        }
        assert rank([sick, healthy], gauges, policy, now=NOW)[0].lane_id == "healthy"

    def test_chain_rank_breaks_full_ties(self):
        policy = RoutingPolicy()
        first, second = _c("first", "k1", 0), _c("second", "k2", 1)
        assert rank([second, first], {}, policy, now=NOW)[0].lane_id == "first"

    def test_inflight_penalty(self):
        policy = RoutingPolicy()
        idle, busy = _c("idle", "k1"), _c("busy", "k2")
        gauges = {
            ("idle", "k1"): Gauge(util=0.5),
            ("busy", "k2"): Gauge(util=0.5, inflight=5),
        }
        assert rank([busy, idle], gauges, policy, now=NOW)[0].lane_id == "idle"


class TestCooldown:
    def test_retry_after_honoured(self):
        table = CooldownTable()
        c = _c("a")
        table.observe(c, OUTCOME_RETRYABLE, now=NOW, retry_after_s=30.0)
        assert table.cooling(c, NOW + 1.0) == 29.0
        assert table.cooling(c, NOW + 31.0) == 0.0

    def test_exponential_backoff_caps(self):
        policy = RoutingPolicy(cooldown_s=5.0, cooldown_max_s=20.0)
        table = CooldownTable()
        c = _c("a")
        table.observe(c, OUTCOME_FATAL, now=NOW, policy=policy)
        assert table.cooling(c, NOW + 0.1) == pytest.approx(4.9)
        table.observe(c, OUTCOME_FATAL, now=NOW + 0.1, policy=policy)
        assert table.cooling(c, NOW + 0.2) == pytest.approx(9.9)
        table.observe(c, OUTCOME_FATAL, now=NOW + 0.2, policy=policy)
        assert table.cooling(c, NOW + 0.3) == pytest.approx(19.9)  # capped at 20s window

    def test_success_clears_cooldown_and_pulls_ewma_down(self):
        table = CooldownTable()
        c = _c("a")
        table.observe(c, OUTCOME_RETRYABLE, now=NOW, retry_after_s=60.0)
        assert table.cooling(c, NOW + 1.0) > 0
        table.observe(c, OUTCOME_SUCCESS, now=NOW + 1.0)
        assert table.cooling(c, NOW + 1.0) == 0.0
        assert 0.0 < table.error_rate[("a", "k")] < 1.0

    def test_cooling_candidate_sorts_last_but_stays_reachable(self):
        policy = RoutingPolicy()
        table = CooldownTable()
        cooled, open_ = _c("cooled", "k1"), _c("open", "k2")
        table.observe(cooled, OUTCOME_RETRYABLE, now=NOW, retry_after_s=60.0)
        gauges = {
            ("cooled", "k1"): Gauge(util=0.0),
            ("open", "k2"): Gauge(util=0.9),
        }
        ordered = rank([cooled, open_], gauges, policy, now=NOW + 1.0, cooldowns=table)
        assert [c.lane_id for c in ordered] == ["open", "cooled"]
        assert score(gauges[("cooled", "k1")], policy, table.cooling(cooled, NOW + 1.0)) == -1.0


class TestPlan:
    def test_respects_tried_and_bounds_attempts(self):
        policy = RoutingPolicy(max_attempts=2)
        cands = [_c("a", "k1", 0), _c("b", "k2", 1), _c("c", "k3", 2)]
        got = plan(cands, {}, policy, now=NOW, already_tried={"a"})
        assert [c.lane_id for c in got] == ["b", "c"]
        policy3 = RoutingPolicy(max_attempts=1)
        assert len(plan(cands, {}, policy3, now=NOW)) == 1

    def test_empty_when_all_tried(self):
        policy = RoutingPolicy()
        cands = [_c("a"), _c("b")]
        assert plan(cands, {}, policy, now=NOW, already_tried={"a", "b"}) == []


class TestPolicyEnv:
    def test_disabled_by_default(self):
        assert not active_routing_enabled({})
        assert active_routing_enabled({"INGRESS_ACTIVE_ROUTING": "on"})
        assert active_routing_enabled({"INGRESS_ACTIVE_ROUTING": "ON "})  # stripped + casefolded
        assert active_routing_enabled({"INGRESS_ACTIVE_ROUTING": " ON"})

    def test_parse_bounds_and_bad_values(self):
        policy = policy_from_env(
            {
                "INGRESS_ACTIVE_ROUTING_MAX_ATTEMPTS": "4",
                "INGRESS_ACTIVE_ROUTING_W_CAPACITY": "not-a-float",
            }
        )
        assert policy.max_attempts == 4
        assert policy.w_capacity == 0.5  # bad value → default, never a crash

    def test_zero_weights_rejected(self):
        try:
            RoutingPolicy(w_capacity=0.0, w_health=0.0, w_cost=0.0)
        except ValueError:
            pass
        else:
            raise AssertionError("zero-sum weights must be rejected")


class TestSpendRow:
    def test_row_fields(self):
        row = spend_row(
            lane="glm",
            seat="",
            role="bulk",
            model="glm-5.2",
            attempts=2,
            latency_ms=123.5,
            input_tokens=10,
            output_tokens=5,
            usd=0.002,
            now=NOW,
        )
        assert row.seat == "unknown"
        assert row.attempts == 2
        assert row.ts == NOW
        assert row.usd == 0.002

    def test_defaults_to_now(self):
        before = time.time()
        row = spend_row(lane="a", seat="s", role="r", model="m", attempts=0, latency_ms=0)
        assert row.attempts == 1
        assert before <= row.ts <= time.time()


class TestIngressSeam:
    def test_active_pick_off_is_legacy(self, monkeypatch):
        monkeypatch.delenv("INGRESS_ACTIVE_ROUTING", raising=False)
        from anthropic_throttle_proxy import ingress

        assert ingress._active_pick("generate", set()) is None
        assert ingress._active_pick("generate", {"anthropic"}) is None

    def test_active_pick_demotes_cooled_and_skips_tried(self, monkeypatch):
        monkeypatch.setenv("INGRESS_ACTIVE_ROUTING", "on")
        from anthropic_throttle_proxy import ingress, routing

        role, chain = max(
            (
                (r, list(ingress._effective_chain(r)))
                for r in ("bulk", "judge", "code", "generate", "unknown")
            ),
            key=lambda item: len(item[1]),
        )
        assert len(chain) >= 2, "need a role chain with a failover target"
        monkeypatch.setattr(
            ingress,
            "lane_state",
            {lid: routing.LaneState(open=True, checked_at=NOW) for lid in chain},
        )
        saved = ingress._active_cooldowns
        try:
            ingress._active_cooldowns = CooldownTable()
            # Cooled first-choice demotes to the next chain candidate.
            ingress._active_cooldowns.observe(
                Candidate(lane_id=chain[0]),
                OUTCOME_RETRYABLE,
                now=time.time(),
                retry_after_s=30.0,
            )
            assert ingress._active_pick(role, set()) == chain[1]
            # ``tried`` lanes are never re-proposed.
            assert ingress._active_pick(role, set(chain)) is None
        finally:
            ingress._active_cooldowns = saved

    def test_active_observe_noop_when_off(self, monkeypatch):
        monkeypatch.delenv("INGRESS_ACTIVE_ROUTING", raising=False)
        from anthropic_throttle_proxy import ingress

        saved = ingress._active_cooldowns
        try:
            ingress._active_cooldowns = CooldownTable()
            ingress._active_observe("glm", OUTCOME_RETRYABLE, 30.0)
            assert ingress._active_cooldowns.until == {}
        finally:
            ingress._active_cooldowns = saved

    def test_retry_after_seconds_parses_numeric_only(self):
        from anthropic_throttle_proxy import ingress

        class _Resp:
            def __init__(self, value):
                self.headers = {"Retry-After": value}

        assert ingress._retry_after_seconds(_Resp("12")) == 12.0
        assert ingress._retry_after_seconds(_Resp("Wed, 21 Oct 2026 07:28:00 GMT")) is None
        assert ingress._retry_after_seconds(_Resp("")) is None
        assert ingress._retry_after_seconds(_Resp("-3")) is None

    def test_burst_selection_spreads_and_never_blocks(self, monkeypatch):
        """Burst proof (spec 337 measurement): a rapid burst of selections with a
        pushed-back chain head never picks the cooled lane and never queues —
        every call returns immediately, in-memory (no waiting on any lane)."""
        monkeypatch.setenv("INGRESS_ACTIVE_ROUTING", "on")
        from anthropic_throttle_proxy import ingress, routing

        chain = list(ingress._effective_chain("bulk"))
        assert len(chain) >= 2
        monkeypatch.setattr(
            ingress,
            "lane_state",
            {lid: routing.LaneState(open=True, checked_at=NOW) for lid in chain},
        )
        saved = ingress._active_cooldowns
        try:
            ingress._active_cooldowns = CooldownTable()
            started = time.perf_counter()
            picks = []
            for i in range(30):
                if i % 5 == 0:
                    # Upstream pushback lands mid-burst: the lane cools down.
                    ingress._active_cooldowns.observe(
                        Candidate(lane_id=chain[0]),
                        OUTCOME_RETRYABLE,
                        now=time.time(),
                        retry_after_s=30.0,
                    )
                picks.append(ingress._active_pick("bulk", set()))
            elapsed = time.perf_counter() - started
            assert all(p == chain[1] for p in picks)
            assert elapsed < 1.0  # 30 in-memory selections: no stall possible here
        finally:
            ingress._active_cooldowns = saved
