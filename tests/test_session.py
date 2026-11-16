"""One simulated day: skip, single fill, target, time flat, a restart, and plan A1."""
import csv
import json
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from spx_noon.broker import SimBroker
from spx_noon.config import Config
from spx_noon.pricing import trade_pnl
from spx_noon.logbook import Outputs
from spx_noon.notify import Notifier
from spx_noon.runner import (
    _State,
    _flatten,
    _manage_position,
    _persist,
    _poll_working,
    _rest_target,
    run_session,
)
from spx_noon.strategy import Bar, OptQuote
from spx_noon.wrap_run import main as wrap_main

from test_rules import DAY, a1_call_bars, levels, today_call_bars

ET = ZoneInfo("America/New_York")
LIVE_ACCOUNT = "U7654321"


def _cfg(tmp, **extra):
    data = dict(broker="sim", mode="paper", base_dir=str(tmp), loop_interval_s=60, decision_grace_s=90)
    data.update(extra)
    return Config(**data)


def _book(long_bid=40.0, long_ask=40.2, short_bid=28.0, short_ask=28.2):
    return {
        (7000, "C"): OptQuote(14.5, 15.5, delta=0.50, con_id=70001),
        (7000, "P"): OptQuote(14.5, 15.5, delta=-0.50, con_id=70002),
        (6800, "C"): OptQuote(80, 80.2, delta=0.95, con_id=68001),
        (6950, "C"): OptQuote(long_bid, long_ask, delta=0.74, con_id=69501),
        (6980, "C"): OptQuote(short_bid, short_ask, delta=0.40, con_id=69801),
    }


def _quotes(kind: str):
    thirteen = datetime(2026, 10, 28, 13, 0, tzinfo=ET)
    twelve_fifteen = datetime(2026, 10, 28, 12, 15, tzinfo=ET)
    twelve_thirty = datetime(2026, 10, 28, 12, 30, tzinfo=ET)

    def book(now):
        if kind == "wide":
            return _book(long_ask=44)
        if kind == "later" and now < twelve_fifteen:
            return _book(long_ask=44)
        if kind == "dip" and now >= twelve_thirty:
            return _book(long_bid=32.0, long_ask=32.4)
        if kind == "target" and now >= thirteen:
            return _book(long_bid=60.0, long_ask=60.4)
        return _book()

    return book


def _run(tmp, kind, start_hour=11, start_min=50, **cfg_extra):
    bars = levels()[0] + today_call_bars()
    start = datetime(2026, 10, 28, start_hour, start_min, tzinfo=ET)
    broker = SimBroker(start, bars, _quotes(kind), spot=7000)
    code = run_session(_cfg(tmp, **cfg_extra), broker)
    path = tmp / "state" / "trades.csv"
    rows = []
    if path.exists():
        with path.open() as fh:
            rows = list(csv.DictReader(fh))
    buys = [order for order in broker.placed if order["action"] == "BUY"]
    sells = [order for order in broker.placed if order["action"] == "SELL"]
    return code, rows, buys, sells, broker


def test_wide_market_is_skipped_and_nothing_is_sent(tmp_path):
    code, rows, buys, sells, _broker = _run(tmp_path, "wide")
    assert code == 0
    assert buys == [] and sells == []
    assert rows and rows[0]["exit_reason"] == "skip_slippage"
    assert float(rows[0]["slippage_per_side"]) > 0.20


def test_a_later_bar_can_still_trade_after_a_slippage_skip(tmp_path):
    code, rows, buys, _sells, _broker = _run(tmp_path, "later")
    assert code == 0
    assert len(buys) == 1
    assert any(row["exit_reason"] == "skip_slippage" for row in rows)
    assert any(row["exit_reason"] in ("target", "time") for row in rows)


def test_target_exit_and_only_one_entry(tmp_path):
    code, rows, buys, sells, _broker = _run(tmp_path, "target")
    assert code == 0
    assert len(buys) == 1
    done = [row for row in rows if row["exit_reason"] == "target"]
    assert len(done) == 1
    fill = float(done[0]["fill_price"])
    exit_fill = float(done[0]["exit_fill"])
    assert exit_fill + 1e-9 >= fill * 1.30 - 0.05
    pnl, fees = trade_pnl(fill, exit_fill)
    assert float(done[0]["pnl"]) == round_money(pnl)
    assert float(done[0]["fees"]) == fees
    assert done[0]["direction"] == "CALL"
    assert float(done[0]["width"]) == 30
    assert float(done[0]["long_strike"]) == 6950
    assert any(order["tif"] == "GTC" for order in sells)


