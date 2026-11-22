"""Broker surface plus the in-memory SimBroker used by tests.

Timestamps handed back to the bot are tz-aware America/New_York.
Legs are (con_id, ratio). Ratio +1 is the long leg, −1 is the short leg.

:func:`select_option_lines` is the one place that decides which option
market-data lines a broker holds. Both live brokers call it, so IBKR and
tastytrade subscribe once, hold a line through a small spot move, and keep the
legs of an open spread whatever the band asks for.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

from .pricing import TICK
from .strategy import Bar, Px

ET = ZoneInfo("America/New_York")

#: Seconds a line stays subscribed after it leaves the wanted set. The brokers
#: pass ``Config.option_line_hold_s``, which defaults to this.
OPTION_LINE_HOLD_S = 60.0


class Selection(NamedTuple):
    """What :func:`select_option_lines` decided.

    ``keep`` is the set of lines that should be subscribed once the call is
    applied; the broker diffs it against what it holds and cancels or requests
    only the difference. ``undesired_since`` is the hold clock to carry into the
    next call: one entry per kept line that is outside the wanted set, stamped
    when it left it.
    """

    keep: frozenset
    undesired_since: dict


def _spot_or_none(spot):
    """A spot that may be ranked against, or ``None``. Zero is not a spot."""
    try:
        value = float(spot)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _keys(items):
    return {(float(strike), str(right)) for strike, right in (items or ())}


def select_option_lines(candidates, *, spot, now, budget, subscribed_at=(), pinned=(),
                        hold_s: float = OPTION_LINE_HOLD_S, undesired_since=None) -> Selection:
    """Decide which option lines should be subscribed. One line is one ``(strike, right)``.

    Pure: it reads no broker and mutates nothing it is handed, so both brokers
    share it and the tests drive it with fixed datetimes.

    ``candidates`` are the lines the caller would like. ``subscribed_at`` is what
    is subscribed now, as the broker's mapping of key to the time that
    subscription started; only the keys matter here, because what ages a line out
    is how long it has been unwanted, not when it was requested. ``pinned`` lines
    are the legs of an open spread or a working entry: they are kept whatever the
    spot does and are subscribed ahead of any unpinned line.

    With a usable spot the wanted lines are every pin plus the nearest unpinned
    candidates, ranked by ``(distance, strike, right)`` so ties do not follow the
    caller's order, up to ``budget`` lines. A line that leaves the wanted set
    stays for ``hold_s`` seconds, so a small spot move does not cancel and
    re-request the edge of the book; while it holds its slot no new unpinned line
    is requested over ``budget``. After the hold it is dropped once, and the call
    that drops it may fill the slot with a wanted line.

    With no usable spot — missing, non-finite, or ``<= 0`` — nothing is ranked
    against 0: every held line stays, and only a missing pin is added, and only
    while the budget has room.
    """
    budget = max(0, int(budget))
    hold_s = max(0.0, float(hold_s))
    held = {(float(strike), str(right)) for strike, right in (subscribed_at or ())}
    pins = _keys(pinned)
    wanted_pool = _keys(candidates) | pins
    stamps = dict(undesired_since or {})
    usable = _spot_or_none(spot)
    if usable is None:
        # No ranking without a spot, so nothing is cancelled. A pin that is not
        # up yet is still worth having, as long as it fits.
        keep = set(held)
        for key in sorted(pins - held):
            if len(keep) >= budget:
                break
            keep.add(key)
        return Selection(frozenset(keep), {k: v for k, v in stamps.items() if k in keep})

    def rank(key):
        return (abs(key[0] - usable), key[0], key[1])

    pins_ranked = sorted(pins, key=rank)
    # Every pin first, then the nearest unpinned candidates fill the budget.
    wanted = set(pins_ranked[:budget])
    for key in sorted(wanted_pool - pins, key=rank):
        if len(wanted) >= budget:
            break
        wanted.add(key)
    cold = {key: stamps.get(key, now) for key in held - wanted}
    holding = {key for key, since in cold.items() if (now - since).total_seconds() < hold_s}
    keep = set(pins_ranked[:budget])
    # What is already subscribed keeps its slot ahead of a line that is not: a
    # line inside its hold window blocks a new unpinned request, not the reverse.
    for key in sorted((held & wanted) | holding, key=rank):
        if len(keep) >= budget:
            break
        keep.add(key)
    for key in sorted(wanted - keep, key=rank):
        if len(keep) >= budget:
            break
        keep.add(key)
    return Selection(frozenset(keep), {key: since for key, since in cold.items() if key in keep})


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
    def pin_options(self, expiry: str, legs):
        """Keep these ``(strike, right)`` lines subscribed until the pins are cleared.

        ``legs`` is ``None`` to clear. A no-op here, so ``SimBroker`` and the test
        doubles need nothing; the live brokers store the pins and honour them on
        the next ``ensure_options``.
        """
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
