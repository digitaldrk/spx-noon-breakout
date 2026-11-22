"""Option market-data lines: subscribe once, hold, and never drop a pinned leg.

The 2026-11-17 IBKR paper session opened about 67,700 request ids in an hour
(~1,100 a minute) with one PUT 7360/7325 open: the position loop asked for the
whole band and then asked again for the two legs, five seconds apart, so every
line was cancelled and re-requested on the next pass. These tests hold that shut
on the IBKR selector, the IBKR adapter, and the runner's position path.

Nothing here opens a socket or sleeps out the hold window. The clock is a
callable the broker reads in place of ``now()``, so the hold is driven by
moving that clock.
"""
import csv
import re
from collections import Counter
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from spx_noon.broker import SimBroker
from spx_noon.config import Config
from spx_noon.ib import IBBroker, OPTION_LINE_HOLD_S, _ReqMeter, select_option_lines
from spx_noon.logbook import Outputs
from spx_noon.notify import Notifier
from spx_noon.runner import (
    _State,
    _ensure_position_quotes,
    _load_saved,
    _poll_working,
    run_session,
)
from spx_noon.strategy import Px

from test_rules import levels, today_call_bars
from test_session import _quotes

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 11, 17, 12, 0, tzinfo=ET)
EXPIRY = "20261117"
#: 61 strikes at the 5-point step: ±150 points, the decision band, 122 lines.
BAND = [7000.0 + 5.0 * step for step in range(-30, 31)]


def _lines(strikes, rights=("C", "P")):
    return [(float(strike), right) for strike in strikes for right in rights]


# --------------------------------------------------------------------------
# The selector, with no broker at all
# --------------------------------------------------------------------------

def _select(candidates, **extra):
    args = dict(spot=7000.0, now=T0, budget=4)
    args.update(extra)
    return select_option_lines(candidates, **args)


def test_the_hold_default_is_sixty_seconds_and_the_config_can_zero_it():
    assert OPTION_LINE_HOLD_S == 60.0
    assert Config(broker="sim", mode="paper").option_line_hold_s == 60.0
    Config(broker="sim", mode="paper", option_line_hold_s=0).validate()
    cfg = Config(broker="sim", mode="paper", option_line_hold_s=-1)
    try:
        cfg.validate()
    except ValueError as exc:
        assert "option_line_hold_s" in str(exc)
    else:
        raise AssertionError("a negative hold has to be refused")


def test_a_repeated_call_wants_exactly_what_is_already_subscribed():
    candidates = _lines([6990, 6995, 7000, 7005, 7010])
    first = _select(candidates, budget=6)
    assert len(first.keep) == 6
    second = _select(
        candidates, budget=6, now=T0 + timedelta(seconds=5),
        subscribed_at={key: T0 for key in first.keep},
        undesired_since=first.undesired_since,
    )
    assert second.keep == first.keep
    assert second.undesired_since == {}


def test_ties_are_broken_by_strike_and_right_not_by_the_callers_order():
    jumbled = [(7005.0, "P"), (6995.0, "C"), (7005.0, "C"), (6995.0, "P")]
    keep = _select(jumbled, budget=2).keep
    assert keep == {(6995.0, "C"), (6995.0, "P")}
    assert _select(list(reversed(jumbled)), budget=2).keep == keep


def test_the_edge_line_is_held_through_a_small_spot_move():
    candidates = _lines([6995, 7000, 7005])
    first = _select(candidates)
    assert first.keep == {(6995.0, "C"), (6995.0, "P"), (7000.0, "C"), (7000.0, "P")}
    held = {key: T0 for key in first.keep}
    # 3 points is less than the 5-point strike step, but it re-ranks the book.
    moved = _select(candidates, spot=7003.0, now=T0 + timedelta(seconds=5), subscribed_at=held)
    assert moved.keep == first.keep           # nothing cancelled
    assert set(moved.undesired_since) == {(6995.0, "C"), (6995.0, "P")}