def test_no_stop_and_flat_two_hours_after_the_noon_bar(tmp_path):
    code, rows, buys, _sells, _broker = _run(tmp_path, "dip")
    assert code == 0
    assert len(buys) == 1
    done = [row for row in rows if row["exit_reason"] == "time"]
    assert len(done) == 1
    assert done[0]["exit_time"].startswith("2026-10-28T14:00")
    assert float(done[0]["exit_fill"]) < float(done[0]["fill_price"]) * 1.30
    pnl, _fees = trade_pnl(float(done[0]["fill_price"]), float(done[0]["exit_fill"]))
    assert float(done[0]["pnl"]) == round_money(pnl)


def test_dry_mode_logs_the_signal_and_sends_nothing(tmp_path):
    code, rows, buys, sells, _broker = _run(tmp_path, "target", mode="dry")
    assert code == 0
    assert buys == [] and sells == []
    assert rows[0]["exit_reason"] == "dry_signal"
    assert rows[0]["direction"] == "CALL"


class _NoOrders(SimBroker):
    def place_combo(self, *args, **kwargs):
        raise AssertionError("dry and alerts must never call the order adapter")


@pytest.mark.parametrize("mode", ["dry", "alerts"])
def test_dry_and_alerts_never_reach_the_order_adapter(tmp_path, mode):
    code, rows, buys, sells, broker = _run(tmp_path / mode, "target", mode=mode)
    assert code == 0
    assert broker.placed == [] and buys == [] and sells == []
    assert rows[0]["exit_reason"] == "dry_signal"
    # and the adapter itself is never reached, even with a qualifying bar
    start = datetime(2026, 10, 28, 11, 50, tzinfo=ET)
    guarded = _NoOrders(start, levels()[0] + today_call_bars(), _quotes("target"), spot=7000)
    assert run_session(_cfg(tmp_path / (mode + "2"), mode=mode), guarded) == 0


def _live_cfg(tmp, **extra):
    data = dict(broker="ibkr", mode="live", port=7496, account=LIVE_ACCOUNT,
                base_dir=str(tmp), loop_interval_s=60, decision_grace_s=90)
    data.update(extra)
    return Config(**data)


