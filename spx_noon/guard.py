"""Order guard for the live Schwab account.

Every place / cancel must name a ticket id that Tod approved by typing `yes <id>`.
Dry and alerts never get a guard with send permission: ``live=False`` refuses everything.
At most 10 order requests per rolling minute.
"""
from __future__ import annotations

import time
from collections import deque


class GuardError(RuntimeError):
    pass


def mask_account(acct: str | None) -> str:
    if not acct:
        return "<none>"
    return "*" * max(0, len(acct) - 3) + acct[-3:]


class OrderGuard:
    def __init__(self, live: bool, max_per_min: int = 10, min_replace_secs: float = 15.0,
                 clock=time.monotonic):
        self.live = live
        self.max_per_min = max_per_min
        self.min_replace_secs = min_replace_secs
        self.clock = clock
        self.approved: dict[str, dict] = {}
        self.closed: set[str] = set()
        self.sent: deque = deque()
        self.last_replace: dict[str, float] = {}
        self.owner: dict[str, str] = {}

    def approve(self, ticket_id: str, cap: float) -> None:
        if not self.live:
            raise GuardError("not in live mode: approvals are not accepted")
        self.approved[ticket_id] = {"cap": float(cap), "time": time.time()}

    def close(self, ticket_id: str) -> None:
        self.closed.add(ticket_id)

    def cap(self, ticket_id: str) -> float:
        return self.approved[ticket_id]["cap"]

    def bind(self, order_id: str, ticket_id: str) -> None:
        if order_id:
            self.owner[str(order_id)] = ticket_id
            self.last_replace.setdefault(str(order_id), self.clock())

    def wait_secs(self, kind: str, order_id: str | None = None) -> float:
        now = self.clock()
        while self.sent and now - self.sent[0] >= 60:
            self.sent.popleft()
        wait = 0.0
        if len(self.sent) >= self.max_per_min:
            wait = 60 - (now - self.sent[0])
        if kind == "replace" and order_id in self.last_replace:
            wait = max(wait, self.min_replace_secs - (now - self.last_replace[order_id]))
        return max(0.0, wait)

    def authorize(self, kind: str, ticket_id: str | None, order_id: str | None = None) -> None:
        if not self.live:
            raise GuardError(f"{kind} refused: bot is not in live mode")
        if not ticket_id or ticket_id not in self.approved:
            raise GuardError(f"{kind} refused: ticket {ticket_id!r} was not approved by Tod")
        if ticket_id in self.closed and kind == "place":
            raise GuardError(f"place refused: ticket {ticket_id} is finished")
        if order_id is not None and self.owner.get(str(order_id)) != ticket_id:
            raise GuardError(f"{kind} refused: order {order_id} does not belong to ticket {ticket_id}")
        wait = self.wait_secs(kind, order_id)
        if wait > 0:
            raise GuardError(f"{kind} refused: rate limit, retry in {wait:.0f}s")
        now = self.clock()
        self.sent.append(now)
        if kind == "replace":
            self.last_replace[str(order_id)] = now
