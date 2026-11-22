"""Row affinity-saturation-hops: the hop pricer on a synthetic journal + ledger."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[1] / "scripts/token-usage-lake/analysis/prefix_rebuilds/hops.py"
_spec = importlib.util.spec_from_file_location("affinity_hops", _PATH)
hp = importlib.util.module_from_spec(_spec)
sys.modules["affinity_hops"] = hp
_spec.loader.exec_module(hp)

A, B = "aaaa1111", "bbbb2222"
D = "2026-11-11T10:00:"


def _j(sec: str, text: str) -> str:
    return f"{D}{sec}-03:00 desktop throttler-gateway[1]: [anthropic-throttle] {text}"


def _req(sec, bid, cid, bytes_, *, migrate=None, inflight=38):
    lines = [_j(sec, f"body_passthrough bid=api-key model=m bytes={bytes_} reason=under-cap")]
    if migrate:
        lines.append(
            _j(sec, f"affinity-migrate session={migrate} from={A if bid == B else B} to={bid}")
        )
    lines.append(_j(sec, f"account-route from=api-key to={bid} label=x"))
    lines.append(
        _j(
            sec,
            f"queue+ method=POST path=/v1/messages bid={bid} cid={cid} "
            f"inflight={inflight} queued=0",
        )
    )
    return lines


def _done(sec, bid, cid, elapsed_ms):
    return _j(
        sec,
        f"done   method=POST path=/v1/messages status=200 bid={bid} cid={cid} "
        f"via=direct model=m elapsed_ms={elapsed_ms} project=p seat=s inflight=1",
    )


JOURNAL = [
    _j("00.000000", f"bearer-new bid={A} max_concurrent=1 hard_max=20 queue_mode=fair"),
    _j("00.000000", f"bearer-new bid={B} max_concurrent=1 hard_max=20 queue_mode=fair"),
    *_req("01.000000", A, "c1", 900_000),  # holds A's only slot from 01 to 21
    *_req("05.000000", B, "c2", 300_000, migrate="s1"),  # hop at 05: A full
    _done("09.000000", B, "c2", 4000),
    _done("21.000000", A, "c1", 20000),  # A's next release: the hop avoided 16 s
    *_req("30.000000", A, "c3", 300_000, migrate="s1"),  # s1 returns A (B -> A)
    _done("31.000000", A, "c3", 1000),
]

T0 = hp.epoch(D + "00-03:00")
LEDGER = [  # s1: warm on A, rebuilt on B, then back on A within the TTL (cache still warm)
    {
        "epoch": int(T0) - 60,
        "bearer": A,
        "model": "m",
        "session": "s1",
        "input": 5,
        "cacheRead": 90_000,
        "cacheWrite": 1_000,
        "output": 300,
        "usage_complete": True,
    },
    {
        "epoch": int(T0) + 9,
        "bearer": B,
        "model": "m",
        "session": "s1",
        "input": 5,
        "cacheRead": 20_000,
        "cacheWrite": 72_000,
        "output": 300,
        "usage_complete": True,
    },
    {
        "epoch": int(T0) + 31,
        "bearer": A,
        "model": "m",
        "session": "s1",
        "input": 5,
        "cacheRead": 91_000,
        "cacheWrite": 1_500,
        "output": 300,
        "usage_complete": True,
    },
]
PRICES = {"m": (5.0, 0.2)}


def _build(estimate=lambda holds, finished, cap, parked, clients, now: (12.0, True)):
    return hp.build(
        JOURNAL, LEDGER, PRICES, since=T0, until=T0 + 3600, cut=T0 + 3600, estimator=estimate
    )


def test_hop_joins_its_own_session_call_and_prices_the_rewrite() -> None:
    first, back = _build()["hops"]
    assert first["hop_tok"]["cache_write"] == 72_000
    # previous prompt 91,005; the hop read 20,000 (shared prefix) -> 71,005 re-written
    assert first["attributable"] == 71_005 and first["rebuilt"] is True
    assert first["list_usd"] == pytest.approx(71_005 * 5.0 / 1e6)
    assert first["delta_usd"] == pytest.approx(71_005 * 4.8 / 1e6)
    assert back["reverses"] == first["t"]  # A -> B -> A inside 1 h
    assert back["rebuilt"] is False  # A's cache was still warm


def test_wait_avoided_is_the_pinned_accounts_next_release() -> None:
    first, _ = _build()["hops"]
    assert (first["busy"], first["cap"], first["parked"]) == (1, 1, 0)
    assert first["wait_avoided_s"] == pytest.approx(16.0)
    assert first["own_wait_s"] == pytest.approx(0.0)
    assert first["bytes"] == 300_000


def test_replay_rules_avoid_by_wait_and_by_body() -> None:
    hops = _build()["hops"]
    rows = {r["rule"]: r for r in hp.replay(hops, waits_t=(5, 30), bytes_x=(262144,))}
    assert rows["(a) stay unless est wait > 30s"]["hops_avoided"] == 2  # est 12 s <= 30
    assert rows["(a) stay unless est wait > 5s"]["hops_avoided"] == 0
    cold = hp.replay(_build(lambda *a: (12.0, False))["hops"], waits_t=(30,), bytes_x=())
    assert cold[1]["hops_avoided"] == 0  # an unevidenced estimate never keeps the pin
    assert rows["(b) stay unless body <= 256 KiB"]["hops_avoided"] == 2  # 300 KB bodies
    assert rows["never release on would-queue"]["added_wait_sum_s"] == pytest.approx(16.0)


def test_inflight_table_counts_hops_per_request() -> None:
    doc = _build()
    table = {r["inflight"]: r for r in hp.inflight_table(doc["requests"], T0, T0 + 3600)}
    assert table["36-40"] == {"inflight": "36-40", "requests": 3, "hops": 2, "per_100": 66.67}


def test_public_rows_carry_no_account_or_connection_ids() -> None:
    rows = [hp.public_hop(h) for h in _build()["hops"]]
    text = repr(rows)
    assert A not in text and B not in text and "c2" not in text
    assert set(rows[0]) == {
        "session",
        "migrate_at",
        "call_at",
        "prev_call_at",
        "pinned_next_admit_at",
        "reverses_hop_at",
        "hop_tokens",
        "prev_tokens",
        "attributable_cache_write",
    }