def test_an_aged_out_line_is_dropped_once_and_is_not_asked_for_again():
    candidates = _lines([6995, 7000, 7005])
    held = {key: T0 for key in _select(candidates).keep}
    moved = _select(candidates, spot=7003.0, now=T0 + timedelta(seconds=5), subscribed_at=held)
    aged = _select(candidates, spot=7003.0, now=T0 + timedelta(seconds=65),
                   subscribed_at=held, undesired_since=moved.undesired_since)
    assert aged.keep == {(7000.0, "C"), (7000.0, "P"), (7005.0, "C"), (7005.0, "P")}
    # One swap, not a swap a loop: the next call wants the same four lines and
    # does not ask for 6995 again.
    after = _select(candidates, spot=7003.0, now=T0 + timedelta(seconds=70),
                    subscribed_at={key: T0 for key in aged.keep},
                    undesired_since=aged.undesired_since)
    assert after.keep == aged.keep
    assert after.undesired_since == {}


def test_a_zero_hold_drops_the_line_on_the_call_that_stops_wanting_it():
    candidates = _lines([6995, 7000, 7005])
    held = {key: T0 for key in _select(candidates).keep}
    now = _select(candidates, spot=7003.0, now=T0 + timedelta(seconds=5),
                  subscribed_at=held, hold_s=0)
    assert (6995.0, "C") not in now.keep and (7005.0, "C") in now.keep


def test_a_pinned_leg_outlives_a_band_that_walked_away():
    pins = {(7360.0, "P"), (7325.0, "P")}
    held = {key: T0 for key in pins}
    # The decision band is 1,300 points below the open spread's legs.
    far = _select(_lines([5990, 5995, 6000]), spot=6000.0, subscribed_at=held, pinned=pins)
    assert pins <= far.keep
    assert len(far.keep) == 4
    assert far.undesired_since == {}          # a pin never ages
    # And a pin that is not up yet is subscribed ahead of any unpinned line.
    cold_start = _select(_lines([5990, 5995, 6000]), spot=6000.0, pinned=pins)
    assert pins <= cold_start.keep


def test_the_budget_is_the_hard_cap_even_for_pins():
    pins = {(7360.0, "P"), (7325.0, "P")}
    # Two pins and room for one line: the nearer pin is kept and nothing else.
    tight = _select(_lines(BAND), budget=1, pinned=pins)
    assert tight.keep == {(7325.0, "P")}
    # Pins plus the nearest unpinned candidates, never over the budget.
    held = {key: T0 for key in _select(_lines(BAND), budget=8).keep}
    for spot in (7000.0, 7002.0, 7008.0, 6991.0):
        picked = _select(_lines(BAND), spot=spot, budget=8, subscribed_at=held, pinned=pins)
        assert len(picked.keep) <= 8
        assert pins <= picked.keep
        held = {key: T0 for key in picked.keep}


def test_a_missing_spot_keeps_the_book_and_never_ranks_against_zero():
    candidates = _lines([5, 10, 6995, 7000])
    held = {key: T0 for key in _lines([7000])}
    for spot in (None, 0.0, -1.0, float("nan"), float("inf"), "n/a"):
        picked = _select(candidates, spot=spot, budget=4, subscribed_at=held)
        assert set(held) <= picked.keep           # nothing cancelled
        assert picked.keep == set(held)           # and no lowest-strike guesswork
    # A missing pin is still worth having, while the budget has room.
    pin = {(7360.0, "P")}
    room = _select(candidates, spot=None, budget=4, subscribed_at=held, pinned=pin)
    assert room.keep == set(held) | pin
    full = {key: T0 for key in _lines([6995, 7000])}
    tight = _select(candidates, spot=None, budget=4, subscribed_at=full, pinned=pin)
    assert tight.keep == set(full)                # no room, and still no cancel


# --------------------------------------------------------------------------
# IBKR: the fake broker, the request counter, and the old double call
# --------------------------------------------------------------------------

class _FakeIB:
    """Enough of ``ib_async.IB`` for ``ensure_options``, counting every reqId."""

    def __init__(self):
        self.req = 0
        self.cancel = 0
        self.requested: list = []
        self.cancelled: list = []
        self.disconnects = 0
        self._con_id = 70000

    def qualifyContracts(self, *contracts):
        out = []
        for contract in contracts:
            self._con_id += 1
            contract.conId = self._con_id
            out.append(contract)
        return out

    def reqMktData(self, contract, *args):
        self.req += 1
        self.requested.append((float(getattr(contract, "strike", 0) or 0),
                               getattr(contract, "right", "")))
        return SimpleNamespace(contract=contract)

    def cancelMktData(self, contract):
        self.cancel += 1
        self.cancelled.append((float(getattr(contract, "strike", 0) or 0),
                               getattr(contract, "right", "")))

    def disconnect(self):
        self.disconnects += 1


