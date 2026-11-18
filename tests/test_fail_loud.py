"""A stop before or during the session is logged, alerted, and nonzero.

The quote watchdog is the other half: a flat session with a dead SPX print
exits, and a session that still holds a spread does not.
"""
import csv
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from spx_noon.broker import SimBroker
from spx_noon.config import Config, SafetyError, load_config
from spx_noon.notify import Notifier
from spx_noon.runner import _Feed, _State, _data_watchdog, run_session
from spx_noon.strategy import Px
from test_rules import DAY, levels, today_call_bars
from test_session import _cfg, _quotes, _run

ET = ZoneInfo("America/New_York")
ROOT_REASON = "tastytrade returned more than one account; set account to the one to use"


def _day_log(tmp_path):
    day = datetime.now(ET).strftime("%Y-%m-%d")
    return (tmp_path / "state" / f"bot_{day}.log").read_text(encoding="utf-8")


def _alerts(tmp_path):
    return (tmp_path / "logs" / "alerts.log").read_text(encoding="utf-8")


def _tasty_file(tmp_path, **extra):
    data = {"broker": "tasty", "mode": "dry", "base_dir": str(tmp_path), "account": ""}
    data.update(extra)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


class _Frozen(SimBroker):
    """An SPX print whose timestamp never moves forward."""

    def spx_px(self):
        return Px(self.spot, self.t - timedelta(minutes=10))


class _Silent(SimBroker):
    """The broker answers, and the answer is not a quote."""

    def spx_px(self):
        return Px(None, None)


class _Probe(SimBroker):
    def __init__(self, *args, accounts=("11111111",), **kwargs):
        super().__init__(*args, **kwargs)
        self.accounts = list(accounts)
        self.events = []

    def managed_accounts(self):
        self.events.append("accounts")
        return list(self.accounts)

    def start_streams(self):
        self.events.append("streams")


def _bars():
    return levels()[0] + today_call_bars()


def test_safety_error_is_logged_and_alerted(tmp_path, monkeypatch, capsys):
    from spx_noon import runner

    def boom(cfg, broker):
        raise SafetyError(ROOT_REASON)

    monkeypatch.setattr(runner, "run_session", boom)
    assert runner.main(["--config", str(_tasty_file(tmp_path))]) == 2
    text = _day_log(tmp_path)
    assert "ERROR" in text
    assert "CRITICAL" not in text
    assert "more than one account" in text
    alerts = _alerts(tmp_path)
    assert "Noon stopped" in alerts
    assert "more than one account" in alerts
    assert "more than one account" in capsys.readouterr().out


def test_unexpected_exception_is_logged_alerted_and_redacted(tmp_path, monkeypatch, capsys):
    from spx_noon import runner

    token = "super-secret-token"
    bearer = "abcdefghijklmnop"

    def boom(cfg, broker):
        raise RuntimeError(f"refresh_token={token} Bearer {bearer}")

    monkeypatch.setattr(runner, "run_session", boom)
    assert runner.main(["--config", str(_tasty_file(tmp_path))]) == 1
    text = _day_log(tmp_path)
    alerts = _alerts(tmp_path)
    printed = capsys.readouterr().out
    assert "CRITICAL" in text
    assert "RuntimeError" in text
    assert "Noon crashed" in alerts
    for blob in (text, alerts, printed):
        assert token not in blob
        assert bearer not in blob
        assert "[redacted]" in blob


def test_a_config_refusal_is_logged_and_alerted(tmp_path, capsys):
    from spx_noon import runner

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mode": "dry", "base_dir": str(tmp_path)}))
    assert runner.main(["--config", str(path)]) == 2
    text = _day_log(tmp_path)
    assert "ERROR" in text
    assert "broker must be set in config.json" in text
    assert "Noon stopped" in _alerts(tmp_path)
    assert "refusing to start:" in capsys.readouterr().out


def test_blank_account_with_two_accounts_refuses_before_streams(tmp_path):
    cfg = Config(broker="tasty", mode="dry", account="", base_dir=str(tmp_path),
                 loop_interval_s=60, decision_grace_s=90)
    start = datetime(2026, 10, 28, 11, 50, tzinfo=ET)
    broker = _Probe(start, _bars(), _quotes("target"), spot=7000, accounts=["11111111", "22222222"])
    with pytest.raises(SafetyError, match="more than one account"):
        run_session(cfg, broker)
    assert broker.events == ["accounts"]
    assert broker.connected is False
    assert broker.placed == []


