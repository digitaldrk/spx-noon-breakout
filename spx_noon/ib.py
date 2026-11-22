"""IBBroker: Mac path. IBKR TWS on localhost.

mode ``paper`` is port 7497 and account ids starting with D. mode ``live`` is
real money on 7496 (TWS) or 4001 (IB Gateway) with a configured account that
does not start with D, and TWS has to report that same account. mode ``dry``
and ``alerts`` connect read-only on 7497 and never send.

Orders carry orderRef SPXNOON-. The bot never modifies an order that does not.
Entry orders are DAY. The take-profit is GTC, as the plan specifies.

Every call that takes a reqId is counted by caller and summarised every five
minutes, so a paper log shows the market-data request rate without any account
id, order ref, or con id in it.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime

from .broker import Broker, select_option_lines, to_et
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

#: How often the request summary goes to the log, in seconds of broker time.
REQ_LOG_EVERY_S = 300.0


class _ReqMeter:
    """Counts the IB calls that take a reqId, by caller.

    Only call counts and the length of the window are kept, so the summary line
    carries no account id, no order ref, and no con id. ``reqMktData`` and
    ``cancelMktData`` are counted apart, since they are the pair that churned;
    the other reqId callers are counted together as ``other`` so one line shows
    the split.
    """

    #: Every caller tag, in the order the summary prints them.
    CALLERS = ("ensure_options", "spx", "qualify", "historical", "chain",
               "contract_details", "open_orders")

    def __init__(self, started, every_s: float = REQ_LOG_EVERY_S):
        self.started = started
        self.every_s = float(every_s)
        self.req: dict[str, int] = {}
        self.cancel: dict[str, int] = {}
        self.other: dict[str, int] = {}

    def _book(self, kind: str) -> dict:
        return {"req": self.req, "cancel": self.cancel, "other": self.other}[kind]

    def count(self, kind: str, caller: str, n: int = 1) -> None:
        book = self._book(kind)
        book[caller] = book.get(caller, 0) + int(n)

    def due(self, now) -> bool:
        return (now - self.started).total_seconds() >= self.every_s

    def summary(self, now) -> str:
        minutes = max((now - self.started).total_seconds(), 0.0) / 60.0
        reqs, cancels = sum(self.req.values()), sum(self.cancel.values())
        others = sum(self.other.values())

        def rate(total: int) -> float:
            return total / minutes if minutes > 0 else 0.0

        split = []
        for caller in self.CALLERS:
            shown = " ".join(
                f"{label}={self._book(kind).get(caller, 0)}"
                for kind, label in (("req", "req"), ("cancel", "cancel"), ("other", "calls"))
                if self._book(kind).get(caller, 0)
            )
            split.append(f"{caller} {shown or '0'}")
        return (f"market data over {minutes:.1f} min: reqMktData {reqs} ({rate(reqs):.1f}/min) "
                f"cancelMktData {cancels} ({rate(cancels):.1f}/min) other reqIds {others}; "
                + ", ".join(split))

    def flush(self, now, logger=log) -> str:
        """Log the window and start a new one."""
        line = self.summary(now)
        logger.info("%s", line)
        self.started = now
        self.req.clear()
        self.cancel.clear()
        self.other.clear()
        return line


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
    if int(cfg.ibkr_port) != PAPER_PORT:
        raise SafetyError(f"refusing: port {cfg.ibkr_port} is not {PAPER_PORT}")
    if cfg.ibkr_host not in LOCALHOSTS:
        raise SafetyError(f"refusing: host {cfg.ibkr_host!r} is not localhost")
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
    if int(cfg.ibkr_port) not in LIVE_PORTS:
        raise SafetyError(f"refusing: port {cfg.ibkr_port} is not an IBKR live port {LIVE_PORTS}")
    if cfg.ibkr_host not in LOCALHOSTS:
        raise SafetyError(f"refusing: host {cfg.ibkr_host!r} is not localhost")
    acct = str(account)
    if not acct.strip():
        raise SafetyError("refusing: a live order needs the configured live account")
    if acct.startswith("D"):
        raise SafetyError("refusing: a live order must not go to a paper (D...) account")
    if acct != str(cfg.broker_account):
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
    want = str(getattr(cfg, "broker_account", "") or "")
    if getattr(cfg, "mode", "") == "live":
        if not cfg.ibkr_live:
            raise SafetyError("refusing: this config is not IBKR live")
        if not want.strip():
            raise SafetyError("refusing: live mode needs broker_account set to the live account id")
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
    port = int(cfg.ibkr_port)
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
        #: When each line was requested, and when each held line left the wanted
        #: set. Both feed :func:`select_option_lines`.
        self._opt_since: dict = {}
        self._opt_cold: dict = {}
        self._opt_pins: set = set()
        self._opt_pin_expiry = ""
        self._reqs = _ReqMeter(self.now())
        self._trades: dict = {}
        self.spx = None
        self.verified_account: str | None = None

    def _store(self, name: str) -> dict:
        """A private dict, created on first use.

        ``tests/test_safety.py`` builds this broker with ``__new__`` and sets only
        the attributes it needs, so nothing below may assume ``__init__`` ran.
        """
        book = getattr(self, name, None)
        if book is None:
            book = {}
            setattr(self, name, book)
        return book

    def _meter(self) -> _ReqMeter:
        meter = getattr(self, "_reqs", None)
        if meter is None:
            meter = self._reqs = _ReqMeter(self.now())
        return meter

    def _count(self, kind: str, caller: str, n: int = 1) -> None:
        """Count one reqId-taking call and log the window when it is up."""
        meter = self._meter()
        meter.count(kind, caller, n)
        now = self.now()
        if meter.due(now):
            meter.flush(now, log)

    def _on_error(self, reqId, code, msg, contract):
        from .broker import ET
        self.errors.append((datetime.now(ET).isoformat(), reqId, code, msg))
        if code not in (2104, 2106, 2158, 2108, 2119):
            log.warning("IB error %s (req %s): %s", code, reqId, msg)

    def connect(self):
        cfg = self.cfg
        port = _dial_port(cfg)
        redact_account(cfg.broker_account)
        self.ib.connect(
            cfg.ibkr_host, port, clientId=cfg.ibkr_client_id, timeout=cfg.connect_timeout_s,
            readonly=bool(cfg.dry_run), account=cfg.broker_account or "")
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
        # A real reconnect drops TWS's subscriptions, so the local book and its
        # clocks go with them and start_streams requests SPX again.
        self._opt_tickers.clear()
        self._store("_opt_since").clear()
        self._store("_opt_cold").clear()
        self._streams_started = False
        self.start_streams()
        return True

    def disconnect(self):
        meter = getattr(self, "_reqs", None)
        if meter is not None:
            try:
                meter.flush(self.now(), log)
            except Exception:  # noqa: BLE001
                pass
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
            self._count("other", "qualify")
            self.ib.qualifyContracts(self.spx)

    def spx_hours(self):
        self._qualify_spx()
        self._count("other", "contract_details")
        details = self.ib.reqContractDetails(self.spx)
        if not details:
            return None, None
        contract = details[0]
        return (contract.liquidHours or contract.tradingHours), contract.timeZoneId

    def start_streams(self):
        if getattr(self, "_streams_started", False):
            return
        self._qualify_spx()
        self._count("req", "spx")
        self._spx_t = self.ib.reqMktData(self.spx, "", False, False)
        self._streams_started = True

    def _px(self, ticker) -> Px:
        if ticker is None:
            return Px(None, None)
        value = _f(ticker.last)
        if value is None:
            bid, ask = _f(ticker.bid), _f(ticker.ask)
            value = (bid + ask) / 2 if (bid and ask) else None
        return Px(value, to_et(ticker.time, self.cfg.ibkr_tws_naive_tz), ticker.marketDataType, _f(ticker.high))

    def spx_px(self):
        return self._px(getattr(self, "_spx_t", None))

    def spx_bars_15m(self, days: int = 5):
        self._qualify_spx()
        self._count("other", "historical")
        bars = self.ib.reqHistoricalData(
            self.spx, endDateTime="", durationStr=f"{days} D", barSizeSetting="15 mins",
            whatToShow="TRADES", useRTH=True, formatDate=2, timeout=60)
        out = []
        for bar in bars or []:
            start = to_et(bar.date, self.cfg.ibkr_tws_naive_tz)
            if start is None:
                continue
            out.append(Bar(start, bar.open, bar.high, bar.low, bar.close, getattr(bar, "volume", 0) or 0))
        return out

    def chain_strikes(self, expiry: str):
        self._qualify_spx()
        self._count("other", "chain")
        chains = self.ib.reqSecDefOptParams("SPX", "", "IND", self.spx.conId)
        for chain in chains:
            if chain.tradingClass == "SPXW" and chain.exchange == "SMART":
                if expiry in chain.expirations:
                    return sorted(float(k) for k in chain.strikes)
                return None
        return None

    def pin_options(self, expiry, legs):
        """Hold these legs until the pins are cleared. ``legs`` is ``None`` to clear.

        The pins belong to one expiry: a pin set for another day is ignored
        rather than held against today's book.
        """
        self._opt_pin_expiry = str(expiry or "")
        self._opt_pins = {(float(strike), str(right)) for strike, right in (legs or ())}

    def _pins_for(self, expiry) -> set:
        if str(expiry or "") != str(getattr(self, "_opt_pin_expiry", "") or ""):
            return set()
        return set(getattr(self, "_opt_pins", None) or ())

    def ensure_options(self, expiry, strikes):
        """Subscribe the lines the selector wants, cancel only what it dropped.

        A line is requested once and reused. Nothing is cancelled because the
        spot moved a little, and a pinned leg is never cancelled at all, so the
        5-second management loop stops opening new request ids.
        """
        from ib_async import Option
        pins = self._pins_for(expiry)
        wanted = []
        seen = set()
        for strike in strikes:
            for right in ("C", "P"):
                key = (float(strike), right)
                if key not in seen:
                    seen.add(key)
                    wanted.append(key)
        # A pin that is outside the passed band still needs its contract.
        for key in sorted(pins):
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
            self._count("other", "qualify")
            for contract in self.ib.qualifyContracts(*contracts):
                if contract is not None and getattr(contract, "conId", 0):
                    self._opt_contracts[(float(contract.strike), contract.right)] = contract
        known = self._opt_contracts
        now = self.now()
        since = self._store("_opt_since")
        cold = self._store("_opt_cold")
        selected = select_option_lines(
            [key for key in wanted if key in known],
            spot=self.spx_px().value,
            now=now,
            budget=self.cfg.max_option_lines,
            subscribed_at={key: since.get(key, now) for key in self._opt_tickers},
            pinned={key for key in pins if key in known or key in self._opt_tickers},
            hold_s=self.cfg.option_line_hold_s,
            undesired_since=cold,
        )
        keep = selected.keep
        self._opt_cold = dict(selected.undesired_since)
        for key in list(self._opt_tickers):
            if key in keep:
                continue
            contract = known.get(key)
            if contract is not None:
                self._count("cancel", "ensure_options")
                self.ib.cancelMktData(contract)
            del self._opt_tickers[key]
            since.pop(key, None)
        for key in sorted(keep):
            if key in self._opt_tickers:
                continue
            contract = known.get(key)
            if contract is None:
                continue
            self._count("req", "ensure_options")
            self._opt_tickers[key] = self.ib.reqMktData(contract, "", False, False)
            since[key] = now

    def option_quotes(self):
        out = {}
        for key, ticker in self._opt_tickers.items():
            greeks = ticker.modelGreeks
            out[key] = OptQuote(
                bid=_f(ticker.bid), ask=_f(ticker.ask),
                iv=_f(greeks.impliedVol) if greeks else None,
                delta=_f(greeks.delta) if greeks else None,
                volume=_f(ticker.volume),
                time=to_et(ticker.time, self.cfg.ibkr_tws_naive_tz),
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
        if client and client != int(self.cfg.ibkr_client_id):
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
            self._count("other", "open_orders")
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
        self._count("other", "open_orders")
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
