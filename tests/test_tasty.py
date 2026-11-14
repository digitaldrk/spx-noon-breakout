"""Tastytrade broker: config defaults, credential split, and order locks."""
import json
import sys
from datetime import datetime, timezone

import pytest

from spx_noon.config import _BROKER_ALIASES, Config, SafetyError, load_config, resolve_broker
from spx_noon.guard import GuardError
from spx_noon.tasty import (
    CERT_HOST,
    PROD_HOST,
    TastyBroker,
    _pad_occ,
    build_vertical_order,
    candles_to_bars,
    session_host,
)

from test_rules import levels, today_call_bars
from test_session import _quotes

ROOT_LEGS = [
    ("SPXW  261028C06950000", 1),
    ("SPXW  261028C06980000", -1),
]
REF = "SPXNOON-20261028-1"


class _URL:
    def __init__(self, host):
        self.host = host


class _Client:
    def __init__(self, host):
        self.base_url = _URL(host)


class Sess:
    def __init__(self, host, is_test, *, client_host=None, label=None):
        self.is_test = is_test
        self.host = label if label is not None else host
        if client_host is not None:
            self._client = _Client(client_host)


class _Order:
    def __init__(self, status, price=-11.8):
        self.status = status
        self.price = price

    def average_fill_price(self):
        if self.status != "Filled":
            raise RuntimeError("no fills")
        return self.price


class Transport:
    def __init__(self):
        self.placed = []
        self.replaced = []
        self.cancelled = []
        self.sessions = []
        self.accounts = ["5WT12345"]
        self.orders = {}
        self._n = 1000

    def place_order(self, session, payload):
        self.sessions.append(session)
        self.placed.append(payload)
        self._n += 1
        oid = str(self._n)
        self.orders[oid] = _Order("Live")
        return oid

    def replace_order(self, session, old, payload):
        self.sessions.append(session)
        self.replaced.append((old, payload))
        self._n += 1
        oid = str(self._n)
        self.orders[oid] = _Order("Live")
        return oid

    def cancel_order(self, session, oid):
        self.sessions.append(session)
        self.cancelled.append(oid)
        self.orders[str(oid)] = _Order("Cancelled")

    def get_order(self, session, oid):
        self.sessions.append(session)
        return self.orders[str(oid)]

    def preview_order(self, session, payload):
        self.sessions.append(session)
        return {"ok": True, "rejects": [], "warnings": []}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _pair(data="production", orders="sandbox"):
    data_s = Sess(PROD_HOST if data == "production" else CERT_HOST, is_test=data == "sandbox")
    order_s = Sess(CERT_HOST if orders == "sandbox" else PROD_HOST, is_test=orders == "sandbox")
    return data_s, order_s


def _broker(tmp_path, *, orders="sandbox", data="production", mode="live", cap=12.10, transport=None):
    cfg = Config(
        broker="tasty", mode=mode, tasty_data=data, tasty_orders=orders,
        base_dir=str(tmp_path), max_orders_per_min=10,
    )
    cfg.ensure_dirs()
    data_s, order_s = _pair(data, orders)
    transport = transport or Transport()
    broker = TastyBroker(
        cfg, data_session=data_s, order_session=order_s, transport=transport, clock=Clock(),
    )
    broker.connect()
    if mode == "live":
        broker.approve_ticket("N120000", cap)
    return broker, transport


def test_aliases_and_windows_auto(monkeypatch):
    assert resolve_broker("tasty") == resolve_broker("tt") == resolve_broker("tastytrade") == "tasty"
    monkeypatch.setattr(sys, "platform", "win32")
    assert resolve_broker("auto") == "tasty"
    assert resolve_broker(None) == "tasty"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert resolve_broker("auto") == "ibkr"
    # the alias table is the whole list of brokers. A retired one is not on it,
    # and anything that is not on it is refused rather than guessed at.
    assert sorted(_BROKER_ALIASES) == ["ibkr", "sim", "tasty", "tastytrade", "tt"]
    for name in ("nope", "tda", "etrade", "ib"):
        with pytest.raises(SafetyError, match="unknown broker"):
            resolve_broker(name)


