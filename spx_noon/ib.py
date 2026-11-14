"""IBBroker: Mac path. IBKR TWS on localhost.

mode ``paper`` is port 7497 and account ids starting with D. mode ``live`` is
real money on 7496 (TWS) or 4001 (IB Gateway) with a configured account that
does not start with D, and TWS has to report that same account. mode ``dry``
and ``alerts`` connect read-only on 7497 and never send.

Orders carry orderRef SPXNOON-. The bot never modifies an order that does not.
Entry orders are DAY. The take-profit is GTC, as the plan specifies.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime

from .broker import Broker, to_et
from .config import (
    LIVE_PORTS,
    LOCALHOSTS,
    ORDER_PREFIX,
    PAPER_PORT,
    SafetyError,
    account_tail,
    log_order,
    redact_account,
)
from .strategy import Bar, OptQuote, Px

log = logging.getLogger("spx_noon.ib")


def _f(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value == -1:
        return None
    return value


def _tails(accounts) -> list[str]:
    return [account_tail(acct) for acct in accounts]


def paper_send_allowed(cfg, account: str, order_ref: str) -> None:
    """Raise unless this order is allowed onto TWS paper."""
    if cfg.broker != "ibkr":
        raise SafetyError("IBKR orders are the Mac paper path")
    if not cfg.paper:
        raise SafetyError("refusing: Mac sends orders only in paper mode")
    if int(cfg.port) != PAPER_PORT:
        raise SafetyError(f"refusing: port {cfg.port} is not {PAPER_PORT}")
    if cfg.host not in LOCALHOSTS:
        raise SafetyError(f"refusing: host {cfg.host!r} is not localhost")
    if not str(account).startswith("D"):
        raise SafetyError(f"refusing: account ...{account_tail(account)} is not an IBKR paper account")
    if not str(order_ref or "").startswith(ORDER_PREFIX):
        raise SafetyError(f"every Noon order must carry an {ORDER_PREFIX} orderRef")


def live_send_allowed(cfg, account: str, order_ref: str) -> None:
    """Raise unless this order is allowed onto the configured IBKR live account."""
    if cfg.broker != "ibkr":
        raise SafetyError("IBKR orders are the Mac path")
    if not cfg.ibkr_live:
        raise SafetyError("refusing: this config is not IBKR live")
    if int(cfg.port) not in LIVE_PORTS:
        raise SafetyError(f"refusing: port {cfg.port} is not an IBKR live port {LIVE_PORTS}")
    if cfg.host not in LOCALHOSTS:
        raise SafetyError(f"refusing: host {cfg.host!r} is not localhost")
    acct = str(account)
    if not acct.strip():
        raise SafetyError("refusing: a live order needs the configured live account")
    if acct.startswith("D"):
        raise SafetyError("refusing: a live order must not go to a paper (D...) account")
    if acct != str(cfg.account):
        raise SafetyError("refusing: the order account is not the configured live account")
    if not str(order_ref or "").startswith(ORDER_PREFIX):
        raise SafetyError(f"every Noon order must carry an {ORDER_PREFIX} orderRef")


def send_allowed(cfg, account: str, order_ref: str) -> None:
    """The one gate every IBKR order passes. dry and alerts never reach a broker."""
    if getattr(cfg, "broker", "") != "ibkr":
        raise SafetyError("IBKR orders are the Mac path")
    if cfg.mode == "live":
        live_send_allowed(cfg, account, order_ref)
        return
    paper_send_allowed(cfg, account, order_ref)


def check_accounts(cfg, accounts) -> str:
    """Verify what TWS reports and return the account this session may use.

    Live: the configured account is reported, does not start with D, and no
    reported account starts with D. Paper, dry, and alerts: every reported
    account must start with D. No socket is touched and no full account id is
    raised.
    """
    reported = [str(acct) for acct in (accounts or [])]
    if not reported:
        raise SafetyError("TWS reported no account")
    want = str(getattr(cfg, "account", "") or "")
    if getattr(cfg, "mode", "") == "live":
        if not cfg.ibkr_live:
            raise SafetyError("refusing: this config is not IBKR live")
        if not want.strip():
            raise SafetyError("refusing: live mode needs account set to the live account id")
        if want.startswith("D"):
            raise SafetyError("refusing: live mode will not use a paper (D...) account")
        if want not in reported:
            raise SafetyError(
                f"refusing: the configured live account is not one TWS reports ({_tails(reported)})")
        paper = [acct for acct in reported if acct.startswith("D")]
        if paper:
            raise SafetyError(
                f"refusing: live mode will not share a session with paper account(s) {_tails(paper)}")
        return want
    if want and not want.startswith("D"):
        raise SafetyError(
            f"refusing: configured account ...{account_tail(want)} is not an IBKR paper (D...) id")
    bad = [acct for acct in reported if not acct.startswith("D")]
    if bad:
        raise SafetyError(f"refusing non-paper account(s) {_tails(bad)}")
    if want:
        if want not in reported:
            raise SafetyError(f"account ...{account_tail(want)} is not managed by TWS ({_tails(reported)})")
        return want
    return reported[0]


def _dial_port(cfg) -> int:
    """The port this config may dial. ``validate()`` has already run."""
    port = int(cfg.port)
    if cfg.mode == "live":
        if port not in LIVE_PORTS:
            raise SafetyError(f"Refusing to connect: port {port} is not an IBKR live port {LIVE_PORTS}")
        if not cfg.ibkr_live:
            raise SafetyError("Refusing to connect: this config is not IBKR live")
    elif port != PAPER_PORT:
        raise SafetyError(f"Refusing to connect: port {port} != {PAPER_PORT}")
    return port


class IBBroker(Broker):
    def __init__(self, cfg):
        cfg.validate()
        if cfg.broker != "ibkr":
            raise SafetyError("IBBroker is the Mac setup (IBKR)")
        _dial_port(cfg)
        from ib_async import IB
        self.cfg = cfg
        self.ib = IB()
        self.errors: list[tuple] = []
        self.ib.errorEvent += self._on_error
        self._opt_tickers: dict = {}
        self._opt_contracts: dict = {}
        self._trades: dict = {}
        self.spx = None
        self.verified_account: str | None = None

    def _on_error(self, reqId, code, msg, contract):
        from .broker import ET
        self.errors.append((datetime.now(ET).isoformat(), reqId, code, msg))
        if code not in (2104, 2106, 2158, 2108, 2119):
            log.warning("IB error %s (req %s): %s", code, reqId, msg)

    def connect(self):
        cfg = self.cfg
        port = _dial_port(cfg)
        redact_account(cfg.account)
        self.ib.connect(
            cfg.host, port, clientId=cfg.client_id, timeout=cfg.connect_timeout_s,
            readonly=bool(cfg.dry_run), account=cfg.account or "")
        self.verified_account = self.verify_accounts()
        redact_account(self.verified_account)
        self.ib.reqMarketDataType(1)
        self._streams_started = False
        self.start_streams()

    def verify_accounts(self) -> str:
        """Check what TWS reports. Any mismatch closes the socket and raises SafetyError."""
        try:
            reported = list(self.ib.managedAccounts())
        except Exception as exc:  # noqa: BLE001
            self.disconnect()
            raise SafetyError(f"could not read the accounts TWS reports: {exc}") from exc
        try:
            return check_accounts(self.cfg, reported)
        except SafetyError:
            self.disconnect()
            raise

    def ensure_connected(self) -> bool:
        if self.ib.isConnected():
            return True
        log.warning("TWS connection lost — reconnecting")
        try:
            self.connect()
        except SafetyError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("reconnect failed: %s", exc)
            return False
        self._opt_tickers.clear()
        self._streams_started = False
        self.start_streams()
        return True

    def disconnect(self):
        try:
            self.ib.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def now(self):
        from .broker import ET
        return datetime.now(ET)

    def sleep(self, seconds):
        self.ib.sleep(seconds)

    def managed_accounts(self):
        return list(self.ib.managedAccounts())

    def net_liquidation(self, account):
        for row in self.ib.accountSummary(account):
            if row.tag == "NetLiquidation" and row.currency in ("USD", "BASE", ""):
                return _f(row.value)
        return None

    def _qualify_spx(self):
        from ib_async import Index
        if self.spx is None:
            self.spx = Index("SPX", "CBOE", "USD")
            self.ib.qualifyContracts(self.spx)

    def spx_hours(self):
        self._qualify_spx()
        details = self.ib.reqContractDetails(self.spx)
        if not details:
            return None, None
        contract = details[0]
        return (contract.liquidHours or contract.tradingHours), contract.timeZoneId

    def start_streams(self):
        if getattr(self, "_streams_started", False):
            return
        self._qualify_spx()
        self._spx_t = self.ib.reqMktData(self.spx, "", False, False)
        self._streams_started = True

    def _px(self, ticker) -> Px:
        if ticker is None:
            return Px(None, None)
        value = _f(ticker.last)
        if value is None:
            bid, ask = _f(ticker.bid), _f(ticker.ask)
            value = (bid + ask) / 2 if (bid and ask) else None
        return Px(value, to_et(ticker.time, self.cfg.tws_naive_tz), ticker.marketDataType, _f(ticker.high))

    def spx_px(self):
        return self._px(getattr(self, "_spx_t", None))

    def spx_bars_15m(self, days: int = 5):
        self._qualify_spx()
        bars = self.ib.reqHistoricalData(
            self.spx, endDateTime="", durationStr=f"{days} D", barSizeSetting="15 mins",
            whatToShow="TRADES", useRTH=True, formatDate=2, timeout=60)
        out = []
        for bar in bars or []:
            start = to_et(bar.date, self.cfg.tws_naive_tz)
            if start is None:
                continue
            out.append(Bar(start, bar.open, bar.high, bar.low, bar.close, getattr(bar, "volume", 0) or 0))
        return out

    def chain_strikes(self, expiry: str):
        self._qualify_spx()
        chains = self.ib.reqSecDefOptParams("SPX", "", "IND", self.spx.conId)
        for chain in chains:
            if chain.tradingClass == "SPXW" and chain.exchange == "SMART":
                if expiry in chain.expirations:
                    return sorted(float(k) for k in chain.strikes)
                return None
        return None

    def ensure_options(self, expiry, strikes):
        from ib_async import Option
        wanted = []
        seen = set()
        for strike in strikes:
            for right in ("C", "P"):
                key = (float(strike), right)
                if key not in seen:
                    seen.add(key)
                    wanted.append(key)
        missing = [key for key in wanted if key not in self._opt_contracts]
        contracts = [
            Option("SPX", expiry, strike, right, "SMART", multiplier="100", currency="USD",
                   tradingClass="SPXW")
            for strike, right in missing
        ]
        if contracts:
            for contract in self.ib.qualifyContracts(*contracts):
                if contract is not None and getattr(contract, "conId", 0):
                    self._opt_contracts[(float(contract.strike), contract.right)] = contract
        spot = self.spx_px().value or 0.0
        budget = self.cfg.max_option_lines

        def rank(key):
            return abs(key[0] - spot)

        keep = set(sorted(wanted, key=rank)[:budget])
        for key in list(self._opt_tickers):
            if key not in keep:
                self.ib.cancelMktData(self._opt_contracts[key])
                del self._opt_tickers[key]
        for key in sorted(keep, key=rank):
            if key in self._opt_tickers:
                continue
            contract = self._opt_contracts.get(key)
            if contract is None:
                continue
            self._opt_tickers[key] = self.ib.reqMktData(contract, "", False, False)

    def option_quotes(self):
        out = {}
        for key, ticker in self._opt_tickers.items():
            greeks = ticker.modelGreeks
            out[key] = OptQuote(
                bid=_f(ticker.bid), ask=_f(ticker.ask),
                iv=_f(greeks.impliedVol) if greeks else None,
                delta=_f(greeks.delta) if greeks else None,
                volume=_f(ticker.volume),
                time=to_et(ticker.time, self.cfg.tws_naive_tz),
                mdtype=ticker.marketDataType,
                con_id=ticker.contract.conId)
        return out

    def place_combo(self, legs, action, qty, limit, account, order_ref=None, tif="DAY"):
        from ib_async import ComboLeg, Contract, LimitOrder
        send_allowed(self.cfg, account, order_ref or "")
        if self.cfg.dry_run:
            raise SafetyError("place_combo called while dry")
        bag = Contract(symbol="SPX", secType="BAG", currency="USD", exchange="SMART")
        bag.comboLegs = [
            ComboLeg(conId=int(cid), ratio=abs(int(ratio)), action="BUY" if ratio > 0 else "SELL",
                     exchange="SMART")
            for cid, ratio in legs
        ]
        order = LimitOrder(action, int(qty), float(limit), tif=tif, account=account, transmit=True,
                           orderRef=order_ref)
        trade = self.ib.placeOrder(bag, order)
        self._trades[order.orderId] = trade
        log_order(self.cfg, logging.INFO, "placed %s %s x%s limit %.2f ref %s acct ...%s",
                  action, tif, int(qty), float(limit), order_ref, account_tail(account))
        return order.orderId

    def _is_own(self, trade) -> bool:
        return str(getattr(trade.order, "orderRef", "") or "").startswith(ORDER_PREFIX)

    def _matches(self, trade, handle) -> bool:
        order = trade.order
        if int(getattr(order, "orderId", -1)) != int(handle):
            return False
        if not self._is_own(trade):
            return False
        client = int(getattr(order, "clientId", 0) or 0)
        if client and client != int(self.cfg.client_id):
            return False
        return True

    def cancel(self, handle):
        if self.cfg.dry_run:
            raise SafetyError("cancel called while dry")
        trade = self._trades.get(handle)
        if trade is None or not self._is_own(trade):
            log_order(self.cfg, logging.WARNING,
                      "refusing to cancel order %s: not a Noon order", handle)
            return
        if not trade.isDone():
            log_order(self.cfg, logging.INFO, "cancelling order %s", handle)
            self.ib.cancelOrder(trade.order)

    def order_state(self, handle):
        trade = self._trades.get(handle)
        if trade is None:
            for candidate in list(self.ib.trades()) + list(self.ib.reqAllOpenOrders()):
                if self._matches(candidate, handle):
                    trade = self._trades[handle] = candidate
                    break
        if trade is None:
            raise KeyError(handle)
        status = trade.orderStatus
        filled = float(getattr(status, "filled", 0) or 0)
        avg = float(getattr(status, "avgFillPrice", 0) or 0)
        if filled <= 0 and getattr(status, "status", "") == "Filled":
            filled = float(getattr(trade.order, "totalQuantity", 0) or 0)
        if avg == 0 and filled:
            avg = float(getattr(trade.order, "lmtPrice", 0) or 0)
        qty = float(getattr(trade.order, "totalQuantity", 0) or 0)
        return {
            "status": status.status,
            "filled": filled,
            "remaining": max(qty - filled, 0.0),
            "avg_price": avg,
            "done": trade.isDone(),
        }

    def own_open_orders(self):
        out = []
        for trade in self.ib.reqAllOpenOrders():
            if not self._is_own(trade) or trade.isDone():
                continue
            order = trade.order
            self._trades[order.orderId] = trade
            out.append({
                "order_id": order.orderId,
                "action": order.action,
                "limit": float(order.lmtPrice),
                "ref": order.orderRef,
                "status": trade.orderStatus.status,
            })
        return out

    def modify_combo(self, handle, limit):
        raise SafetyError("refusing to reprice: noon breakout does not chase")
