"""One simulated day: skip, single fill, target, time flat, and a restart."""
import csv
from datetime import date, datetime
from zoneinfo import ZoneInfo

from spx_noon.broker import SimBroker
from spx_noon.config import Config
from spx_noon.pricing import trade_pnl
from spx_noon.runner import run_session
from spx_noon.strategy import Bar, OptQuote

from test_rules import DAY, levels, today_call_bars

ET = ZoneInfo("America/New_York")


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
    assert sells and all(order["tif"] == "GTC" or order["action"] == "SELL" for order in sells)
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
    broker = SimBroker(start, bars, _quotes("dip"), spot=7000)
    assert run_session(cfg, broker) == 0
    assert [order for order in broker.placed if order["action"] == "BUY"] == []
    with (tmp_path / "state" / "trades.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows[-1]["exit_reason"] == "time"
    assert rows[-1]["exit_time"].startswith("2026-10-28T14:00")


def round_money(value: float) -> float:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return float(text)


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