def test_config_defaults_and_rejects_a_mismatch(tmp_path, monkeypatch):
    cfg = Config(broker="tasty", mode="dry", base_dir=str(tmp_path))
    assert cfg.tasty_data == "production"
    assert cfg.tasty_orders == "sandbox"
    assert cfg.real_money is False
    assert cfg.sends_orders() is False
    live = Config(broker="tasty", mode="live", base_dir=str(tmp_path))
    assert live.live and live.sends_orders()
    real = Config(broker="tasty", mode="live", tasty_data="production", tasty_orders="production",
                  base_dir=str(tmp_path))
    assert real.real_money is True
    with pytest.raises(SafetyError, match="tasty_data"):
        Config(broker="tasty", mode="dry", tasty_data="sandbox", tasty_orders="production").validate()
    with pytest.raises(SafetyError):
        Config(broker="tasty", mode="dry", max_orders_per_min=11).validate()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("TASTYTRADE_ORDERS", "production")
    monkeypatch.setenv("tasty_allow_production_orders", "1")
    path = tmp_path / "bare.json"
    path.write_text('{"mode": "dry", "broker": "ibkr"}')
    loaded = load_config(path)
    assert loaded.broker == "tasty"
    assert loaded.tasty_orders == "sandbox"
    assert loaded.tasty_data == "production"


def test_no_production_order_toggle_in_source():
    blob = "\n".join(
        path.read_text() for path in (__import__("pathlib").Path(__file__).resolve().parent.parent / "spx_noon").glob("*.py")
    )
    assert "tasty_allow_production_orders" not in blob
    assert "TASTYTRADE_ORDERS" not in blob


def test_vertical_price_and_candle_bars():
    opened = build_vertical_order(ROOT_LEGS, "BUY", 1, 12.1, tif="DAY")
    assert opened["price"] == -12.1
    assert opened["price-effect"] == "Debit"
    assert opened["time-in-force"] == "Day"
    assert opened["legs"][0]["action"] == "Buy to Open"
    assert opened["legs"][1]["action"] == "Sell to Open"
    closed = build_vertical_order(ROOT_LEGS, "SELL", 1, 15.7, tif="GTC")
    assert closed["price"] == 15.7
    assert closed["price-effect"] == "Credit"
    assert closed["time-in-force"] == "GTC"
    assert closed["legs"][0]["action"] == "Sell to Close"
    assert closed["legs"][1]["action"] == "Buy to Close"
    assert _pad_occ("SPXW  251104C07000000") == "SPXW  251104C07000000"
    assert _pad_occ("SPXW251104C07000000") == "SPXW  251104C07000000"

    class _Candle:
        def __init__(self, moment):
            self.time = int(moment.timestamp() * 1000)
            self.open = self.high = self.low = self.close = 10
            self.volume = 1

    regular = _Candle(datetime(2026, 10, 28, 14, 30, tzinfo=timezone.utc))
    early = _Candle(datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc))
    bars = candles_to_bars([regular, early])
    assert len(bars) == 1
    assert bars[0].t.hour == 10 and bars[0].t.minute == 30


def test_client_host_beats_a_spoofed_label():
    session = Sess(PROD_HOST, is_test=True, client_host=CERT_HOST, label=PROD_HOST)
    assert session_host(session) == CERT_HOST
    assert session.host == PROD_HOST


def test_missing_prod_creds_do_not_fall_back(monkeypatch, tmp_path):
    for name in (
        "TASTYTRADE_PROD_CLIENT_SECRET", "TASTYTRADE_PROD_REFRESH_TOKEN",
        "TASTYTRADE_SANDBOX_CLIENT_SECRET", "TASTYTRADE_SANDBOX_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TASTYTRADE_SANDBOX_CLIENT_SECRET", "sb-secret-value")
    monkeypatch.setenv("TASTYTRADE_SANDBOX_REFRESH_TOKEN", "sb-refresh-value")
    monkeypatch.setenv("TASTYTRADE_CRED_DIR", str(tmp_path / "empty-secrets"))
    opened = []

    def factory(secret, refresh, is_test):
        opened.append(is_test)
        return Sess(CERT_HOST if is_test else PROD_HOST, is_test=is_test)

    cfg = Config(broker="tasty", mode="dry", base_dir=str(tmp_path))
    broker = TastyBroker(cfg, session_factory=factory)
    with pytest.raises(SafetyError, match="production"):
        broker.connect()
    assert opened == []


