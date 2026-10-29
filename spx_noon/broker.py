"""Broker surface plus the in-memory SimBroker used by tests.

Timestamps handed back to the bot are tz-aware America/New_York.
Legs are (con_id, ratio). Ratio +1 is the long leg, −1 is the short leg.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .pricing import TICK
from .strategy import Bar, Px

ET = ZoneInfo("America/New_York")


class Broker:
    def connect(self): ...
    def ensure_connected(self) -> bool:
        return True
    def disconnect(self): ...
    def now(self) -> datetime: ...
    def sleep(self, seconds: float): ...
    def managed_accounts(self) -> list[str]: ...
    def net_liquidation(self, account: str) -> float | None: ...
    def spx_hours(self) -> tuple[str | None, str | None]: ...
    def spx_px(self) -> Px: ...
    def spx_bars_15m(self, days: int = 5) -> list[Bar]: ...
    def chain_strikes(self, expiry: str) -> list[float] | None: ...
    def ensure_options(self, expiry: str, strikes: list[float]): ...
    def option_quotes(self) -> dict: ...
    def preview_combo(self, legs, action, qty, limit, tif="DAY") -> dict:
        return {"ok": True, "rejects": [], "warnings": []}
    def approve_ticket(self, ticket_id: str, cap: float): ...
    def finish_ticket(self): ...
    def place_combo(self, legs, action, qty, limit, account, order_ref=None, tif="DAY"): ...
    def cancel(self, handle): ...
    def order_state(self, handle) -> dict: ...
    def own_open_orders(self) -> list[dict]:
        return []


def to_et(dt, naive_tz: str = "US/Central"):
    if dt is None:
        return None
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return datetime.combine(dt, time(0), ET)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(_tz(naive_tz)))
    return dt.astimezone(ET)


def _tz(name: str) -> str:
    return {"US/Central": "America/Chicago", "US/Eastern": "America/New_York"}.get(name, name)


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value == -1:
        return None
    return value


class SimBroker(Broker):
    """Fake clock and combo fills. A buy at the mid fills. A sell fills once the spread bid
    reaches the limit. Nothing here touches the network."""

    def __init__(self, now: datetime, bars: list[Bar], quotes, spot: float = 7000.0,
                 account: str = "DU999001"):
        self.t = now
        self.bars = list(bars)
        self.quotes = quotes
        self.spot = spot
        self.account = account
        self.connected = False
        self.orders: dict = {}
        self._oid = 100
        self.placed: list = []
        self.cancelled: list = []
        self.approved: dict = {}
        self.ticket: str | None = None

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def now(self):
        return self.t

    def sleep(self, seconds: float):
        self.t = self.t + timedelta(seconds=float(seconds))

    def managed_accounts(self):
        return [self.account]

    def net_liquidation(self, account):
        return 10000.0

    def spx_hours(self):
        day = self.t.strftime("%Y%m%d")
        return f"{day}:0930-{day}:1600", "America/New_York"

    def spx_px(self):
        return Px(self.spot, self.t, 1, None)

    def spx_bars_15m(self, days: int = 5):
        return list(self.bars)

    def chain_strikes(self, expiry: str):
        return sorted({float(k) for k, _r in self.option_quotes()})

    def ensure_options(self, expiry, strikes):
        return None

    def option_quotes(self):
        book = self.quotes(self.t) if callable(self.quotes) else self.quotes
        return dict(book)

    def approve_ticket(self, ticket_id, cap):
        self.approved[ticket_id] = float(cap)
        self.ticket = ticket_id

    def finish_ticket(self):
        if self.ticket:
            self.approved.pop(self.ticket, None)
            self.ticket = None

    def _by_id(self):
        return {q.con_id: q for q in self.option_quotes().values()}

    def _combo(self, legs):
        by_id = self._by_id()
        mid = natural = close_bid = 0.0
        for cid, ratio in legs:
            quote = by_id[cid]
            mid += ratio * ((quote.bid + quote.ask) / 2.0)
            if ratio > 0:
                natural += ratio * quote.ask
                close_bid += ratio * quote.bid
            else:
                natural += ratio * quote.bid
                close_bid += ratio * quote.ask
        return mid, natural, close_bid

    def place_combo(self, legs, action, qty, limit, account, order_ref=None, tif="DAY"):
        self._oid += 1
        order = {
            "legs": list(legs), "action": action, "qty": int(qty), "limit": float(limit),
            "order_ref": order_ref, "tif": tif, "status": "Submitted", "filled": 0, "avg": 0.0,
        }
        self.orders[self._oid] = order
        self.placed.append({"id": self._oid, "action": action, "qty": int(qty), "limit": float(limit),
                            "account": account, "order_ref": order_ref, "tif": tif})
        self._try_fill(self._oid)
        return self._oid

    def _try_fill(self, handle):
        order = self.orders.get(handle)
        if order is None or order["status"] != "Submitted":
            return
        mid, _natural, close_bid = self._combo(order["legs"])
        # A buy posted at the floored mid is within one nickel of mid. Fill it at the limit.
        if order["action"] == "BUY" and order["limit"] + TICK + 1e-9 >= mid:
            order.update(status="Filled", filled=order["qty"], avg=order["limit"])
        elif order["action"] == "SELL" and close_bid + 1e-9 >= order["limit"]:
            order.update(status="Filled", filled=order["qty"], avg=order["limit"])

    def cancel(self, handle):
        self.cancelled.append(handle)
        order = self.orders.get(handle)
        if order and order["status"] != "Filled":
            order["status"] = "Cancelled"

    def order_state(self, handle):
        self._try_fill(handle)
        order = self.orders[handle]
        filled = order["filled"]
        return {
            "status": order["status"],
            "filled": filled,
            "remaining": order["qty"] - filled,
            "avg_price": order["avg"],
            "done": order["status"] in ("Filled", "Cancelled"),
        }

    def own_open_orders(self):
        out = []
        for oid, order in self.orders.items():
            if order["status"] == "Submitted" and str(order.get("order_ref") or "").startswith("SPXNOON-"):
                out.append({"order_id": oid, "action": order["action"], "limit": order["limit"],
                            "ref": order["order_ref"], "status": order["status"]})
        return out