def test_ibkr_live_keeps_its_own_book_position_file_and_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    cfg = _live_cfg(tmp_path)
    cfg.validate()
    assert cfg.ibkr_live is True and cfg.live is False and cfg.sends_orders() is True
    start = datetime(2026, 10, 28, 11, 50, tzinfo=ET)
    broker = SimBroker(start, levels()[0] + today_call_bars(), _quotes("target"), spot=7000,
                       account=LIVE_ACCOUNT)
    assert run_session(cfg, broker) == 0
    state = tmp_path / "state"
    assert (state / "trades-live.csv").exists()
    assert not (state / "trades.csv").exists()
    assert (state / "own_position-live.json").exists()
    assert not (state / "own_position.json").exists()
    assert (state / "report-live-2026-10-28.md").exists()
    assert not (state / "report_2026-10-28.md").exists()
    with (state / "trades-live.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert [row["exit_reason"] for row in rows if row["exit_reason"] == "target"]
    assert all(row["mode"] == "live" for row in rows)
    printed = capsys.readouterr().out
    assert "BROKER=ibkr MODE=live ACCOUNT=4321 PORT=7496 REAL MONEY=YES" in printed
    assert LIVE_ACCOUNT not in printed


def test_a_paper_position_file_cannot_be_flattened_on_the_live_account(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    cfg = _live_cfg(tmp_path)
    cfg.ensure_dirs()
    from spx_noon.resume import save
    save(cfg.state_dir, {"date": DAY.isoformat(), "status": "open", "had_fill": True,
                         "fill_price": 12.0, "account": "DU999001"})
    assert (cfg.state_dir / "own_position.json").exists()
    start = datetime(2026, 10, 28, 11, 50, tzinfo=ET)
    broker = SimBroker(start, levels()[0] + today_call_bars(), _quotes("target"), spot=7000,
                       account=LIVE_ACCOUNT)
    assert run_session(cfg, broker) == 0
    sells = [order for order in broker.placed if order["action"] == "SELL"]
    assert broker.placed and sells
    assert all(order["account"] == LIVE_ACCOUNT for order in broker.placed)
    assert all(order["account"] != "DU999001" for order in sells)
    # the paper file is untouched; live has its own
    assert json.loads((cfg.state_dir / "own_position.json").read_text())["account"] == "DU999001"
    assert (cfg.state_dir / "own_position-live.json").exists()


def test_a_live_session_banner_prints_on_a_day_that_will_not_trade(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    start = datetime(2026, 11, 26, 11, 50, tzinfo=ET)  # Thanksgiving
    bars = [Bar(datetime(2026, 11, 25, 10, 0, tzinfo=ET), 1, 2, 1, 1)]
    broker = SimBroker(start, bars, _quotes("target"), spot=7000, account=LIVE_ACCOUNT)
    assert run_session(_live_cfg(tmp_path, port=4001), broker) == 0
    assert broker.placed == []
    printed = capsys.readouterr().out
    assert "BROKER=ibkr MODE=live ACCOUNT=4321 PORT=4001 REAL MONEY=YES" in printed


def test_a_live_session_stops_on_an_account_mismatch(tmp_path, monkeypatch):
    from spx_noon.config import SafetyError
    monkeypatch.setattr(sys, "platform", "darwin")
    start = datetime(2026, 10, 28, 11, 50, tzinfo=ET)
    for reported in ("U1111111", "DU999001"):
        broker = SimBroker(start, levels()[0] + today_call_bars(), _quotes("target"), spot=7000,
                           account=reported)
        with pytest.raises(SafetyError):
            run_session(_live_cfg(tmp_path / reported), broker)
        assert broker.placed == []
        assert broker.connected is False  # the socket is closed again


def test_the_live_position_file_blocks_a_second_live_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    cfg = _live_cfg(tmp_path)
    cfg.ensure_dirs()
    from spx_noon.resume import save
    save(cfg.state_dir, {"date": DAY.isoformat(), "status": "closed", "had_fill": True,
                         "fill_price": 12.0}, live=True)
    assert (cfg.state_dir / "own_position-live.json").exists()
    assert not (cfg.state_dir / "own_position.json").exists()
    start = datetime(2026, 10, 28, 11, 50, tzinfo=ET)
    broker = SimBroker(start, levels()[0] + today_call_bars(), _quotes("target"), spot=7000,
                       account=LIVE_ACCOUNT)
    assert run_session(cfg, broker) == 0
    assert broker.placed == []


def test_a_live_restart_manages_the_open_live_spread(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    cfg = _live_cfg(tmp_path)
    cfg.ensure_dirs()
    entry = datetime(2026, 10, 28, 12, 0, tzinfo=ET)
    payload = {
        "date": DAY.isoformat(), "status": "open", "direction": "CALL", "right": "C",
        "spx": 7000, "pdh": 7010, "pdl": 6890, "em": 30, "width": 30,
        "long_strike": 6950, "short_strike": 6980, "long_delta": 0.74, "short_delta": 0.40,
        "mid": 12.0, "limit": 12.0, "fill_price": 12.0,
        "entry_close": entry.isoformat(),
        "deadline": datetime(2026, 10, 28, 14, 0, tzinfo=ET).isoformat(),
        "legs": [[69501, 1], [69801, -1]],
        "account": LIVE_ACCOUNT, "ref": "SPXNOON-20261028-1",
        "tp_order_id": None, "mode": "live", "note": "restart",
    }
    from spx_noon.resume import save
    save(cfg.state_dir, payload, live=True)
    start = datetime(2026, 10, 28, 13, 30, tzinfo=ET)
    broker = SimBroker(start, levels()[0] + today_call_bars(), _quotes("dip"), spot=7000,
                       account=LIVE_ACCOUNT)
    assert run_session(cfg, broker) == 0
    assert [order for order in broker.placed if order["action"] == "BUY"] == []
    assert broker.placed and all(order["account"] == LIVE_ACCOUNT for order in broker.placed)
    with (tmp_path / "state" / "trades-live.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows[-1]["exit_reason"] == "time"
    assert not (tmp_path / "state" / "trades.csv").exists()
    assert not (tmp_path / "state" / "own_position.json").exists()


def _wrap_config(tmp_path, **keys):
    data = {"port": 7497, "mode": "dry", "base_dir": str(tmp_path)}
    data.update(keys)
    path = tmp_path / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_the_paper_and_live_wraps_never_see_each_other_s_rows(tmp_path):
    path = _wrap_config(tmp_path)
    today = datetime.now(ET).date()
    Outputs(tmp_path / "state", book="paper").append_trade(
        {"date": today, "direction": "CALL", "exit_reason": "target", "mode": "paper"})
    Outputs(tmp_path / "state", book="live").append_trade(
        {"date": today, "direction": "PUT", "exit_reason": "time", "mode": "live"})
    assert wrap_main(["--config", str(path), "--broker", "ibkr", "--now"]) == 0
    paper_text = (tmp_path / "reports" / f"wrap-{today.isoformat()}.md").read_text()
    assert "CALL" in paper_text and "PUT" not in paper_text
    assert "trades.csv" in paper_text and "trades-live.csv" not in paper_text
    # --book live works while the config is still dry on the paper port
    assert wrap_main(["--config", str(path), "--broker", "ibkr", "--now", "--book", "live"]) == 0
    live_text = (tmp_path / "reports" / f"wrap-live-{today.isoformat()}.md").read_text()
    assert "PUT" in live_text and "CALL" not in live_text
    assert "trades-live.csv" in live_text


def test_wrap_with_no_book_follows_a_live_config(tmp_path):
    path = _wrap_config(tmp_path, mode="live", port=7496, account=LIVE_ACCOUNT)
    today = datetime.now(ET).date()
    Outputs(tmp_path / "state", book="live").append_trade(
        {"date": today, "direction": "PUT", "exit_reason": "time", "mode": "live"})
    assert wrap_main(["--config", str(path), "--broker", "ibkr", "--now"]) == 0
    assert (tmp_path / "reports" / f"wrap-live-{today.isoformat()}.md").exists()
    assert not (tmp_path / "reports" / f"wrap-{today.isoformat()}.md").exists()


def test_restart_manages_the_open_spread_and_does_not_enter_again(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.ensure_dirs()
    deadline = datetime(2026, 10, 28, 14, 0, tzinfo=ET)
    entry = datetime(2026, 10, 28, 12, 0, tzinfo=ET)
    payload = {
        "date": DAY.isoformat(),
        "status": "open",
        "direction": "CALL",
        "right": "C",
        "spx": 7000,
        "pdh": 7010,
        "pdl": 6890,
        "em": 30,
        "width": 30,
        "long_strike": 6950,
        "short_strike": 6980,
        "long_delta": 0.74,
        "short_delta": 0.40,
        "mid": 12.0,
        "limit": 12.0,
        "fill_price": 12.0,
        "entry_close": entry.isoformat(),
        "deadline": deadline.isoformat(),
        "legs": [[69501, 1], [69801, -1]],
        "account": "DU999001",
        "ref": "SPXNOON-20261028-1",
        "tp_order_id": None,
        "mode": "paper",
        "note": "restart",
    }
    from spx_noon.resume import save
    save(cfg.state_dir, payload)
    bars = levels()[0] + today_call_bars()
    start = datetime(2026, 10, 28, 13, 30, tzinfo=ET)
    broker = _Watch(start, bars, _quotes("dip"), spot=7000)
    assert run_session(cfg, broker) == 0
    assert [order for order in broker.placed if order["action"] == "BUY"] == []
    assert any(6950.0 in strikes and 6980.0 in strikes for _expiry, strikes in broker.ensured)
    with (tmp_path / "state" / "trades.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows[-1]["exit_reason"] == "time"
    assert rows[-1]["exit_time"].startswith("2026-10-28T14:00")


def round_money(value: float) -> float:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return float(text)


class _Watch(SimBroker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ensured = []
        self.streams = 0

    def ensure_options(self, expiry, strikes):
        self.ensured.append((expiry, [float(strike) for strike in strikes]))

    def start_streams(self):
        self.streams += 1


class _Warm(SimBroker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ready_at = None

    def option_quotes(self):
        if self.ready_at is None:
            self.ready_at = self.now() + timedelta(seconds=2)
        if self.now() < self.ready_at:
            return {}
        return super().option_quotes()


class _Sticky(SimBroker):
    def order_state(self, handle):
        return {"status": "Submitted", "filled": 0, "remaining": 1, "avg_price": 0.0, "done": False}

    def cancel(self, handle):
        self.cancelled.append(handle)


def test_empty_quotes_retry_inside_the_grace_window(tmp_path):
    bars = levels()[0] + today_call_bars()
    start = datetime(2026, 10, 28, 11, 50, tzinfo=ET)
    broker = _Warm(start, bars, _quotes("target"), spot=7000)
    assert run_session(_cfg(tmp_path), broker) == 0
    buys = [order for order in broker.placed if order["action"] == "BUY"]
    assert len(buys) == 1


def test_closed_fill_blocks_a_second_entry_after_restart(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.ensure_dirs()
    from spx_noon.resume import save
    save(cfg.state_dir, {"date": DAY.isoformat(), "status": "closed", "had_fill": True, "fill_price": 12.0})
    start = datetime(2026, 10, 28, 11, 50, tzinfo=ET)
    broker = SimBroker(start, levels()[0] + today_call_bars(), _quotes("target"), spot=7000)
    assert run_session(cfg, broker) == 0
    assert broker.placed == []


def test_unconfirmed_cancel_keeps_the_working_entry(tmp_path):
    cfg = _cfg(tmp_path, exit_wait_s=2)
    broker = _Sticky(datetime(2026, 10, 28, 13, 0, tzinfo=ET), [], _book(), spot=7000)
    state = _State()
    state.working = {
        "id": 7,
        "cancel_after": datetime(2026, 10, 28, 12, 1, tzinfo=ET),
        "ref": "SPXNOON-20261028-1",
    }
    _poll_working(cfg, broker, None, None, state)
    assert state.working["id"] == 7
    assert broker.cancelled == [7]
    assert state.working["cancel_after"] > broker.now() - timedelta(seconds=1)


def test_known_target_id_is_kept_when_the_save_fails(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.ensure_dirs()
    broker = SimBroker(datetime(2026, 10, 28, 12, 5, tzinfo=ET), [], _book(), spot=7000)
    pos = {
        "legs": [(69501, 1), (69801, -1)],
        "account": "DU999001",
        "ref": "SPXNOON-20261028-1",
        "tp_limit": 15.6,
        "tp_order_id": None,
        "deadline": datetime(2026, 10, 28, 14, 0, tzinfo=ET),
    }

    def boom(_cfg, _pos):
        raise OSError("disk full")

    monkeypatch.setattr("spx_noon.runner._persist", boom)
    _rest_target(cfg, broker, pos)
    assert pos["tp_order_id"]
    assert any(order["tif"] == "GTC" for order in broker.placed)


class _NoSell(SimBroker):
    def _try_fill(self, handle):
        order = self.orders.get(handle)
        if order and order["action"] == "SELL":
            return
        super()._try_fill(handle)


def test_flatten_stops_after_three_failed_attempts(tmp_path):
    cfg = _cfg(tmp_path, exit_wait_s=1)
    cfg.ensure_dirs()
    broker = _NoSell(datetime(2026, 10, 28, 14, 0, tzinfo=ET), [], _book(), spot=7000)
    pos = {
        "legs": [(69501, 1), (69801, -1)],
        "account": "DU999001",
        "ref": "SPXNOON-20261028-1",
        "deadline": datetime(2026, 10, 28, 14, 0, tzinfo=ET),
        "fill_price": 12.0,
        "long_strike": 6950,
        "short_strike": 6980,
        "right": "C",
    }
    state = _State()
    state.position = pos
    notifier = Notifier(tmp_path / "logs", toast=False)
    out = Outputs(cfg.state_dir)
    for _ in range(3):
        assert _flatten(cfg, broker, notifier, out, state, "time", {"close_bid": 12.0}) is False
    placed = len(broker.placed)
    assert placed == 3
    assert _flatten(cfg, broker, notifier, out, state, "time", {"close_bid": 12.0}) is False
    assert len(broker.placed) == placed


def test_session_starts_the_quote_stream(tmp_path):
    start = datetime(2026, 10, 28, 11, 50, tzinfo=ET)
    broker = _Watch(start, levels()[0] + today_call_bars(), _quotes("target"), spot=7000)
    assert run_session(_cfg(tmp_path, mode="dry"), broker) == 0
    assert broker.streams == 1


# --------------------------------------------------------------------------
# Plan A1: the morning skip, the 80-delta fill, the +8 exit, and a restart
# --------------------------------------------------------------------------

A1_LEGS = [[69251, 1], [69551, -1]]
NOON = datetime(2026, 10, 28, 12, 0, tzinfo=ET)
ONE = datetime(2026, 10, 28, 13, 0, tzinfo=ET)
TWO = datetime(2026, 10, 28, 14, 0, tzinfo=ET)


def _a1_book(long_bid=52.0, long_ask=52.2):
    """The default book plus the 80-delta strike and its short leg 30 points up.

    ``_book()`` itself is untouched, so B's session tests fill against exactly
    the chain they always have.
    """
    book = _book()
    book[(6925, "C")] = OptQuote(long_bid, long_ask, delta=0.80, con_id=69251)
    book[(6955, "C")] = OptQuote(34.0, 34.2, delta=0.60, con_id=69551)
    return book


def _a1_quotes(kind="flat"):
    """``flat`` never moves. ``rich`` puts the spread bid through debit × 1.30 at 1:00."""

    def book(now):
        if kind == "rich" and now >= ONE:
            return _a1_book(long_bid=60.0, long_ask=60.4)
        return _a1_book()

    return book


class _Drift(SimBroker):
    """SimBroker whose SPX print moves once, at a wall-clock time.

    ``SimBroker.spot`` stays a float; this only decides which float it is.
    """

    def __init__(self, *args, moves_at=None, moves_to=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.moves_at = moves_at
        self.moves_to = moves_to

    def spx_px(self):
        if self.moves_at is not None and self.now() >= self.moves_at:
            self.spot = float(self.moves_to)
        return super().spx_px()


def _rows(tmp):
    path = tmp / "state" / "trades.csv"
    if not path.exists():
        return []
    with path.open() as fh:
        return list(csv.DictReader(fh))


def _a1_run(tmp, bars=None, *, kind="flat", moves_at=None, moves_to=None, **cfg_extra):
    bars = levels()[0] + (a1_call_bars() if bars is None else bars)
    broker = _Drift(datetime(2026, 10, 28, 11, 50, tzinfo=ET), bars, _a1_quotes(kind),
                    spot=7000, moves_at=moves_at, moves_to=moves_to)
    code = run_session(_cfg(tmp, plan="A1", **cfg_extra), broker)
    buys = [order for order in broker.placed if order["action"] == "BUY"]
    sells = [order for order in broker.placed if order["action"] == "SELL"]
    return code, _rows(tmp), buys, sells, broker


def test_a1_skips_the_whole_day_when_the_morning_already_broke(tmp_path):
    # today_call_bars() has a 10:15 close (bar start 10:00) above PDH
    code, rows, buys, sells, broker = _a1_run(tmp_path / "a1", bars=today_call_bars())
    assert code == 0
    assert broker.placed == [] and buys == [] and sells == []
    assert len(rows) == 1
    assert rows[0]["exit_reason"] == "skip_morning"
    assert rows[0]["timestamp"].startswith("2026-10-28T12:00")
    assert "10:15" in rows[0]["note"]
    assert rows[0]["long_strike"] == "" and rows[0]["fill_price"] == ""
    assert not (tmp_path / "a1" / "state" / "own_position.json").exists()
    report = (tmp_path / "a1" / "state" / "report_2026-10-28.md").read_text()
    assert "Plan A1" in report and "+8 SPX points" in report
    # the same day on version B is a trade: the morning break is an A1 rule only
    b_code, _b_rows, b_buys, _b_sells, _b = _run(tmp_path / "b", "target")
    assert b_code == 0 and len(b_buys) == 1


def test_a1_buys_the_eighty_delta_and_exits_on_eight_points(tmp_path):
    code, rows, buys, sells, _broker = _a1_run(
        tmp_path, moves_at=ONE, moves_to=7008)       # exactly +8 from the 7000 print
    assert code == 0
    assert len(buys) == 1 and buys[0]["limit"] == 18.0
    done = [row for row in rows if row["exit_reason"] == "points"]
    assert len(done) == 1
    row = done[0]
    assert row["direction"] == "CALL"
    assert float(row["long_strike"]) == 6925 and float(row["short_strike"]) == 6955
    assert float(row["long_delta"]) == 0.8 and float(row["width"]) == 30
    assert float(row["spx"]) == 7000
    assert row["exit_time"].startswith("2026-10-28T13:00")
    # the exit came from the print, not the premium: the bid never moved
    assert float(row["exit_fill"]) < float(row["fill_price"]) * 1.30
    assert len(sells) == 1 and sells[0]["tif"] == "DAY"   # no premium GTC was ever parked


def test_a1_does_not_take_the_thirty_percent_premium(tmp_path):
    # the spread bid goes through debit x 1.30 at 1:00 and SPX never moves
    code, rows, buys, sells, _broker = _a1_run(tmp_path, kind="rich")
    assert code == 0 and len(buys) == 1
    done = [row for row in rows if row["exit_reason"] in ("target", "time", "points")]
    assert len(done) == 1
    row = done[0]
    assert row["exit_reason"] == "time"
    assert row["exit_time"].startswith("2026-10-28T14:00")
    assert float(row["exit_fill"]) >= float(row["fill_price"]) * 1.30
    assert len(sells) == 1 and sells[0]["tif"] == "DAY"


def test_a1_holds_a_move_against_the_trade_to_the_two_hour_flat(tmp_path):
    code, rows, buys, sells, _broker = _a1_run(
        tmp_path, moves_at=datetime(2026, 10, 28, 12, 30, tzinfo=ET), moves_to=6985)
    assert code == 0 and len(buys) == 1
    done = [row for row in rows if row["exit_reason"] == "time"]
    assert len(done) == 1
    assert done[0]["exit_time"].startswith("2026-10-28T14:00")   # no earlier stop
    assert len(sells) == 1 and sells[0]["tif"] == "DAY"


def test_an_a1_ticket_names_the_plan_and_no_premium_target(tmp_path):
    code, rows, _buys, _sells, _broker = _a1_run(tmp_path, mode="dry")
    assert code == 0 and rows[0]["exit_reason"] == "dry_signal"
    ticket = (tmp_path / "logs" / "alerts.log").read_text()
    assert "plan A1" in ticket
    assert "long 6925C" in ticket
    assert "exit +8 SPX points or flat at 14:00 ET" in ticket
    assert "target" not in ticket


def _a1_saved(**extra):
    """An open A1 spread on disk, as ``_persist`` writes it. No plan key unless asked."""
    payload = {
        "date": DAY.isoformat(), "status": "open", "direction": "CALL", "right": "C",
        "spx": 7000, "pdh": 7010, "pdl": 6890, "em": 30, "width": 30,
        "long_strike": 6925, "short_strike": 6955, "long_delta": 0.80, "short_delta": 0.60,
        "mid": 18.0, "limit": 18.0, "fill_price": 18.0,
        "entry_close": NOON.isoformat(), "deadline": TWO.isoformat(),
        "legs": A1_LEGS, "account": "DU999001", "ref": "SPXNOON-20261028-1",
        "tp_order_id": None, "mode": "paper", "note": "restart",
    }
    payload.update(extra)
    return payload


def _restart(tmp, saved, *, plan="B", moves_to=None):
    from spx_noon.resume import save
    cfg = _cfg(tmp, plan=plan)
    cfg.ensure_dirs()
    save(cfg.state_dir, saved)
    start = datetime(2026, 10, 28, 13, 30, tzinfo=ET)
    broker = _Drift(start, levels()[0] + a1_call_bars(), _a1_quotes(), spot=7000,
                    moves_at=None if moves_to is None else start, moves_to=moves_to)
    assert run_session(cfg, broker) == 0
    assert [order for order in broker.placed if order["action"] == "BUY"] == []
    return _rows(tmp), broker


def test_an_a1_restart_rests_no_premium_order_and_exits_on_points(tmp_path):
    rows, broker = _restart(tmp_path / "points", _a1_saved(plan="A1"), plan="A1", moves_to=7008)
    assert all(order["tif"] == "DAY" for order in broker.placed)
    assert rows[-1]["exit_reason"] == "points"
    assert rows[-1]["exit_time"].startswith("2026-10-28T13:30")
    # and one that never gets there keeps the saved deadline
    rows, broker = _restart(tmp_path / "time", _a1_saved(plan="A1"), plan="A1")
    assert all(order["tif"] == "DAY" for order in broker.placed)
    assert rows[-1]["exit_reason"] == "time"
    assert rows[-1]["exit_time"].startswith("2026-10-28T14:00")
    # the stored plan also names the report, even if the config has moved to B
    rows, broker = _restart(tmp_path / "switched", _a1_saved(plan="A1"), plan="B", moves_to=7008)
    assert all(order["tif"] == "DAY" for order in broker.placed)
    assert rows[-1]["exit_reason"] == "points"
    report = (tmp_path / "switched" / "state" / "report_2026-10-28.md").read_text()
    assert "Plan A1" in report and "Version B" not in report


class _NoSellDrift(_Drift):
    """A drifting print whose sells never fill, so a flatten has to be retried."""

    def _try_fill(self, handle):
        order = self.orders.get(handle)
        if order and order["action"] == "SELL":
            return
        super()._try_fill(handle)


def test_a_triggered_a1_point_exit_is_not_forgotten_when_the_print_comes_back(tmp_path):
    """A1 has no resting GTC behind it, so a hit has to keep being worked."""
    cfg = _cfg(tmp_path, plan="A1", exit_wait_s=1)
    cfg.ensure_dirs()
    broker = _NoSellDrift(ONE, [], _a1_quotes(), spot=7008)
    state = _State()
    pos = dict(_a1_saved(plan="A1"))
    pos["entry_close"] = NOON
    pos["deadline"] = TWO
    pos["legs"] = [tuple(leg) for leg in pos["legs"]]
    state.position = pos
    notifier = Notifier(tmp_path / "logs", toast=False)
    out = Outputs(cfg.state_dir)
    assert _manage_position(cfg, broker, notifier, out, state) is False
    assert pos["points_hit"] is True
    assert len(broker.placed) == 1                    # one sell tried, not filled
    broker.spot = 7001                                # the print retreats under +8
    broker.t = ONE + timedelta(minutes=1)
    assert _manage_position(cfg, broker, notifier, out, state) is False
    assert len(broker.placed) == 2                    # still being worked, not abandoned
    # and when a sell finally gets there the row is the point exit, not the clock
    broker.__class__ = _Drift
    broker.t = ONE + timedelta(minutes=2)
    assert _manage_position(cfg, broker, notifier, out, state) is True
    assert _rows(tmp_path)[-1]["exit_reason"] == "points"


def test_a_triggered_a1_point_exit_survives_a_restart(tmp_path):
    """The latch is saved when it trips, not only when a sell goes out."""
    cfg = _cfg(tmp_path, plan="A1")
    cfg.ensure_dirs()
    pos = dict(_a1_saved(plan="A1"))
    pos["entry_close"] = NOON
    pos["deadline"] = TWO
    pos["legs"] = [tuple(leg) for leg in pos["legs"]]
    _persist(cfg, pos)
    state = _State(position=pos, plan="A1")
    # +8 is reached while there is no quote to sell against: nothing can be sent
    broker = _Drift(ONE, [], {}, spot=7008)
    notifier = Notifier(tmp_path / "logs", toast=False)
    assert _manage_position(cfg, broker, notifier, Outputs(cfg.state_dir), state) is False
    assert broker.placed == []
    assert json.loads((cfg.state_dir / "own_position.json").read_text())["points_hit"] is True
    # a restart with the print back under +8 still takes the point exit
    broker = _Drift(ONE + timedelta(minutes=1), levels()[0] + a1_call_bars(),
                    _a1_quotes(), spot=7001)
    assert run_session(_cfg(tmp_path, plan="A1"), broker) == 0
    assert [order for order in broker.placed if order["action"] == "BUY"] == []
    assert _rows(tmp_path)[-1]["exit_reason"] == "points"


def test_a_closed_a1_trade_keeps_its_plan_in_a_later_report_that_day(tmp_path):
    rows, _broker = _restart(tmp_path, _a1_saved(plan="A1"), plan="A1", moves_to=7008)
    assert rows[-1]["exit_reason"] == "points"
    report = tmp_path / "state" / "report_2026-10-28.md"
    assert "Plan A1" in report.read_text()
    # the same day, restarted after the config moved back to B: the trade was A1
    broker = _Drift(datetime(2026, 10, 28, 14, 30, tzinfo=ET),
                    levels()[0] + a1_call_bars(), _a1_quotes(), spot=7000)
    assert run_session(_cfg(tmp_path, plan="B"), broker) == 0
    assert broker.placed == []
    assert "Plan A1" in report.read_text()


class _AlwaysFilled(_Drift):
    """Every order this broker is asked about is already filled at 18.00."""

    def order_state(self, handle):
        return {"status": "Filled", "filled": 1, "remaining": 0, "avg_price": 18.0, "done": True}


def test_a_working_a1_entry_that_filled_before_the_restart_rests_no_gtc(tmp_path):
    from spx_noon.resume import save
    cfg = _cfg(tmp_path, plan="A1")
    cfg.ensure_dirs()
    save(cfg.state_dir, _a1_saved(status="working", plan="A1", entry_order_id=101))
    start = datetime(2026, 10, 28, 13, 30, tzinfo=ET)
    broker = _AlwaysFilled(start, levels()[0] + a1_call_bars(), _a1_quotes(), spot=7000,
                           moves_at=start, moves_to=7008)
    assert run_session(cfg, broker) == 0
    assert [order for order in broker.placed if order["action"] == "BUY"] == []
    assert all(order["tif"] == "DAY" for order in broker.placed)
    rows = _rows(tmp_path)
    assert rows[-1]["exit_reason"] == "points"
    assert json.loads((cfg.state_dir / "own_position.json").read_text())["plan"] == "A1"


def test_a_restart_with_no_stored_plan_is_still_version_b(tmp_path):
    for plan in ("B", "A1"):
        # the position file is the authority, even when today's config says A1
        rows, broker = _restart(tmp_path / plan, _a1_saved(), plan=plan, moves_to=7008)
        # the premium target still rests, and +8 points is not an exit on version B
        assert any(order["tif"] == "GTC" for order in broker.placed), plan
        assert rows[-1]["exit_reason"] == "time"
        assert rows[-1]["exit_time"].startswith("2026-10-28T14:00")
        report = (tmp_path / plan / "state" / "report_2026-10-28.md").read_text()
        assert "Version B" in report and "Plan A1" not in report


def test_a_working_entry_with_no_stored_plan_fills_as_version_b(tmp_path):
    """A working file written before the plan key existed is a version B entry."""
    from spx_noon.resume import save
    cfg = _cfg(tmp_path, plan="A1")
    cfg.ensure_dirs()
    save(cfg.state_dir, _a1_saved(status="working", entry_order_id=101))   # no plan key
    start = datetime(2026, 10, 28, 13, 30, tzinfo=ET)
    broker = _AlwaysFilled(start, levels()[0] + a1_call_bars(), _a1_quotes(), spot=7000,
                           moves_at=start, moves_to=7008)
    assert run_session(cfg, broker) == 0
    saved = json.loads((cfg.state_dir / "own_position.json").read_text())
    assert saved["plan"] == "B" and saved["tp_limit"] == 23.4
    # the premium target went out, and +8 points did not close the spread
    assert any(order["tif"] == "GTC" for order in broker.placed)
    assert _rows(tmp_path)[-1]["exit_reason"] == "target"


def test_holiday_does_not_trade(tmp_path):
    thanksgiving = date(2026, 11, 26)
    start = datetime(2026, 11, 26, 11, 50, tzinfo=ET)
    bars = [Bar(datetime(2026, 11, 25, 10, 0, tzinfo=ET), 1, 2, 1, 1)]
    broker = SimBroker(start, bars, _quotes("target"), spot=7000)
    # Thursday session hours would still be closed by the holiday list.
    broker.t = start
    assert run_session(_cfg(tmp_path), broker) == 0
    assert broker.placed == []
    assert thanksgiving.isoformat() == "2026-11-26"
