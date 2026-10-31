"""Balanced work, round-robin tie-breaking and observable accounting."""

from anthropic_throttle_proxy.account_pool import BalancedRoundRobin


def test_equal_work_rotates_and_read_only_choice_does_not_advance():
    pool = BalancedRoundRobin()
    members = {"A": "aaa", "B": "bbb"}
    chosen = []
    for _ in range(6):
        label = min(members, key=pool.key)
        assert min(members, key=pool.key) == label
        pool.selected(label, members)
        ticket = pool.begin(members[label])
        pool.finish(ticket, {"input": 10, "output": 10})
        chosen.append(label)
    assert chosen == ["A", "B", "A", "B", "A", "B"]


def test_large_response_is_compensated_by_actual_token_work():
    pool = BalancedRoundRobin()
    members = {"A": "aaa", "B": "bbb"}
    pool.selected("A", members)
    pool.finish(pool.begin("aaa"), {"input": 90, "output": 10})
    for _ in range(5):
        assert min(members, key=pool.key) == "B"
        pool.selected("B", members)
        pool.finish(pool.begin("bbb"), {"input": 10, "output": 10})
    assert min(members, key=pool.key) == "A"


def test_inflight_work_is_reserved_and_local_refusal_rolls_back():
    pool = BalancedRoundRobin()
    members = {"A": "aaa", "B": "bbb"}
    pool.selected("A", members)
    ticket = pool.begin("aaa")
    assert pool.key("B") < pool.key("A")
    pool.finish(ticket, None, discarded=True)
    row = pool.rows(members)[0]
    assert row["attempts"] == 0 and row["estimated"] == 0 and row["tokens"] == 0


def test_unknown_sent_outcome_is_not_free_work():
    pool = BalancedRoundRobin()
    pool.selected("A", {"A": "aaa", "B": "bbb"})
    pool.finish(pool.begin("aaa"), None)
    assert pool.key("B") < pool.key("A")
    row = pool.rows({"A": "aaa", "B": "bbb"})[0]
    assert row["estimated"] == 1 and row["tokens"] == 0
    assert row["token_share"] is None


def test_rejoining_account_gets_no_absence_credit():
    pool = BalancedRoundRobin()
    pool.selected("A", {"A": "aaa"})
    pool.finish(pool.begin("aaa"), {"input": 1000})
    members = {"A": "aaa", "B": "bbb"}
    pool.selected("B", members)
    pool.finish(pool.begin("bbb"), {"input": 20})
    assert min(members, key=pool.key) == "A"


def test_bearer_rotation_preserves_label_accounting_and_cached_tokens():
    pool = BalancedRoundRobin()
    pool.selected("A", {"A": "old"})
    pool.finish(
        pool.begin("old"), {"input": 10, "cache_read": 30, "cache_creation": 20, "output": 40}
    )
    pool.selected("A", {"A": "new"})
    assert pool.begin("old") is None
    pool.finish(pool.begin("new"), {"output": 10})
    row = pool.rows({"A": "new"})[0]
    assert row["attempts"] == 2 and row["tokens"] == 110


def test_zero_history_is_unknown_and_finishing_a_ticket_is_idempotent():
    pool = BalancedRoundRobin()
    assert pool.rows({"A": "aaa"})[0]["request_share"] is None
    pool.selected("A", {"A": "aaa"})
    ticket = pool.begin("aaa")
    pool.finish(ticket, {"input": 10})
    pool.finish(ticket, {"input": 10})
    assert pool.rows({"A": "aaa"})[0]["tokens"] == 10


def _drive_selection(*, poll: bool) -> tuple[list[str], list[tuple[int, int]]]:
    """Replays the router's pick loop; ``poll`` interleaves read-only status reads."""
    pool = BalancedRoundRobin()
    members = {"A": "aaa", "B": "bbb"}
    picks = []
    for i in range(12):
        if poll:
            for _ in range(3):
                pool.rows(members)
                pool.key("A")
                pool.key("B")
        label = min(members, key=pool.key)
        pool.selected(label, members)
        ticket = pool.begin(members[label])
        pool.finish(ticket, {"input": 100 + 7 * i, "output": 20})
        picks.append(label)
    return picks, [pool.key("A"), pool.key("B")]


def test_read_only_polls_never_advance_the_rotation_turn() -> None:
    """A status poll must not move the next pick or the tie-break state."""
    assert _drive_selection(poll=True) == _drive_selection(poll=False)
