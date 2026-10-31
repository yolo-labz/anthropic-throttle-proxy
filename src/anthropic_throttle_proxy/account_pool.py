"""Local, since-start token-work balancing with round-robin tie-breaking.

Only configured account labels and bearer hashes enter this tracker. Selection
is a pure read; status polling cannot spend a turn. Missing upstream usage is
charged an explicit estimate, never silently interpreted as free work.
"""

import time
from dataclasses import dataclass


@dataclass
class _Account:
    work: int = 0
    pending: int = 0
    tokens: int = 0
    attempts: int = 0
    estimated: int = 0
    turn: int = 0


@dataclass
class _Ticket:
    account: _Account
    estimate: int
    done: bool = False


class BalancedRoundRobin:
    def __init__(self):
        self.started_at = time.time()
        self._accounts: dict[str, _Account] = {}
        self._members: dict[str, str] = {}
        self._turn = 0
        self._measured = 0
        self._tokens = 0
        self._floor = 0

    @property
    def quantum(self) -> int:
        return max(1, self._tokens // self._measured) if self._measured else 1024

    def key(self, label: str, queued: int = 0) -> tuple[int, int]:
        account = self._accounts.get(label)
        if account is None:
            return self._floor + queued * self.quantum, 0
        work = account.work if label in self._members else max(account.work, self._floor)
        return work + account.pending + queued * self.quantum, account.turn

    def selected(self, label: str, members: dict[str, str]) -> None:
        continuing = self._members.keys() & members.keys()
        self._floor = min((self._accounts[key].work for key in continuing), default=self._floor)
        for key in members:
            account = self._accounts.setdefault(key, _Account())
            if key not in self._members:
                account.work = max(account.work, self._floor)
        self._members = dict(members)
        self._turn += 1
        self._accounts[label].turn = self._turn

    def begin(self, bearer: str) -> _Ticket | None:
        label = next((key for key, bid in self._members.items() if bid == bearer), None)
        if label is None:
            return None
        account = self._accounts[label]
        estimate = self.quantum
        account.pending += estimate
        account.attempts += 1
        return _Ticket(account, estimate)

    def finish(
        self, ticket: _Ticket | None, usage: dict | None, *, discarded: bool = False
    ) -> None:
        if ticket is None or ticket.done:
            return
        ticket.done = True
        account = ticket.account
        account.pending -= ticket.estimate
        if discarded:
            account.attempts -= 1
            return
        tokens = sum(
            value
            for key in ("input", "cache_read", "cache_creation", "output")
            if isinstance(value := (usage or {}).get(key), int) and value > 0
        )
        account.work += tokens or ticket.estimate
        account.tokens += tokens
        if tokens:
            self._tokens += tokens
            self._measured += 1
        else:
            account.estimated += 1

    def rows(self, members: dict[str, str]) -> list[dict]:
        accounts = {label: self._accounts.get(label, _Account()) for label in members}
        attempts = sum(account.attempts for account in accounts.values())
        tokens = sum(account.tokens for account in accounts.values())
        return [
            {
                "label": label,
                "bearer": members[label],
                "attempts": account.attempts,
                "tokens": account.tokens,
                "estimated": account.estimated,
                "pending_tokens": account.pending,
                "request_share": round(100 * account.attempts / attempts, 1) if attempts else None,
                "token_share": round(100 * account.tokens / tokens, 1) if tokens else None,
            }
            for label, account in accounts.items()
        ]


BALANCER = BalancedRoundRobin()