def test_sandbox_orders_load_sandbox_creds_only(monkeypatch, tmp_path):
    for name in (
        "TASTYTRADE_PROD_CLIENT_SECRET", "TASTYTRADE_PROD_REFRESH_TOKEN",
        "TASTYTRADE_SANDBOX_CLIENT_SECRET", "TASTYTRADE_SANDBOX_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "secrets"
    folder.mkdir()
    (folder / ".prod_secret").write_text("file-prod-secret\n")
    (folder / ".prod_refresh").write_text("file-prod-refresh\n")
    (folder / ".sb_secret").write_text("file-sb-secret\n")
    (folder / ".sb_refresh").write_text("file-sb-refresh\n")
    monkeypatch.setenv("TASTYTRADE_CRED_DIR", str(folder))
    opened = []

    def factory(secret, refresh, is_test):
        opened.append((secret, refresh, is_test))
        host = CERT_HOST if is_test else PROD_HOST
        return Sess(host, is_test=is_test)

    cfg = Config(broker="tasty", mode="dry", base_dir=str(tmp_path / "home"))
    broker = TastyBroker(cfg, session_factory=factory)
    broker.connect()
    assert opened[0] == ("file-prod-secret", "file-prod-refresh", False)
    assert opened[1] == ("file-sb-secret", "file-sb-refresh", True)
    assert broker.data_session is not broker.order_session
    assert broker.safety_banner() == f"DATA={PROD_HOST} ORDERS={CERT_HOST} REAL MONEY=NO"
    text = json.dumps(opened)
    assert "file-prod-secret" not in broker.safety_banner()
    assert text  # creds stayed in the factory log, not the banner


def test_identical_creds_cannot_drive_sandbox_orders(monkeypatch, tmp_path):
    monkeypatch.setenv("TASTYTRADE_PROD_CLIENT_SECRET", "same-secret")
    monkeypatch.setenv("TASTYTRADE_PROD_REFRESH_TOKEN", "same-refresh")
    monkeypatch.setenv("TASTYTRADE_SANDBOX_CLIENT_SECRET", "same-secret")
    monkeypatch.setenv("TASTYTRADE_SANDBOX_REFRESH_TOKEN", "same-refresh")
    monkeypatch.setenv("TASTYTRADE_CRED_DIR", str(tmp_path / "none"))
    cfg = Config(broker="tasty", mode="dry", base_dir=str(tmp_path))
    broker = TastyBroker(cfg, session_factory=lambda *a, **k: Sess(PROD_HOST, False))
    with pytest.raises(SafetyError, match="sandbox orders"):
        broker.connect()


def test_live_refuses_without_a_cap_and_writes_the_order_log(tmp_path):
    transport = Transport()
    cfg = Config(broker="tasty", mode="live", base_dir=str(tmp_path))
    cfg.ensure_dirs()
    data_s, order_s = _pair()
    broker = TastyBroker(cfg, data_session=data_s, order_session=order_s, transport=transport, clock=Clock())
    broker.connect()
    with pytest.raises((GuardError, SafetyError)):
        broker.place_combo(ROOT_LEGS, "BUY", 1, 12.10, "5WT12345", REF)
    assert transport.placed == []
    broker.approve_ticket("N120000", 12.10)
    with pytest.raises(GuardError, match="cap"):
        broker.place_combo(ROOT_LEGS, "BUY", 1, 12.30, "5WT12345", REF)
    order_id = broker.place_combo(ROOT_LEGS, "BUY", 1, 12.10, "5WT12345", REF)
    assert order_id == "1001"
    assert transport.sessions == [broker.order_session]
    assert transport.sessions[0] is not broker.data_session
    assert transport.placed[0]["price-effect"] == "Debit"
    saved = json.loads(broker.store_path.read_text())
    assert broker.store_path.name == f"tt_orders-{broker.today.isoformat()}.json"
    assert saved["orders"][order_id]["host"] == CERT_HOST
    assert saved["orders"][order_id]["real_money"] is False
    blob = broker.store_path.read_text()
    assert "secret" not in blob.lower()
    assert "refresh" not in blob.lower()


def test_data_session_and_wrong_host_abort_before_the_order(tmp_path):
    broker, transport = _broker(tmp_path / "same")
    broker.order_session = broker.data_session
    with pytest.raises(SafetyError, match="data session"):
        broker.place_combo(ROOT_LEGS, "BUY", 1, 12.10, "5WT12345", REF)
    assert transport.placed == []

    broker, transport = _broker(tmp_path / "spoof")
    order_id = broker.place_combo(ROOT_LEGS, "BUY", 1, 12.10, "5WT12345", REF)
    broker.order_session = Sess(CERT_HOST, is_test=True, client_host=PROD_HOST, label=CERT_HOST)
    with pytest.raises(SafetyError, match="api.tastyworks.com"):
        broker.replace_combo(order_id, 12.05)
    assert transport.replaced == []

    prod, transport = _broker(tmp_path / "prod", data="production", orders="production")
    assert prod.safety_banner() == f"DATA={PROD_HOST} ORDERS={PROD_HOST} REAL MONEY=YES"
    order_id = prod.place_combo(ROOT_LEGS, "BUY", 1, 12.10, "5WT12345", REF)
    prod.order_session = Sess(PROD_HOST, is_test=False, client_host=CERT_HOST, label=PROD_HOST)
    with pytest.raises(SafetyError, match="production order"):
        prod.cancel(order_id)
    prod.order_session = prod.data_session
    with pytest.raises(SafetyError, match="data session"):
        prod.cancel(order_id)
    assert transport.cancelled == []


def test_rate_limit_counts_place_replace_and_cancel(tmp_path):
    broker, transport = _broker(tmp_path)
    first = broker.place_combo(ROOT_LEGS, "BUY", 1, 12.10, "5WT12345", REF)
    for i in range(8):
        broker.place_combo(ROOT_LEGS, "BUY", 1, 12.10, "5WT12345", f"SPXNOON-20261028-{i}")
    broker.clock.t += 16
    broker.replace_combo(first, 12.05)
    with pytest.raises(GuardError, match="rate"):
        broker.place_combo(ROOT_LEGS, "BUY", 1, 12.10, "5WT12345", "SPXNOON-20261028-over")
    assert len(transport.placed) == 9
    assert len(transport.replaced) == 1
    with pytest.raises(GuardError, match="rate"):
        broker.cancel(first)
    assert transport.cancelled == []


def test_dry_and_modify_do_not_send(tmp_path):
    broker, transport = _broker(tmp_path, mode="dry")
    with pytest.raises(SafetyError):
        broker.place_combo(ROOT_LEGS, "BUY", 1, 12.10, "5WT12345", REF)
    with pytest.raises(SafetyError, match="chase"):
        broker.modify_combo("1", 12.0)
    assert transport.placed == []


def test_tasty_live_auto_approves_without_a_prompt(tmp_path):
    from zoneinfo import ZoneInfo

    from spx_noon.broker import SimBroker
    from spx_noon.logbook import Outputs
    from spx_noon.notify import Notifier
    from spx_noon.runner import _State, _maybe_enter

    et = ZoneInfo("America/New_York")

    cfg = Config(
        broker="tasty", mode="live", base_dir=str(tmp_path),
        decision_grace_s=90, loop_interval_s=60,
    )
    cfg.ensure_dirs()
    noon = datetime(2026, 10, 28, 12, 0, tzinfo=et)
    broker = SimBroker(noon, levels()[0] + today_call_bars(), _quotes("target"), spot=7000)
    state = _State()
    # _maybe_enter takes no approver: there is nothing a human could be asked
    _maybe_enter(
        cfg, broker, Notifier(tmp_path / "logs", toast=False),
        Outputs(cfg.state_dir), state, "5WT12345", [noon],
    )
    buys = [order for order in broker.placed if order["action"] == "BUY"]
    assert len(buys) == 1
    assert broker.approved
    assert max(broker.approved.values()) == pytest.approx(buys[0]["limit"])


def test_error_text_redacts_secrets(tmp_path):
    broker, _transport = _broker(tmp_path, mode="dry")
    broker._secrets = ["sb-secret-value", "sb-refresh-value"]
    text = broker._redacted(RuntimeError(
        'refresh_token=sb-refresh-value client_secret: "sb-secret-value" Bearer abcdefghij'
    ))
    assert "sb-secret-value" not in text
    assert "sb-refresh-value" not in text
    assert "abcdefghij" not in text
    assert "[redacted]" in text


def test_secret_filenames_are_gitignored():
    text = (__import__("pathlib").Path(__file__).resolve().parent.parent / ".gitignore").read_text()
    for name in ("secrets/", ".sb_secret", ".sb_refresh", ".prod_secret", ".prod_refresh"):
        assert name in text