class _Market:
    """The clock and the SPX print the broker reads. Both move inside a test."""

    def __init__(self, now=T0, spot=7000.0):
        self.t = now
        self.spot = spot

    def tick(self, seconds: float):
        self.t = self.t + timedelta(seconds=float(seconds))

    def now(self):
        return self.t

    def px(self):
        return Px(self.spot, self.t, 1, None)


def _ib(tmp_path, *, lines=120, hold_s=OPTION_LINE_HOLD_S, spot=7000.0):
    """``IBBroker`` without ib_async's IB, the way ``tests/test_safety.py`` does."""
    cfg = Config(broker="ibkr", mode="paper", base_dir=str(tmp_path),
                 max_option_lines=lines, option_line_hold_s=hold_s)
    cfg.validate()
    broker = IBBroker.__new__(IBBroker)
    broker.cfg = cfg
    broker.ib = _FakeIB()
    broker._opt_tickers = {}
    broker._opt_contracts = {}
    broker._trades = {}
    broker.spx = None
    broker.verified_account = None
    broker._streams_started = False
    market = _Market(spot=spot)
    broker.now = market.now
    broker.spx_px = market.px
    return broker, broker.ib, market


def _windows(records):
    """``(minutes, reqMktData, per minute)`` out of every summary line logged."""
    out = []
    for message in [rec.message for rec in records if "market data over" in rec.message]:
        found = re.search(r"over ([\d.]+) min: reqMktData (\d+) \(([\d.]+)/min\)", message)
        assert found, message
        out.append((float(found.group(1)), int(found.group(2)), float(found.group(3))))
    return out


def test_the_old_double_call_pattern_stops_opening_new_request_ids(tmp_path, caplog):
    """Criterion 1: the band, then the two legs, every five seconds, for ten minutes."""
    broker, ib, market = _ib(tmp_path)
    legs = [(6980.0, "P"), (7015.0, "P")]
    broker.pin_options(EXPIRY, legs)
    broker.ensure_options(EXPIRY, BAND)
    assert ib.req == 120 and ib.cancel == 0        # 122 lines, 120 of budget
    assert ib.requested[:2] == [(7000.0, "C"), (7000.0, "P")]   # nearest first
    opening = ib.req
    broker._reqs.flush(market.now())               # measure the loop, not the open
    started = market.now()
    with caplog.at_level("INFO", logger="spx_noon.ib"):
        for loop in range(120):                    # 12 loops a minute, ten minutes
            broker.ensure_options(EXPIRY, BAND)
            broker.ensure_options(EXPIRY, [strike for strike, _right in legs])
            if loop < 4:
                market.spot += 0.5                 # 2 points: less than one strike
            market.tick(5)
        broker.disconnect()                        # the last window flushes here
    minutes = (market.now() - started).total_seconds() / 60.0
    assert minutes == 10.0
    assert Counter(ib.requested).most_common(1)[0][1] == 1      # nothing re-requested
    assert len(broker._opt_tickers) <= 120
    assert set(legs) <= set(broker._opt_tickers)
    assert opening == 120
    # Two five-minute lines, and every one of them near zero, not hundreds.
    windows = _windows(caplog.records)
    assert [window for window, _reqs, _rate in windows] == [5.0, 5.0]
    assert sum(reqs for _window, reqs, _rate in windows) <= 4
    assert all(rate < 1.0 for _window, _reqs, rate in windows)


def test_the_position_path_cancels_the_band_once_and_never_re_requests_it(tmp_path):
    """The band ages out 60s after the position path takes over, and that is all."""
    broker, ib, market = _ib(tmp_path)
    legs = [(6980.0, "P"), (7015.0, "P")]
    broker.pin_options(EXPIRY, legs)
    broker.ensure_options(EXPIRY, BAND)
    ib.req = ib.cancel = 0
    for _ in range(12):                            # the loops inside the 60s hold
        broker.ensure_options(EXPIRY, [strike for strike, _right in legs])
        market.tick(5)
    assert (ib.req, ib.cancel) == (0, 0)
    broker.ensure_options(EXPIRY, [strike for strike, _right in legs])   # 60s on
    cancelled = ib.cancel
    assert cancelled == 116                        # 120 held, 4 still wanted
    assert set(legs) <= set(broker._opt_tickers)
    for _ in range(60):
        market.tick(5)
        broker.ensure_options(EXPIRY, [strike for strike, _right in legs])
    assert ib.req == 0 and ib.cancel == cancelled
    assert not [key for key in ib.cancelled if key in set(legs)]


