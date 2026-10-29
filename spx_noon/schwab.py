"""SchwabBroker: Windows path. Tod's Schwab account, REST polling.

Live orders are refused unless Tod has approved the ticket with `yes <id>`.
Dry and alerts have no send permission. Orders this bot did not record are left alone.
The SPXNOON- ref lives in state/schwab_orders-<date>.json because Schwab has no orderRef.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time as _time
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import guard
from .broker import Broker
from .config import ORDER_PREFIX, SafetyError
from .schwabapi import SchwabClient, SchwabError, TokenStore
from .strategy import Bar, OptQuote, Px

ET = ZoneInfo("America/New_York")
log = logging.getLogger("spx_noon.schwab")
SPX = "$SPX"
_SYM = re.compile(r"^SPXW?\s+(\d{6})([CP])(\d{8})$")
WORKING = {
    "AWAITING_PARENT_ORDER", "AWAITING_CONDITION", "AWAITING_STOP_CONDITION", "AWAITING_MANUAL_REVIEW",
    "ACCEPTED", "AWAITING_UR_OUT", "PENDING_ACTIVATION", "QUEUED", "WORKING", "NEW",
    "AWAITING_RELEASE_TIME", "PENDING_ACKNOWLEDGEMENT", "PENDING_CANCEL", "PENDING_REPLACE",
}
DONE = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "REPLACED"}


def option_symbol(expiry_yyyymmdd: str, strike: float, right: str) -> str:
    return f"SPXW  {expiry_yyyymmdd[2:]}{right}{int(round(float(strike) * 1000)):08d}"


def parse_symbol(sym: str):
    match = _SYM.match(sym or "")
    if not match:
        return None
    return "20" + match.group(1), match.group(2), round(int(match.group(3)) / 1000, 2)


def build_vertical_order(legs, action: str, qty: int, limit: float, tif: str = "DAY") -> dict:
    """legs are [(symbol, signed ratio)] with +1 long and −1 short on the opening trade.

    BUY opens the debit. SELL closes it for a credit. ``limit`` is a positive price.
    """
    if qty != 1:
        raise guard.GuardError(f"noon breakout sends 1 spread, not {qty}")
    if float(limit) <= 0:
        raise guard.GuardError(f"refusing non-positive limit {limit}")
    opening = action == "BUY"
    order_legs = []
    for symbol, ratio in legs:
        buy_leg = (ratio > 0) == opening
        side = "BUY" if buy_leg else "SELL"
        effect = "TO_OPEN" if opening else "TO_CLOSE"
        order_legs.append({
            "instruction": f"{side}_{effect}",
            "quantity": 1,
            "instrument": {"symbol": symbol, "assetType": "OPTION"},
        })
    price = f"{round(round(float(limit) / 0.05) * 0.05, 2):.2f}"
    return {
        "session": "NORMAL",
        "duration": "GOOD_TILL_CANCEL" if tif == "GTC" else "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "VERTICAL",
        "orderType": "NET_DEBIT" if action == "BUY" else "NET_CREDIT",
        "price": price,
        "quantity": 1,
        "orderLegCollection": order_legs,
    }


def _ms(ts) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ts) / 1000, timezone.utc).astimezone(ET) if ts else None
    except (TypeError, ValueError, OSError):
        return None


def _num(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value in (-999.0, -1.0):
        return None
    return value


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def execution_price(order: dict, action: str) -> float | None:
    """Net fill from execution legs. Positive debit for a buy, positive credit for a sell.

    The order's ``price`` field is the limit we asked for, not the fill.
    """
    legs = {leg.get("legId"): leg for leg in order.get("orderLegCollection") or []}
    lots = float(order.get("filledQuantity") or 0)
    if lots <= 0:
        return None
    paid = 0.0
    seen = False
    for activity in order.get("orderActivityCollection") or []:
        for fill in activity.get("executionLegs") or []:
            leg = legs.get(fill.get("legId"))
            if not leg:
                continue
            sign = 1.0 if str(leg.get("instruction", "")).startswith("BUY") else -1.0
            paid += sign * float(fill["price"]) * float(fill["quantity"])
            seen = True
    if not seen:
        return None
    net = paid / lots
    return round(net if action == "BUY" else -net, 4)


class SchwabBroker(Broker):
    def __init__(self, cfg, client: SchwabClient | None = None, order_guard: guard.OrderGuard | None = None,
                 clock=_time.monotonic):
        cfg.validate()
        if cfg.broker != "schwab":
            raise SafetyError("SchwabBroker is the Windows setup")
        self.cfg = cfg
        self.guard = order_guard or guard.OrderGuard(
            live=cfg.live, max_per_min=cfg.max_orders_per_min, min_replace_secs=cfg.min_replace_secs, clock=clock)
        if client is None:
            client = SchwabClient(TokenStore(Path(cfg.token_path).expanduser()), order_guard=self.guard)
        client.order_guard = self.guard
        self.c = client
        self.clock = clock
        self._q: dict = {}
        self._q_at = -1e9
        self._chain_at = -1e9
        self._quotes: dict = {}
        self._want: set = set()
        self._expiry = None
        self._strikes_cache: dict = {}
        self.ticket: str | None = None
        self.connected_ok = False
        self._last_ok = None
        self.today = datetime.now(ET).date()
        self.store_path = cfg.state_dir / f"schwab_orders-{self.today.isoformat()}.json"
        self.store = {"tickets": {}, "orders": {}}
        if self.store_path.exists():
            try:
                self.store = json.loads(self.store_path.read_text())
            except ValueError:
                pass

    def _save(self):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.store, indent=1, default=str))
        tmp.replace(self.store_path)

    def connect(self):
        self.c.account_hash(self.cfg.account or None)
        self.c.quotes([SPX])
        self.connected_ok = True
        self._last_ok = datetime.now(ET)
        if self.cfg.live:
            for ticket_id, ticket in self.store.get("tickets", {}).items():
                if not ticket.get("finished"):
                    self.guard.approve(ticket_id, float(ticket["cap"]))
                    self.ticket = ticket_id
            for order_id, order in self.store.get("orders", {}).items():
                self.guard.bind(order.get("current") or order_id, order["ticket"])

    def ensure_connected(self) -> bool:
        if self._last_ok and (datetime.now(ET) - self._last_ok).total_seconds() < 60:
            return True
        try:
            self._poll_quotes(force=True)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Schwab API unreachable: %s", exc)
            return False

    def disconnect(self):
        return None

    def now(self):
        return datetime.now(ET)

    def sleep(self, seconds):
        _time.sleep(max(0.0, seconds))

    def managed_accounts(self):
        rows = self.c.get("/trader/v1/accounts/accountNumbers") or []
        return [str(row["accountNumber"]) for row in rows]

    def net_liquidation(self, account):
        acct = (self.c.account(positions=False) or {}).get("securitiesAccount", {})
        balances = acct.get("currentBalances") or {}
        return _num(balances.get("liquidationValue"))

    def spx_hours(self):
        day = datetime.now(ET).date()
        payload = self.c.market_hours("option", day.isoformat()) or {}
        ind = (payload.get("option") or {}).get("IND") or (payload.get("option") or {}).get("option")
        key = day.strftime("%Y%m%d")
        if not ind:
            closed = payload.get("option") is not None and not payload.get("option")
            return (f"{key}:CLOSED" if closed else None), "America/New_York"
        if not ind.get("isOpen"):
            return f"{key}:CLOSED", "America/New_York"
        segments = []
        for session in (ind.get("sessionHours") or {}).get("regularMarket") or []:
            start = datetime.fromisoformat(session["start"]).astimezone(ET)
            end = datetime.fromisoformat(session["end"]).astimezone(ET)
            segments.append(f"{start:%Y%m%d:%H%M}-{end:%Y%m%d:%H%M}")
        return (";".join(segments) or None), "America/New_York"

    def _poll_quotes(self, force=False):
        if not force and self.clock() - self._q_at < self.cfg.poll_secs:
            return
        self._q = self.c.quotes([SPX]) or {}
        self._q_at = self.clock()
        self._last_ok = datetime.now(ET)

    def spx_px(self):
        try:
            self._poll_quotes()
        except Exception as exc:  # noqa: BLE001
            log.warning("quote poll failed: %s", exc)
        quote = (self._q.get(SPX) or {}).get("quote") or {}
        value = _num(quote.get("lastPrice"))
        stamp = _ms(quote.get("tradeTime") or quote.get("quoteTime"))
        return Px(value, stamp, 1 if value is not None else None, _num(quote.get("highPrice")))

    @staticmethod
    def _bars(candles) -> list[Bar]:
        out = []
        for candle in candles or []:
            stamp = _ms(candle["datetime"])
            if stamp is None:
                continue
            out.append(Bar(stamp, candle["open"], candle["high"], candle["low"], candle["close"],
                           candle.get("volume", 0) or 0))
        return out

    def spx_bars_15m(self, days: int = 5):
        end = datetime.now(ET)
        start = datetime.combine(end.date() - timedelta(days=days + 4), time(0), ET)
        candles = self.c.price_history_range(SPX, "minute", 15, start.timestamp() * 1000, end.timestamp() * 1000)
        bars = self._bars(candles)
        return [bar for bar in bars if time(9, 30) <= bar.t.time() < time(16, 0)]

    def chain_strikes(self, expiry: str):
        if expiry in self._strikes_cache:
            return self._strikes_cache[expiry]
        day = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
        chain = self.c.option_chain(SPX, day)
        strikes = set()
        for side in ("callExpDateMap", "putExpDateMap"):
            for _exp, rows in (chain.get(side) or {}).items():
                for _strike, contracts in rows.items():
                    for contract in contracts:
                        parsed = parse_symbol(contract.get("symbol", ""))
                        if contract.get("optionRoot") == "SPXW" and parsed and parsed[0] == expiry:
                            strikes.add(float(contract["strikePrice"]))
        found = sorted(strikes) or None
        self._strikes_cache[expiry] = found
        return found

    def ensure_options(self, expiry, strikes):
        self._expiry = expiry
        spot = self.spx_px().value
        want = {float(strike) for strike in strikes}
        budget = max(1, self.cfg.max_option_lines // 2)
        if spot and len(want) > budget:
            want = set(sorted(want, key=lambda strike: abs(strike - spot))[:budget])
        if want != self._want:
            self._want = set(want)
            self._chain_at = -1e9

    def _poll_chain(self):
        if not self._want or not self._expiry or self.clock() - self._chain_at < self.cfg.poll_secs:
            return
        spot = self.spx_px().value or (sum(self._want) / len(self._want))
        far = max(abs(strike - spot) for strike in self._want)
        count = int(min(400, 2 * math.ceil(far / 5.0) + 6))
        day = f"{self._expiry[:4]}-{self._expiry[4:6]}-{self._expiry[6:]}"
        chain = self.c.option_chain(SPX, day, strike_count=count)
        out = {}
        for side in ("callExpDateMap", "putExpDateMap"):
            for _exp, rows in (chain.get(side) or {}).items():
                for _strike, contracts in rows.items():
                    for contract in contracts:
                        parsed = parse_symbol(contract.get("symbol", ""))
                        if contract.get("optionRoot") != "SPXW" or parsed is None or parsed[0] != self._expiry:
                            continue
                        strike = float(contract["strikePrice"])
                        if strike not in self._want:
                            continue
                        iv = _num(contract.get("volatility"))
                        out[(strike, parsed[1])] = OptQuote(
                            bid=_num(contract.get("bid")),
                            ask=_num(contract.get("ask")),
                            iv=iv / 100.0 if iv and iv > 0 else None,
                            delta=_num(contract.get("delta")),
                            volume=_num(contract.get("totalVolume")),
                            time=_ms(contract.get("quoteTimeInLong")),
                            mdtype=1,
                            con_id=contract["symbol"],
                        )
        self._quotes = out
        self._chain_at = self.clock()
        self._last_ok = datetime.now(ET)

    def option_quotes(self):
        try:
            self._poll_chain()
        except Exception as exc:  # noqa: BLE001
            log.warning("chain poll failed: %s", exc)
        return dict(self._quotes)

    def preview_combo(self, legs, action, qty, limit, tif="DAY"):
        order = build_vertical_order(legs, action, int(qty), float(limit), tif)
        result = self.c.preview_order(order)
        result["order"] = order
        return result

    def approve_ticket(self, ticket_id, cap):
        self.guard.approve(ticket_id, float(cap))
        self.ticket = ticket_id
        self.store.setdefault("tickets", {})[ticket_id] = {
            "cap": float(cap), "approved_at": datetime.now(ET).isoformat(),
        }
        self._save()

    def finish_ticket(self):
        if self.ticket:
            self.guard.close(self.ticket)
            self.store.setdefault("tickets", {}).setdefault(self.ticket, {})["finished"] = True
            self._save()

    def _need_ticket(self):
        if not self.cfg.live:
            raise SafetyError("order call outside live mode")
        ticket_id = self.ticket
        if not ticket_id or ticket_id not in self.guard.approved:
            raise guard.GuardError("refused: no approved ticket for this trade")
        return ticket_id

    def _wait_rate(self, kind, order_id=None):
        wait = self.guard.wait_secs(kind, order_id)
        if wait > 0:
            if wait > 30:
                raise guard.GuardError(f"{kind} refused: rate limit ({wait:.0f}s)")
            _time.sleep(wait + 0.05)

    def place_combo(self, legs, action, qty, limit, account, order_ref=None, tif="DAY"):
        if not str(order_ref or "").startswith(ORDER_PREFIX):
            raise SafetyError(f"every Noon order must carry an {ORDER_PREFIX} ref")
        ticket_id = self._need_ticket()
        if action == "BUY" and float(limit) > self.guard.cap(ticket_id) + 1e-9:
            raise guard.GuardError(
                f"refused: {float(limit):.2f} is above the approved cap {self.guard.cap(ticket_id):.2f}")
        order = build_vertical_order(legs, action, int(qty), float(limit), tif)
        self._wait_rate("place")
        order_id = self.c.place_order(order, ticket_id)
        if not order_id:
            raise RuntimeError("Schwab accepted the order but returned no order id")
        self.guard.bind(order_id, ticket_id)
        self.store.setdefault("orders", {})[order_id] = {
            "ticket": ticket_id,
            "ref": order_ref,
            "action": action,
            "qty": int(qty),
            "legs": [list(leg) for leg in legs],
            "limit": float(limit),
            "tif": tif,
            "current": order_id,
            "placed_at": datetime.now(ET).isoformat(),
        }
        self._save()
        return order_id

    def _own(self, handle):
        found = self.store.get("orders", {}).get(str(handle))
        if found is None:
            log.warning("refusing: order %s was not placed by Noon Breakout", handle)
        return found

    def cancel(self, handle):
        meta = self._own(handle)
        if meta is None:
            return
        state = self.order_state(handle)
        if state["done"]:
            return
        self._wait_rate("cancel", meta["current"])
        try:
            self.c.cancel_order(meta["current"], meta["ticket"])
        except SchwabError as exc:
            log.warning("cancel %s: %s", meta["current"], exc)

    def order_state(self, handle):
        meta = self.store.get("orders", {}).get(str(handle))
        if meta is None:
            raise KeyError(handle)
        order = self.c.get_order(meta["current"]) or {}
        raw = (order.get("status") or "").upper()
        qty = float(meta["qty"])
        filled = float(order.get("filledQuantity") or 0)
        if raw == "FILLED" and filled <= 0:
            filled = qty
        if raw == "FILLED":
            avg = execution_price(order, meta.get("action") or "BUY")
            if avg is None:
                avg = _num(order.get("price"))
            if avg is None:
                avg = float(meta["limit"])
            status = "Filled"
        elif raw in ("CANCELED", "CANCELLED", "REJECTED", "EXPIRED"):
            avg = 0.0
            status = "Cancelled"
        else:
            avg = 0.0
            status = "Submitted"
        return {
            "status": status,
            "filled": filled,
            "remaining": max(qty - filled, 0.0),
            "avg_price": avg or 0.0,
            "done": status in ("Filled", "Cancelled") or raw in DONE,
        }

    def own_open_orders(self):
        out = []
        for order_id, meta in self.store.get("orders", {}).items():
            if not str(meta.get("ref") or "").startswith(ORDER_PREFIX):
                continue
            try:
                state = self.order_state(order_id)
            except Exception:  # noqa: BLE001
                continue
            if state["done"]:
                continue
            out.append({
                "order_id": order_id,
                "action": meta["action"],
                "limit": meta["limit"],
                "ref": meta["ref"],
                "status": state["status"],
            })
        return out

    def modify_combo(self, handle, limit):
        raise SafetyError("refusing to reprice: noon breakout does not chase")
