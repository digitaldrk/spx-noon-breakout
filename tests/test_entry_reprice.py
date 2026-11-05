"""Paper entry repricing. Broker and quotes are in-memory; nothing connects."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from spx_noon.config import Config, SafetyError
from spx_noon.logbook import Outputs
from spx_noon.notify import Notifier
from spx_noon.pricing import (
    entry_reprice_cap,
    market_data_class,
    next_entry_limit,
    reprice_quote_block,
    require_combo_limit,
)
from spx_noon.resume import load, save
from spx_noon.runner import (
    _State,
    _claim_existing_entry,
    _load_saved,
    _maybe_enter,
    _maybe_reprice,
    _poll_working,
)
from spx_noon.strategy import OptQuote

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 5, 12, 1, tzinfo=ET)


def _cfg(tmp, **extra):
    data = dict(
        broker="sim", mode="paper", base_dir=str(tmp),
        entry_reprice_secs=15, exit_wait_s=1, loop_interval_s=5,
    )
    data.update(extra)
    cfg = Config(**data)
    cfg.ensure_dirs()
    return cfg


def _plan(limit=12.10, natural=12.30, mid=12.12):
    return SimpleNamespace(
        direction="PUT", right="P",
        long_strike=7425.0, short_strike=7370.0,
        long_delta=-0.75, short_delta=-0.43,
        width=55, em=56.0, spot=7379.0, pdh=7715.0, pdl=7635.0,
        mid=mid, natural=natural, limit=limit, slippage_per_side=0.05,
        entry_close=datetime(2026, 11, 5, 12, 0, tzinfo=ET),
        trigger_close=datetime(2026, 11, 5, 9, 45, tzinfo=ET),
        trigger_low=7591.0, trigger_high=7681.0,
        legs=lambda: [(11, 1), (12, -1)],
    )


def _working(plan, **extra):
    cap = entry_reprice_cap(plan.limit, plan.natural, 0.10)
    data = {
        "id": 10,
        "ref": "SPXNOON-20261105-1",
        "plan": plan,
        "date": "2026-11-05",
        "direction": plan.direction,
        "right": plan.right,
        "spx": plan.spot,
        "pdh": plan.pdh,
        "pdl": plan.pdl,
        "em": plan.em,
        "width": plan.width,
        "long_strike": plan.long_strike,
        "short_strike": plan.short_strike,
        "long_delta": plan.long_delta,
        "short_delta": plan.short_delta,
        "mid": plan.mid,
        "limit": plan.limit,
        "original_limit": plan.limit,
        "original_natural": plan.natural,
        "reprice_cap": cap,
        "reprice_attempts": 0,
        "placed_at": NOW - timedelta(seconds=30),
        "cancel_after": datetime(2026, 11, 5, 12, 15, tzinfo=ET),
        "account": "DU999001",
        "entry_close": plan.entry_close,
        "deadline": plan.entry_close + timedelta(hours=2),
        "legs": [[11, 1], [12, -1]],
        "status": "working",
    }
    data.update(extra)
    return data


def _quote(bid, ask, mdtype=1, age=1, now=NOW):
    stamp = None if age is None else now - timedelta(seconds=age)
    return OptQuote(bid, ask, delta=-0.5, con_id=1, time=stamp, mdtype=mdtype)


def _book(long_bid, long_ask, short_bid, short_ask, mdtype=1, age=1, now=NOW):
    long_q = _quote(long_bid, long_ask, mdtype, age, now)
    short_q = _quote(short_bid, short_ask, mdtype, age, now)
    long_q.con_id = 11
    short_q.con_id = 12
    return {(7425.0, "P"): long_q, (7370.0, "P"): short_q}


class BookBroker:
    """Quotes and order state the test sets. No socket and no order transmission."""

    def __init__(self, now=NOW, quotes=None):
        self.t = now
        self.quotes = quotes or {}
        self.orders = {}
        self.modifies = []
        self.cancels = []
        self.placed = []
        self.fail_modify = False
        self.open_orders = []
        self.fill_refs = set()
        self._oid = 100

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=float(seconds))

    def option_quotes(self):
        return self.quotes

    def chain_strikes(self, expiry):
        return [7425.0, 7370.0]

    def ensure_options(self, expiry, strikes):
        return None

    def place_combo(self, legs, action, qty, limit, account, order_ref=None, tif="DAY"):
        self._oid += 1
        self.orders[self._oid] = {
            "status": "Submitted", "filled": 0, "avg": 0.0, "done": False, "limit": float(limit),
        }
        self.placed.append({
            "id": self._oid, "action": action, "limit": float(limit), "tif": tif, "order_ref": order_ref,
        })
        return self._oid

    def modify_combo(self, handle, limit):
        if self.fail_modify:
            raise RuntimeError("modify rejected")
        self.modifies.append((handle, float(limit)))
        order = self.orders.setdefault(handle, {
            "status": "Submitted", "filled": 0, "avg": 0.0, "done": False,
        })
        order["limit"] = float(limit)

    def cancel(self, handle):
        self.cancels.append(handle)
        order = self.orders.setdefault(handle, {
            "status": "Submitted", "filled": 0, "avg": 0.0, "done": False,
        })
        order.update(order.get("on_cancel") or {"status": "Cancelled", "filled": 0, "avg": 0.0, "done": True})

    def order_state(self, handle):
        if handle not in self.orders:
            raise KeyError(handle)
        order = self.orders[handle]
        filled = float(order["filled"])
        return {
            "status": order["status"],
            "filled": filled,
            "remaining": max(1 - filled, 0),
            "avg_price": order["avg"],
            "done": order["done"],
        }

    def own_open_orders(self):
        return list(self.open_orders)

    def own_entry_refs(self):
        return set(self.fill_refs)


def _note(tmp):
    return Notifier(tmp / "logs", toast=False)


def test_cap_is_fixed_from_the_original_prices_and_rejects_the_unapproved_walk():
    assert entry_reprice_cap(35.30, 35.36, 0.10) == 35.35
    assert entry_reprice_cap(12.10, 12.30, 0.10) == 12.20
    first = entry_reprice_cap(12.10, 12.16, 0.10)
    assert first == 12.15
    assert entry_reprice_cap(12.10, 20.0, 0.10) == 12.20
    assert first == 12.15
    with pytest.raises(SafetyError, match="0.40"):
        Config(broker="ibkr", mode="paper", entry_reprice_max_add=0.40).validate()


def test_market_data_class_and_a_receive_stamp_is_not_freshness():
    assert market_data_class(1) == "live"
    assert market_data_class(2) == "frozen"
    assert market_data_class(3) == "delayed"
    assert market_data_class(4) == "delayed-frozen"
    assert market_data_class(None) == "missing"
    assert market_data_class(9) == "invalid"
    now = NOW
    live = _book(40.10, 40.30, 27.90, 28.10, mdtype=1, age=1, now=now)
    assert reprice_quote_block(live[(7425, "P")], live[(7370, "P")], now, 30) is None
    delayed = _book(40.10, 40.30, 27.90, 28.10, mdtype=3, age=1, now=now)
    assert "delayed" in reprice_quote_block(delayed[(7425, "P")], delayed[(7370, "P")], now, 30)
    frozen = _book(40.10, 40.30, 27.90, 28.10, mdtype=2, age=1, now=now)
    assert "frozen" in reprice_quote_block(frozen[(7425, "P")], frozen[(7370, "P")], now, 30)
    quiet = _book(40.10, 40.30, 27.90, 28.10, mdtype=1, age=90, now=now)
    assert "stream quiet" in reprice_quote_block(quiet[(7425, "P")], quiet[(7370, "P")], now, 30)
    unstamped = _book(40.10, 40.30, 27.90, 28.10, mdtype=1, age=None, now=now)
    assert "no receive stamp" in reprice_quote_block(unstamped[(7425, "P")], unstamped[(7370, "P")], now, 30)
    assert reprice_quote_block(None, live[(7370, "P")], now, 30) == "long missing"
    crossed = _book(40.40, 40.10, 27.90, 28.10, mdtype=1, age=1, now=now)
    assert "invalid" in reprice_quote_block(crossed[(7425, "P")], crossed[(7370, "P")], now, 30)


def test_one_nickel_toward_the_mid_and_not_through_it():
    assert next_entry_limit(12.10, 12.30, 12.17, 12.40) == 12.15
    assert next_entry_limit(12.15, 12.30, 12.17, 12.40) is None
    assert next_entry_limit(12.10, 12.20, 13.00, 13.20) == 12.15
    assert require_combo_limit(12.15) == 12.15
    with pytest.raises(ValueError, match="off-tick"):
        require_combo_limit(12.17)


def test_moving_mid_reprices_one_step_at_a_time_and_stops_at_the_original_cap(tmp_path):
    cfg = _cfg(tmp_path)
    plan = _plan()
    working = _working(plan)
    broker = BookBroker(quotes=_book(40.20, 40.40, 27.90, 28.10))
    broker.orders[10] = {"status": "PreSubmitted", "filled": 0, "avg": 0.0, "done": False}
    _maybe_reprice(cfg, broker, working, broker.order_state(10))
    assert broker.modifies == [(10, 12.15)]
    assert working["reprice_cap"] == 12.20
    broker.t += timedelta(seconds=20)
    broker.quotes = _book(41.00, 41.20, 27.90, 28.10, now=broker.t)
    _maybe_reprice(cfg, broker, working, broker.order_state(10))
    assert broker.modifies[-1] == (10, 12.20)
    broker.t += timedelta(seconds=20)
    _maybe_reprice(cfg, broker, working, broker.order_state(10))
    assert broker.modifies == [(10, 12.15), (10, 12.20)]
    assert working["reprice_cap"] == 12.20
    assert working["original_limit"] == 12.10


def test_bad_quotes_do_not_raise_or_spend_an_attempt(tmp_path):
    cfg = _cfg(tmp_path)
    working = _working(_plan())
    broker = BookBroker(quotes=_book(40.20, 40.40, 27.90, 28.10, mdtype=3))
    broker.orders[10] = {"status": "Submitted", "filled": 0, "avg": 0.0, "done": False}
    _maybe_reprice(cfg, broker, working, broker.order_state(10))
    broker.quotes = _book(40.20, 40.40, 27.90, 28.10, mdtype=2)
    _maybe_reprice(cfg, broker, working, broker.order_state(10))
    broker.quotes = {(7425.0, "P"): _quote(40.2, 40.4)}
    _maybe_reprice(cfg, broker, working, broker.order_state(10))
    broker.quotes = _book(40.40, 40.10, 27.90, 28.10)
    _maybe_reprice(cfg, broker, working, broker.order_state(10))
    assert broker.modifies == []
    assert working["reprice_attempts"] == 0
    assert working["limit"] == 12.10


def test_modify_failure_keeps_the_order_and_the_cap(tmp_path):
    cfg = _cfg(tmp_path)
    working = _working(_plan())
    broker = BookBroker(quotes=_book(40.20, 40.40, 27.90, 28.10))
    broker.orders[10] = {"status": "Submitted", "filled": 0, "avg": 0.0, "done": False}
    broker.fail_modify = True
    _maybe_reprice(cfg, broker, working, broker.order_state(10))
    assert working["limit"] == 12.10
    assert working["reprice_attempts"] == 1
    assert working["reprice_cap"] == 12.20
    broker.t += timedelta(seconds=1)
    _maybe_reprice(cfg, broker, working, broker.order_state(10))
    assert working["reprice_attempts"] == 1
    broker.fail_modify = False
    broker.t += timedelta(seconds=20)
    _maybe_reprice(cfg, broker, working, broker.order_state(10))
    assert broker.modifies == [(10, 12.15)]
    assert working["reprice_attempts"] == 2
    working["reprice_attempts"] = 6
    broker.t += timedelta(seconds=20)
    _maybe_reprice(cfg, broker, working, broker.order_state(10))
    assert broker.modifies == [(10, 12.15)]
    assert broker.placed == []


def test_deadline_cancels_without_another_reprice(tmp_path):
    cfg = _cfg(tmp_path)
    plan = _plan()
    working = _working(plan, cancel_after=NOW - timedelta(seconds=1))
    state = _State(working=working)
    broker = BookBroker(quotes=_book(41.0, 41.2, 27.9, 28.1))
    broker.orders[10] = {"status": "Submitted", "filled": 0, "avg": 0.0, "done": False}
    out = Outputs(cfg.state_dir)
    _poll_working(cfg, broker, _note(tmp_path), out, state)
    assert broker.modifies == []
    assert broker.cancels == [10]
    assert state.working is None
    assert out.read_trades(plan.entry_close.date())[-1]["exit_reason"] == "skip_no_fill"


def test_partial_fill_cancels_the_remainder_and_does_not_reprice(tmp_path):
    cfg = _cfg(tmp_path)
    working = _working(_plan())
    state = _State(working=working)
    broker = BookBroker()
    broker.orders[10] = {
        "status": "Submitted", "filled": 1, "avg": 12.2, "done": False,
        "on_cancel": {"status": "Cancelled", "filled": 1, "avg": 12.2, "done": True},
    }
    _poll_working(cfg, broker, _note(tmp_path), Outputs(cfg.state_dir), state)
    assert broker.modifies == []
    assert broker.cancels == [10]
    assert [order["action"] for order in broker.placed] == ["SELL"]
    assert state.working is None
    assert state.position["fill_price"] == 12.2
    assert state.done_for_day is True


def test_fill_during_cancel_opens_the_position(tmp_path):
    cfg = _cfg(tmp_path)
    working = _working(_plan(), cancel_after=NOW - timedelta(seconds=1))
    state = _State(working=working)
    broker = BookBroker()
    broker.orders[10] = {"status": "Submitted", "filled": 0, "avg": 0.0, "done": False,
                         "on_cancel": {"status": "Filled", "filled": 1, "avg": 12.15, "done": True}}
    out = Outputs(cfg.state_dir)
    _poll_working(cfg, broker, _note(tmp_path), out, state)
    assert state.position["fill_price"] == 12.15
    assert state.working is None
    assert out.read_trades(working["entry_close"].date()) == []
    assert broker.modifies == []


def test_unknown_restart_blocks_another_entry_and_keeps_the_stored_cap(tmp_path):
    cfg = _cfg(tmp_path)
    plan = _plan()
    saved = _working(plan)
    saved["reprice_cap"] = 12.20
    saved["limit"] = 12.15
    saved["date"] = "2026-11-05"
    saved["legs"] = [[11, 1], [12, -1]]
    saved.pop("plan")
    save(cfg.state_dir, saved)
    broker = BookBroker()
    state = _State()
    _load_saved(cfg, broker, state, load(cfg.state_dir))
    assert state.working["reprice_cap"] == 12.20
    assert state.working["reprice_stopped"] == "unreconciled"
    assert load(cfg.state_dir)["status"] == "working"
    _maybe_enter(cfg, broker, _note(tmp_path), None, Outputs(cfg.state_dir), state, "DU999001", [])
    assert broker.placed == []


def test_restart_does_not_raise_the_stored_cap(tmp_path):
    cfg = _cfg(tmp_path)
    plan = _plan()
    saved = _working(plan, reprice_cap=12.20, limit=12.15)
    saved["date"] = "2026-11-05"
    saved["legs"] = [[11, 1], [12, -1]]
    saved.pop("plan")
    broker = BookBroker(quotes=_book(41.0, 41.2, 27.9, 28.1))
    broker.orders[10] = {"status": "PreSubmitted", "filled": 0, "avg": 0.0, "done": False}
    state = _State()
    _load_saved(cfg, broker, state, saved)
    assert state.working["reprice_cap"] == 12.20
    _maybe_reprice(cfg, broker, state.working, broker.order_state(10))
    assert broker.modifies == [(10, 12.20)]
    assert state.working["reprice_cap"] == 12.20


def test_a_fill_report_without_a_price_blocks_a_second_entry(tmp_path):
    cfg = _cfg(tmp_path)
    saved = _working(_plan())
    saved["date"] = "2026-11-05"
    saved["legs"] = [[11, 1], [12, -1]]
    saved.pop("plan")
    broker = BookBroker()
    broker.fill_refs.add("SPXNOON-20261105-1")
    state = _State()
    _load_saved(cfg, broker, state, saved)
    assert state.done_for_day is True
    assert state.halt is True
    assert state.position is None
    assert state.working is None
    _maybe_enter(cfg, broker, _note(tmp_path), None, Outputs(cfg.state_dir), state, "DU999001", [])
    assert broker.placed == []


def test_an_open_entry_is_adopted_instead_of_sending_another(tmp_path):
    cfg = _cfg(tmp_path)
    broker = BookBroker()
    broker.open_orders = [{
        "order_id": 77, "action": "BUY", "limit": 31.60,
        "ref": "SPXNOON-20261105-2", "status": "PreSubmitted",
    }]
    state = _State()
    assert _claim_existing_entry(cfg, broker, state) is True
    assert state.working["id"] == 77
    assert state.working["reprice_cap"] == 31.60
    assert state.working["reprice_stopped"] == "adopted"
    _maybe_enter(cfg, broker, _note(tmp_path), None, Outputs(cfg.state_dir), state, "DU999001", [])
    assert broker.placed == []


def test_live_and_dry_do_not_reprice(tmp_path):
    assert Config(broker="tasty", mode="live", base_dir=str(tmp_path)).entry_reprice_on() is False
    assert Config(broker="schwab", mode="live", account="705", base_dir=str(tmp_path)).entry_reprice_on() is False
    assert Config(broker="ibkr", mode="dry", base_dir=str(tmp_path)).entry_reprice_on() is False
    paper = Config(broker="ibkr", mode="paper", base_dir=str(tmp_path))
    assert paper.entry_reprice_on() is True
    paper.entry_reprice = False
    assert paper.entry_reprice_on() is False
    cfg = _cfg(tmp_path, mode="dry")
    working = _working(_plan())
    broker = BookBroker(quotes=_book(41.0, 41.2, 27.9, 28.1))
    broker.orders[10] = {"status": "Submitted", "filled": 0, "avg": 0.0, "done": False}
    _maybe_reprice(cfg, broker, working, broker.order_state(10))
    assert broker.modifies == []


def test_ib_modify_stays_a_paper_limit_on_the_tick(tmp_path):
    from spx_noon.ib import IBBroker

    dry = IBBroker(Config(broker="ibkr", mode="dry", base_dir=str(tmp_path)))
    with pytest.raises(SafetyError, match="paper"):
        dry.modify_combo(5, 12.15)

    cfg = Config(broker="ibkr", mode="paper", base_dir=str(tmp_path), account="DU999001")
    broker = IBBroker(cfg)
    sent = []
    broker.ib = SimpleNamespace(placeOrder=lambda contract, order: sent.append((contract, order)))
    with pytest.raises(SafetyError, match="off-tick"):
        broker.modify_combo(5, 12.17)
    assert sent == []

    order = SimpleNamespace(
        orderType="LMT", lmtPrice=12.10, orderRef="SPXNOON-20261105-1",
        clientId=41, orderId=5,
    )
    trade = SimpleNamespace(
        order=order,
        orderStatus=SimpleNamespace(status="PreSubmitted", filled=0),
        contract=object(),
        isDone=lambda: False,
    )
    broker._trades[5] = trade
    broker.modify_combo(5, 12.15)
    assert sent == [(trade.contract, order)]
    assert order.lmtPrice == 12.15
    assert order.orderType == "LMT"
    with pytest.raises(SafetyError, match="lower"):
        broker.modify_combo(5, 12.10)
    order.orderType = "MKT"
    with pytest.raises(SafetyError, match="non-limit"):
        broker.modify_combo(5, 12.20)
    assert len(sent) == 1