def test_a_pinned_leg_survives_a_far_band_and_a_spot_jump(tmp_path):
    """Criterion 3: the legs stay from the pin until the pins are cleared."""
    broker, ib, market = _ib(tmp_path)
    legs = [(7360.0, "P"), (7325.0, "P")]
    broker.pin_options(EXPIRY, legs)
    broker.ensure_options(EXPIRY, [strike for strike, _right in legs])
    assert set(legs) <= set(broker._opt_tickers)
    market.spot = 6500.0                           # a jump of 500 points
    far = [6500.0 + 5.0 * step for step in range(-30, 31)]
    for _ in range(40):                            # well past the 60s hold
        broker.ensure_options(EXPIRY, far)
        market.tick(5)
    assert set(legs) <= set(broker._opt_tickers)
    assert len(broker._opt_tickers) <= 120
    assert not [key for key in ib.cancelled if key in set(legs)]
    # Flat and nothing working: the pins are cleared and the legs may age out.
    broker.pin_options("", None)
    for _ in range(20):
        broker.ensure_options(EXPIRY, far)
        market.tick(5)
    assert not set(legs) & set(broker._opt_tickers)
    # A pin set for another expiry is not held against today's book.
    broker.pin_options("20261118", legs)
    assert broker._pins_for(EXPIRY) == set()


def test_a_missing_spx_spot_cancels_nothing_and_subscribes_no_zero_strikes(tmp_path):
    """Criterion 4: no spot is not a spot of 0."""
    broker, ib, market = _ib(tmp_path, lines=4)
    broker.ensure_options(EXPIRY, [7000.0, 7005.0])
    held = set(broker._opt_tickers)
    assert len(held) == 4
    ib.req = ib.cancel = 0
    market.spot = None
    broker.ensure_options(EXPIRY, [5.0, 10.0, 7000.0, 7005.0])
    assert (ib.req, ib.cancel) == (0, 0)
    assert set(broker._opt_tickers) == held
    # A pin with no room waits; with room it is subscribed, spot or no spot.
    broker.pin_options(EXPIRY, [(7360.0, "P")])
    broker.ensure_options(EXPIRY, [5.0, 7000.0])
    assert (ib.req, ib.cancel) == (0, 0)
    broker.cfg.max_option_lines = 5
    broker.ensure_options(EXPIRY, [5.0, 7000.0])
    assert ib.req == 1 and ib.cancel == 0
    assert (7360.0, "P") in broker._opt_tickers
    assert not [key for key, _right in ib.requested if key < 6000]


def test_a_reconnect_is_the_only_thing_that_drops_the_local_book(tmp_path):
    broker, ib, market = _ib(tmp_path, lines=4)
    broker.ensure_options(EXPIRY, [7000.0, 7005.0])
    assert len(broker._opt_tickers) == 4 and len(broker._opt_since) == 4
    broker.ib.connected = False
    broker.connect = lambda: None
    broker.ib.isConnected = lambda: False
    broker.start_streams = lambda: None
    assert broker.ensure_connected() is True
    assert broker._opt_tickers == {} and broker._opt_since == {}
    assert broker._opt_cold == {}


# --------------------------------------------------------------------------
# The request counter and its summary line
# --------------------------------------------------------------------------

