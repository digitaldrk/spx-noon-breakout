"""Paper port, machine broker, and the live approval gate."""
import sys
from pathlib import Path

import pytest

from spx_noon.config import ORDER_PREFIX, Config, SafetyError, load_config
from spx_noon.guard import GuardError, OrderGuard
from spx_noon.ib import paper_send_allowed
from spx_noon.schwab import SchwabBroker, build_vertical_order

ROOT = Path(__file__).resolve().parent.parent


def test_examples_are_dry_and_carry_no_secrets():
    for name in ("config.example.json", "config.windows.example.json"):
        text = (ROOT / name).read_text().lower()
        assert "refresh_token" not in text
        assert "app_secret" not in text
        assert "private_key" not in text


def test_mac_example_is_paper_port_and_ignores_a_broker_key(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    cfg = load_config(ROOT / "config.example.json")
    assert cfg.broker == "ibkr" and cfg.mode == "dry" and cfg.dry_run is True
    assert cfg.port == 7497 and cfg.client_id == 41 and not cfg.live and not cfg.paper
    flipped = tmp_path / "config.json"
    flipped.write_text('{"broker": "schwab", "mode": "dry", "port": 7497}')
    assert load_config(flipped).broker == "ibkr"


def test_windows_example_stays_schwab_and_does_not_send(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cfg = load_config(ROOT / "config.windows.example.json")
    assert cfg.broker == "schwab" and cfg.mode == "dry" and cfg.dry_run is True
    assert cfg.live is False
    assert cfg.sends_orders() is False


def test_mac_refuses_live_and_a_non_paper_port(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(SafetyError):
        Config(mode="live").validate()
    with pytest.raises(SafetyError):
        Config(port=7496).validate()
    with pytest.raises(SafetyError):
        Config(host="1.2.3.4").validate()
    paper = Config(mode="paper")
    assert paper.paper and paper.sends_orders()
    dry = Config(mode="alerts", dry_run=False)
    assert dry.dry_run is True and not dry.sends_orders()


def test_windows_live_is_off_unless_asked(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert Config().mode == "dry" and not Config().live
    assert Config(mode="live", account="705").live
    with pytest.raises(SafetyError):
        Config(mode="live", account="").validate()
    with pytest.raises(SafetyError):
        Config(mode="paper", account="705").validate()


def test_paper_send_gate():
    cfg = Config(broker="ibkr", mode="paper", account="DU123")
    paper_send_allowed(cfg, "DU123", "SPXNOON-20261028-1")
    with pytest.raises(SafetyError):
        paper_send_allowed(Config(broker="ibkr", mode="dry"), "DU123", "SPXNOON-20261028-1")
    with pytest.raises(SafetyError):
        paper_send_allowed(cfg, "U123456", "SPXNOON-20261028-1")
    with pytest.raises(SafetyError):
        paper_send_allowed(cfg, "DU123", "BRK-1")
    assert ORDER_PREFIX == "SPXNOON-"


class _FakeSchwab:
    def __init__(self):
        self.placed = []
        self.order_guard = None

    def place_order(self, order, ticket_id):
        self.order_guard.authorize("place", ticket_id)
        self.placed.append(order)
        return "9001"


def _legs():
    return [
        ("SPXW  261028C06950000", 1),
        ("SPXW  261028C06980000", -1),
    ]


def test_vertical_order_is_a_debit_and_the_exit_is_gtc():
    opened = build_vertical_order(_legs(), "BUY", 1, 12.1, tif="DAY")
    assert opened["complexOrderStrategyType"] == "VERTICAL"
    assert opened["orderType"] == "NET_DEBIT"
    assert opened["duration"] == "DAY"
    assert opened["price"] == "12.10"
    assert opened["orderLegCollection"][0]["instruction"] == "BUY_TO_OPEN"
    assert opened["orderLegCollection"][1]["instruction"] == "SELL_TO_OPEN"
    closed = build_vertical_order(_legs(), "SELL", 1, 15.7, tif="GTC")
    assert closed["orderType"] == "NET_CREDIT"
    assert closed["duration"] == "GOOD_TILL_CANCEL"
    assert closed["orderLegCollection"][0]["instruction"] == "SELL_TO_CLOSE"


def test_live_schwab_refuses_without_tod_approval(tmp_path):
    cfg = Config(broker="schwab", mode="live", account="705", base_dir=str(tmp_path))
    client = _FakeSchwab()
    broker = SchwabBroker(cfg, client=client)
    with pytest.raises((GuardError, SafetyError)):
        broker.place_combo(_legs(), "BUY", 1, 12.10, "705", "SPXNOON-20261028-1")
    assert client.placed == []
    broker.approve_ticket("N120000", 12.10)
    with pytest.raises(GuardError):
        broker.place_combo(_legs(), "BUY", 1, 12.30, "705", "SPXNOON-20261028-1")
    order_id = broker.place_combo(_legs(), "BUY", 1, 12.10, "705", "SPXNOON-20261028-1")
    assert order_id == "9001"
    assert client.placed[0]["orderType"] == "NET_DEBIT"
    dry = SchwabBroker(Config(broker="schwab", mode="dry", account="705", base_dir=str(tmp_path / "dry")),
                       client=_FakeSchwab())
    with pytest.raises(SafetyError):
        dry.place_combo(_legs(), "BUY", 1, 12.10, "705", "SPXNOON-20261028-1")


def test_guard_refuses_when_not_live():
    guard = OrderGuard(live=False)
    with pytest.raises(GuardError):
        guard.approve("N120000", 12.10)
    with pytest.raises(GuardError):
        guard.authorize("place", "N120000")


def test_package_does_not_import_candle_or_fly():
    blob = "\n".join(path.read_text() for path in (ROOT / "spx_noon").glob("*.py"))
    lowered = blob.lower()
    assert "spx_breakout" not in lowered
    assert "spx_fly" not in lowered
    strategy = (ROOT / "spx_noon" / "strategy.py").read_text().lower()
    assert "ema" not in strategy