def test_the_single_account_is_resolved_before_streams(tmp_path):
    cfg = Config(broker="tasty", mode="dry", account="", base_dir=str(tmp_path),
                 loop_interval_s=60, decision_grace_s=90)
    start = datetime(2026, 10, 28, 11, 50, tzinfo=ET)
    broker = _Probe(start, _bars(), _quotes("target"), spot=7000, accounts=["11111111"])
    assert run_session(cfg, broker) == 0
    assert broker.events == ["accounts", "streams"]
    assert broker.placed == []
    assert "Noon data stale" not in _alerts(tmp_path)


def test_watchdog_fires_on_a_stale_spx_print(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    start = datetime(2026, 10, 28, 11, 50, tzinfo=ET)
    broker = _Frozen(start, _bars(), _quotes("target"), spot=7000)
    assert run_session(_cfg(tmp_path), broker) == 1
    assert broker.placed == []
    assert broker.t == start
    assert "No fresh SPX quote" in caplog.text
    assert "ERROR" in caplog.text
    alerts = _alerts(tmp_path)
    assert "Noon data stale" in alerts
    assert "Stopping before the noon decision" in alerts


def test_watchdog_fires_when_no_heartbeat_arrives_after_start(tmp_path):
    start = datetime(2026, 10, 28, 11, 50, tzinfo=ET)
    broker = _Silent(start, _bars(), _quotes("target"), spot=7000)
    assert run_session(_cfg(tmp_path), broker) == 1
    assert broker.placed == []
    assert broker.t < datetime(2026, 10, 28, 12, 0, tzinfo=ET)
    assert (broker.t - start).total_seconds() == 180
    assert "Stopping before the noon decision" in _alerts(tmp_path)


def test_watchdog_does_not_fire_on_a_healthy_loop(tmp_path):
    code, _rows, buys, _sells, _broker = _run(tmp_path, "target")
    assert code == 0
    assert len(buys) == 1
    alerts = _alerts(tmp_path)
    assert "Noon data stale" not in alerts
    assert "No fresh SPX" not in alerts


def test_watchdog_stays_up_and_flattens_an_open_spread(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.ensure_dirs()
    entry = datetime(2026, 10, 28, 12, 0, tzinfo=ET)
    from spx_noon.resume import save
    save(cfg.state_dir, {
        "date": DAY.isoformat(), "status": "open", "direction": "CALL", "right": "C",
        "spx": 7000, "pdh": 7010, "pdl": 6890, "em": 30, "width": 30,
        "long_strike": 6950, "short_strike": 6980, "long_delta": 0.74, "short_delta": 0.40,
        "mid": 12.0, "limit": 12.0, "fill_price": 12.0,
        "entry_close": entry.isoformat(),
        "deadline": datetime(2026, 10, 28, 14, 0, tzinfo=ET).isoformat(),
        "legs": [[69501, 1], [69801, -1]],
        "account": "DU999001", "ref": "SPXNOON-20261028-1",
        "tp_order_id": None, "mode": "paper", "note": "restart",
    })
    start = datetime(2026, 10, 28, 13, 30, tzinfo=ET)
    broker = _Frozen(start, _bars(), _quotes("dip"), spot=7000)
    assert run_session(cfg, broker) == 0
    assert [order for order in broker.placed if order["action"] == "BUY"] == []
    assert [order for order in broker.placed if order["action"] == "SELL"]
    alerts = _alerts(tmp_path)
    assert alerts.count("Noon data stale") == 1
    assert "Staying up" in alerts
    assert "Stopping before the noon decision" not in alerts
    with (tmp_path / "state" / "trades.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[-1]["exit_reason"] == "time"


def test_watchdog_does_not_exit_or_send_while_an_entry_is_working(tmp_path):
    now = datetime(2026, 10, 28, 12, 5, tzinfo=ET)
    state = _State()
    state.working = {"id": 7}
    broker = _Frozen(now, [], {}, spot=7000)
    code = _data_watchdog(
        _cfg(tmp_path), broker, Notifier(tmp_path / "logs", toast=False),
        state, _Feed(started=now - timedelta(minutes=10)), [], timedelta(seconds=90))
    assert code is None
    assert broker.placed == []
    assert "Staying up" in _alerts(tmp_path)
    assert "Stopping before the noon decision" not in _alerts(tmp_path)


def test_watchdog_default_is_three_minutes_and_must_stay_positive():
    cfg = Config(broker="sim", mode="dry")
    assert cfg.data_watchdog_s == 180
    loaded = load_config(Path(__file__).resolve().parent.parent / "config.windows.example.json")
    assert loaded.data_watchdog_s == 180
    bad = Config(broker="sim", mode="dry", data_watchdog_s=0)
    with pytest.raises(ValueError, match="data_watchdog_s"):
        bad.validate()