def test_the_counter_splits_the_callers_and_logs_no_identifiers(tmp_path, caplog):
    broker, ib, market = _ib(tmp_path, lines=4)
    ib.reqSecDefOptParams = lambda *args: []
    ib.reqContractDetails = lambda contract: []
    ib.reqAllOpenOrders = lambda: []
    ib.trades = lambda: []
    broker.spx = SimpleNamespace(conId=416904, strike=0.0, right="")
    broker.ensure_options(EXPIRY, [7000.0, 7005.0])
    broker.chain_strikes(EXPIRY)
    broker.spx_hours()
    broker.own_open_orders()
    counts = broker._reqs
    assert counts.req == {"ensure_options": 4}
    # qualifyContracts fans out to one reqContractDetails per contract: 4 lines,
    # 4 request ids, not one call.
    assert counts.other == {"qualify": 4, "chain": 1, "contract_details": 1, "open_orders": 1}
    line = counts.summary(market.now() + timedelta(minutes=5))
    assert "reqMktData 4 (0.8/min)" in line and "cancelMktData 0 (0.0/min)" in line
    assert "other reqIds 7" in line
    assert "ensure_options req=4" in line and "open_orders calls=1" in line
    assert "qualify calls=4" in line
    assert "spx 0" in line
    for con_id in [ticker.contract.conId for ticker in broker._opt_tickers.values()] + [416904]:
        assert str(con_id) not in line
    assert "SPXNOON" not in line and "DU" not in line
    # Five minutes of broker time is one line, and disconnect flushes the rest.
    with caplog.at_level("INFO", logger="spx_noon.ib"):
        market.tick(301)
        broker.ensure_options(EXPIRY, [7000.0, 7010.0])      # the next count flushes
        assert sum(broker._reqs.req.values()) == 0           # the window restarted
        broker.disconnect()
    summaries = [rec.message for rec in caplog.records if "market data over" in rec.message]
    assert len(summaries) == 2
    assert "reqMktData 4" in summaries[0] and "reqMktData 0 (0.0/min)" in summaries[1]
    assert ib.disconnects == 1


def test_a_quiet_stretch_still_prints_its_own_near_zero_line(tmp_path, caplog):
    """The settled book counts nothing, and that is exactly what has to be logged."""
    broker, ib, market = _ib(tmp_path, lines=4)
    broker.ensure_options(EXPIRY, [7000.0, 7005.0])
    broker._reqs.flush(market.now())
    with caplog.at_level("INFO", logger="spx_noon.ib"):
        for _ in range(121):                       # ten minutes of five-second loops
            broker.ensure_options(EXPIRY, [7000.0, 7005.0])
            market.tick(5)
    assert (ib.req, ib.cancel) == (4, 0)           # the opening four, and nothing since
    windows = _windows(caplog.records)
    assert [(window, reqs, rate) for window, reqs, rate in windows] == [(5.0, 0, 0.0)] * 2


def test_the_meter_rate_is_the_window_not_the_process():
    meter = _ReqMeter(T0, every_s=300.0)
    meter.count("req", "ensure_options", 120)
    meter.count("cancel", "ensure_options", 2)
    assert meter.due(T0 + timedelta(seconds=299)) is False
    assert meter.due(T0 + timedelta(seconds=300)) is True
    line = meter.summary(T0 + timedelta(minutes=2))
    assert "reqMktData 120 (60.0/min)" in line and "cancelMktData 2 (1.0/min)" in line
    meter.flush(T0 + timedelta(minutes=2))
    assert meter.req == {} and meter.cancel == {} and meter.started == T0 + timedelta(minutes=2)
    # A flush with no window still prints, and divides by no zero.
    assert "over 0.0 min" in meter.summary(T0 + timedelta(minutes=2))


# --------------------------------------------------------------------------
# The runner: one call on the position path, pins while anything is live
# --------------------------------------------------------------------------

class _Pinning(SimBroker):
    """Records every ``pin_options`` and ``ensure_options``, in order."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list = []

    def pin_options(self, expiry, legs):
        self.calls.append(("pin", expiry, None if legs is None else [tuple(leg) for leg in legs]))

    def ensure_options(self, expiry, strikes):
        self.calls.append(("ensure", expiry, [float(strike) for strike in strikes]))


def _pos():
    entry = datetime(2026, 10, 28, 12, 0, tzinfo=ET)
    return {
        "date": "2026-10-28", "right": "C", "long_strike": 6950.0, "short_strike": 6980.0,
        "spx": 7000.0, "entry_close": entry,
    }


def test_the_position_path_pins_the_legs_and_calls_ensure_options_once(tmp_path):
    cfg = Config(broker="sim", mode="paper", base_dir=str(tmp_path))
    broker = _Pinning(datetime(2026, 10, 28, 13, 0, tzinfo=ET), [], {}, spot=7000)
    _ensure_position_quotes(cfg, broker, _pos())
    assert broker.calls == [
        ("pin", "20261028", [(6950.0, "C"), (6980.0, "C")]),
        ("ensure", "20261028", [6950.0, 6980.0]),
    ]


def test_a_session_pins_the_working_entry_and_clears_the_pins_when_flat(tmp_path):
    cfg = Config(broker="sim", mode="paper", base_dir=str(tmp_path),
                 loop_interval_s=60, decision_grace_s=90)
    bars = levels()[0] + today_call_bars()
    broker = _Pinning(datetime(2026, 10, 28, 11, 50, tzinfo=ET), bars, _quotes("target"), spot=7000)
    assert run_session(cfg, broker) == 0
    pins = [call for call in broker.calls if call[0] == "pin"]
    assert pins, "the entry and the open spread have to be pinned"
    assert pins[0] == ("pin", "20261028", [(6950.0, "C"), (6980.0, "C")])
    assert pins[-1] == ("pin", "", None)           # flat, and nothing working
    # The decision band is still subscribed, and the position path asks once.
    ensured = [call for call in broker.calls if call[0] == "ensure"]
    assert any(len(call[2]) > 2 for call in ensured)
    assert any(call[2] == [6950.0, 6980.0] for call in ensured)
    with (tmp_path / "state" / "trades.csv").open() as fh:
        assert list(csv.DictReader(fh))[-1]["exit_reason"] == "target"


def test_a_restart_pins_a_working_entry_and_subscribes_the_legs_on_the_fill(tmp_path):
    """``_poll_working`` does not subscribe, as before. The pin is what carries over."""
    cfg = Config(broker="sim", mode="paper", base_dir=str(tmp_path))
    cfg.ensure_dirs()
    saved = {
        "date": "2026-10-28", "status": "working", "plan": "B", "direction": "CALL", "right": "C",
        "spx": 7000, "pdh": 7010, "pdl": 6890, "em": 30, "width": 30,
        "long_strike": 6950, "short_strike": 6980, "long_delta": 0.74, "short_delta": 0.40,
        "mid": 12.0, "limit": 12.0, "fill_price": None,
        "entry_close": datetime(2026, 10, 28, 12, 0, tzinfo=ET).isoformat(),
        "deadline": datetime(2026, 10, 28, 14, 0, tzinfo=ET).isoformat(),
        "legs": [[69501, 1], [69801, -1]], "account": "DU999001",
        "ref": "SPXNOON-20261028-1", "entry_order_id": 101, "mode": "paper",
    }

    class _RestartEntry(_Pinning):
        """A restored entry that is still working, and then fills."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.filled = False

        def order_state(self, handle):
            if not self.filled:
                return {"status": "Submitted", "filled": 0, "remaining": 1,
                        "avg_price": 0.0, "done": False}
            return {"status": "Filled", "filled": 1, "remaining": 0,
                    "avg_price": 12.0, "done": True}

    bars = levels()[0] + today_call_bars()
    broker = _RestartEntry(datetime(2026, 10, 28, 13, 0, tzinfo=ET), bars, _quotes("dip"), spot=7000)
    state = _State(plan="B")
    _load_saved(cfg, broker, state, saved)
    assert state.working is not None
    assert broker.calls == [("pin", "20261028", [(6950.0, "C"), (6980.0, "C")])]
    broker.filled = True
    _poll_working(cfg, broker, Notifier(cfg.home / "logs"), Outputs(cfg.state_dir), state)
    assert state.position is not None
    assert broker.calls[-2:] == [
        ("pin", "20261028", [(6950.0, "C"), (6980.0, "C")]),
        ("ensure", "20261028", [6950.0, 6980.0]),
    ]


def test_an_entry_that_never_fills_clears_the_pins(tmp_path):
    cfg = Config(broker="sim", mode="paper", base_dir=str(tmp_path),
                 loop_interval_s=60, decision_grace_s=90)
    bars = levels()[0] + today_call_bars()

    class _NoFill(_Pinning):
        def order_state(self, handle):
            return {"status": "Cancelled", "filled": 0, "remaining": 1,
                    "avg_price": 0.0, "done": True}

    broker = _NoFill(datetime(2026, 10, 28, 11, 50, tzinfo=ET), bars, _quotes("target"), spot=7000)
    assert run_session(cfg, broker) == 0
    pins = [call for call in broker.calls if call[0] == "pin"]
    assert pins[0][2] == [(6950.0, "C"), (6980.0, "C")]
    assert pins[-1] == ("pin", "", None)

