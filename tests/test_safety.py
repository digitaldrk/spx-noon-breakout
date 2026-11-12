"""Paper port, machine broker, the IBKR live latch, and the live approval gate."""
import subprocess
import sys
from pathlib import Path

import pytest

from spx_noon.config import (
    LIVE_ACCOUNT_ENV,
    LIVE_ALLOW_ENV,
    LIVE_PORTS,
    ORDER_PREFIX,
    Config,
    SafetyError,
    load_config,
    log_order,
    startup_banner,
)
from spx_noon.guard import GuardError, OrderGuard
from spx_noon.ib import check_accounts, paper_send_allowed, send_allowed
from spx_noon.schwab import SchwabBroker, build_vertical_order, execution_price
from spx_noon.schwabapi import select_account

ROOT = Path(__file__).resolve().parent.parent
LIVE_ACCOUNT = "U7654321"


def _latch(monkeypatch, account=LIVE_ACCOUNT):
    monkeypatch.setenv(LIVE_ALLOW_ENV, "1")
    monkeypatch.setenv(LIVE_ACCOUNT_ENV, account)


def _no_latch(monkeypatch):
    monkeypatch.delenv(LIVE_ALLOW_ENV, raising=False)
    monkeypatch.delenv(LIVE_ACCOUNT_ENV, raising=False)


def _live_cfg(port=7496, account=LIVE_ACCOUNT, **extra):
    return Config(broker="ibkr", mode="live", account=account, port=port, **extra)


def test_examples_are_dry_and_carry_no_secrets():
    for name in ("config.example.json", "config.windows.example.json"):
        text = (ROOT / name).read_text().lower()
        assert "refresh_token" not in text
        assert "app_secret" not in text
        assert "private_key" not in text


def test_mac_example_is_paper_port_and_ignores_a_broker_key(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert "dry_run" not in (ROOT / "config.example.json").read_text()
    cfg = load_config(ROOT / "config.example.json")
    assert cfg.broker == "ibkr" and cfg.mode == "dry" and cfg.dry_run is True
    assert cfg.port == 7497 and cfg.client_id == 41 and not cfg.live and not cfg.paper
    flipped = tmp_path / "config.json"
    flipped.write_text('{"broker": "schwab", "mode": "dry", "port": 7497}')
    assert load_config(flipped).broker == "ibkr"


def test_windows_example_stays_tasty_and_does_not_send(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    text = (ROOT / "config.windows.example.json").read_text()
    assert "dry_run" not in text
    assert "tasty_allow_production_orders" not in text
    cfg = load_config(ROOT / "config.windows.example.json")
    assert cfg.broker == "tasty" and cfg.mode == "dry" and cfg.dry_run is True
    assert cfg.tasty_data == "production" and cfg.tasty_orders == "sandbox"
    assert cfg.live is False and cfg.real_money is False and cfg.confirms_orders is False
    assert cfg.sends_orders() is False


def test_mode_sets_dry_run_and_a_file_key_cannot_override(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    paper = load_config(_write(tmp_path, "paper.json", '{"mode": "paper", "port": 7497}'))
    assert paper.mode == "paper" and paper.dry_run is False and paper.sends_orders()
    alerts = load_config(_write(tmp_path, "alerts.json", '{"mode": "alerts", "port": 7497}'))
    assert alerts.mode == "alerts" and alerts.dry_run is True and not alerts.sends_orders()
    held = load_config(_write(tmp_path, "held.json", '{"mode": "paper", "dry_run": true, "port": 7497}'))
    assert held.mode == "paper" and held.dry_run is False and held.sends_orders()
    blocked = load_config(_write(tmp_path, "blocked.json", '{"mode": "dry", "dry_run": false, "port": 7497}'))
    assert blocked.mode == "dry" and blocked.dry_run is True and not blocked.sends_orders()
    leftover = load_config(_write(tmp_path, "leftover.json", '{"dry_run": false, "port": 7497}'))
    assert leftover.mode == "dry" and leftover.dry_run is True and not leftover.sends_orders()

    monkeypatch.setattr(sys, "platform", "win32")
    live = load_config(_write(tmp_path, "live.json", '{"mode": "live", "dry_run": true, "account": "705"}'))
    assert live.mode == "live" and live.dry_run is False and live.sends_orders()
    quiet = load_config(_write(tmp_path, "quiet.json", '{"mode": "alerts", "dry_run": false, "account": "705"}'))
    assert quiet.mode == "alerts" and quiet.dry_run is True and not quiet.sends_orders()


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


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
    assert Config().broker == "tasty" and Config().mode == "dry" and not Config().live
    live = Config(mode="live")
    assert live.broker == "tasty" and live.live and live.sends_orders()
    assert live.confirms_orders is False and live.real_money is False
    with pytest.raises(SafetyError):
        Config(broker="schwab", mode="live", account="").validate()
    with pytest.raises(SafetyError):
        Config(mode="paper").validate()


def test_ibkr_live_refuses_unless_the_env_latch_is_exact(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    _no_latch(monkeypatch)
    with pytest.raises(SafetyError, match=LIVE_ALLOW_ENV):
        _live_cfg().validate()
    monkeypatch.setenv(LIVE_ALLOW_ENV, "1")
    with pytest.raises(SafetyError, match=LIVE_ACCOUNT_ENV):
        _live_cfg().validate()
    monkeypatch.setenv(LIVE_ACCOUNT_ENV, "U0000000")
    with pytest.raises(SafetyError, match=LIVE_ACCOUNT_ENV):
        _live_cfg().validate()
    # no strip and no casefold on either variable
    for allow in ("true", "yes", "1 ", " 1", "01", ""):
        monkeypatch.setenv(LIVE_ALLOW_ENV, allow)
        monkeypatch.setenv(LIVE_ACCOUNT_ENV, LIVE_ACCOUNT)
        with pytest.raises(SafetyError, match=LIVE_ALLOW_ENV):
            _live_cfg().validate()
    monkeypatch.setenv(LIVE_ALLOW_ENV, "1")
    for account_env in (LIVE_ACCOUNT + " ", " " + LIVE_ACCOUNT, LIVE_ACCOUNT.lower()):
        monkeypatch.setenv(LIVE_ACCOUNT_ENV, account_env)
        with pytest.raises(SafetyError, match=LIVE_ACCOUNT_ENV):
            _live_cfg().validate()
    # a blank account, and a paper account even when the latch names that same id
    _latch(monkeypatch, "")
    with pytest.raises(SafetyError, match="account"):
        _live_cfg(account="").validate()
    _latch(monkeypatch, "DU123456")
    with pytest.raises(SafetyError, match="paper account"):
        _live_cfg(account="DU123456").validate()
    # the file alone never enables real money
    _no_latch(monkeypatch)
    path = _write(tmp_path, "live.json",
                  '{"mode": "live", "port": 7496, "account": "%s"}' % LIVE_ACCOUNT)
    with pytest.raises(SafetyError):
        load_config(path)
    _latch(monkeypatch)
    cfg = load_config(path)
    assert cfg.broker == "ibkr" and cfg.mode == "live" and cfg.ibkr_live is True


def test_ibkr_live_takes_only_the_two_live_ports_on_localhost(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)
    assert LIVE_PORTS == (7496, 4001)
    for port in LIVE_PORTS:
        cfg = _live_cfg(port=port)
        cfg.validate()
        assert cfg.ibkr_live is True
    for port in (7497, 4002, 0, 7495):
        with pytest.raises(SafetyError, match="live port"):
            _live_cfg(port=port).validate()
    for host in ("1.2.3.4", "tws.example.com", ""):
        with pytest.raises(SafetyError, match="localhost"):
            _live_cfg(host=host).validate()


def test_latched_ibkr_live_sends_but_is_not_the_schwab_live_path(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)
    for port in LIVE_PORTS:
        cfg = _live_cfg(port=port)
        cfg.validate()
        assert cfg.dry_run is False
        assert cfg.sends_orders() is True
        assert cfg.ibkr_live is True
        assert cfg.live is False          # no Schwab/tasty preview and no "yes" wait
        assert cfg.confirms_orders is False
        assert cfg.real_money is False    # that flag stays tastytrade's
        assert cfg.paper is False


def test_ibkr_live_flags_go_dark_when_the_latch_is_dropped(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)
    cfg = _live_cfg()
    cfg.validate()
    assert cfg.ibkr_live and cfg.sends_orders()
    _no_latch(monkeypatch)
    assert cfg.ibkr_live is False
    assert cfg.sends_orders() is False
    assert cfg.real_money_flag == "NO"


def test_paper_stays_on_7497_and_keeps_its_d_account(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)  # the latch must not loosen paper
    Config(broker="ibkr", mode="paper", port=7497).validate()
    for port in (7496, 4001):
        with pytest.raises(SafetyError, match="paper port"):
            Config(broker="ibkr", mode="paper", port=port).validate()
    blank = Config(broker="ibkr", mode="paper", account="")
    blank.validate()
    assert blank.paper and not blank.ibkr_live
    explicit = Config(broker="ibkr", mode="paper", account="U7654321")
    explicit.validate()  # validate stays quiet; the account gate is send/verify time
    with pytest.raises(SafetyError):
        paper_send_allowed(explicit, "U7654321", "SPXNOON-20261112-1")
    with pytest.raises(SafetyError):
        check_accounts(explicit, ["U7654321"])
    assert check_accounts(blank, ["DU999001"]) == "DU999001"


def test_dry_and_alerts_keep_the_paper_port_and_never_send(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)
    for mode in ("dry", "alerts"):
        cfg = Config(broker="ibkr", mode=mode, account=LIVE_ACCOUNT, dry_run=False)
        cfg.validate()
        assert cfg.port == 7497 and cfg.dry_run is True
        assert cfg.sends_orders() is False
        assert cfg.ibkr_live is False and cfg.real_money_flag == "NO"
        with pytest.raises(SafetyError):
            send_allowed(cfg, LIVE_ACCOUNT, "SPXNOON-20261112-1")
        with pytest.raises(SafetyError, match="paper port"):
            Config(broker="ibkr", mode=mode, port=7496).validate()


def test_banner_says_real_money_only_for_latched_ibkr_live(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)
    live = _live_cfg()
    live.validate()
    assert startup_banner(live, LIVE_ACCOUNT) == \
        "BROKER=ibkr MODE=live ACCOUNT=4321 PORT=7496 REAL MONEY=YES"
    gateway = _live_cfg(port=4001)
    gateway.validate()
    assert startup_banner(gateway, LIVE_ACCOUNT).endswith("PORT=4001 REAL MONEY=YES")
    assert LIVE_ACCOUNT not in startup_banner(live, LIVE_ACCOUNT)
    paper = Config(broker="ibkr", mode="paper", account="DU999001")
    assert startup_banner(paper, "DU999001") == \
        "BROKER=ibkr MODE=paper ACCOUNT=9001 PORT=7497 REAL MONEY=NO"
    for mode in ("dry", "alerts"):
        cfg = Config(broker="ibkr", mode=mode, account="DU999001")
        assert startup_banner(cfg, "DU999001").endswith("REAL MONEY=NO")
    _no_latch(monkeypatch)
    assert startup_banner(live, LIVE_ACCOUNT).endswith("REAL MONEY=NO")


def test_ibkr_order_log_lines_carry_the_flag_and_other_brokers_do_not(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)
    live = _live_cfg()
    live.validate()
    with caplog.at_level("INFO"):
        log_order(live, 20, "working entry %s", "SPXNOON-20261112-1")
        log_order(Config(broker="ibkr", mode="paper"), 20, "working entry %s", "ref")
        log_order(Config(broker="tasty", mode="live", account="705"), 20, "tasty entry %s", "ref")
    messages = [record.getMessage() for record in caplog.records]
    assert messages[0] == "working entry SPXNOON-20261112-1 REAL MONEY=YES"
    assert messages[1] == "working entry ref REAL MONEY=NO"
    assert messages[2] == "tasty entry ref"


def test_account_verifier_needs_no_socket(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)
    live = _live_cfg()
    live.validate()
    assert check_accounts(live, [LIVE_ACCOUNT]) == LIVE_ACCOUNT
    assert check_accounts(live, ["U1111111", LIVE_ACCOUNT]) == LIVE_ACCOUNT
    with pytest.raises(SafetyError, match="no account"):
        check_accounts(live, [])
    with pytest.raises(SafetyError, match="TWS reports"):
        check_accounts(live, ["U1111111"])
    with pytest.raises(SafetyError, match="paper account"):
        check_accounts(live, [LIVE_ACCOUNT, "DU999001"])
    # a D account in live mode is refused even when the latch names it
    _latch(monkeypatch, "DU999001")
    with pytest.raises(SafetyError):
        check_accounts(_live_cfg(account="DU999001"), ["DU999001"])
    # losing the latch mid-session is a mismatch, not a reconnect
    _no_latch(monkeypatch)
    with pytest.raises(SafetyError, match="not latched"):
        check_accounts(live, [LIVE_ACCOUNT])
    paper = Config(broker="ibkr", mode="paper", account="DU999001")
    assert check_accounts(paper, ["DU999001"]) == "DU999001"
    with pytest.raises(SafetyError, match="non-paper") as err:
        check_accounts(paper, ["DU999001", "U1111111"])
    assert "U1111111" not in str(err.value) and "1111" in str(err.value)
    with pytest.raises(SafetyError, match="non-paper"):
        check_accounts(Config(broker="ibkr", mode="dry"), ["U1111111"])


def test_live_send_gate(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)
    live = _live_cfg()
    live.validate()
    send_allowed(live, LIVE_ACCOUNT, "SPXNOON-20261112-1")
    with pytest.raises(SafetyError, match="orderRef"):
        send_allowed(live, LIVE_ACCOUNT, "BRK-1")
    with pytest.raises(SafetyError, match="paper"):
        send_allowed(live, "DU999001", "SPXNOON-20261112-1")
    with pytest.raises(SafetyError, match="configured live account"):
        send_allowed(live, "U1111111", "SPXNOON-20261112-1")
    for port in (7497, 4002):
        off_port = _live_cfg(port=port)
        with pytest.raises(SafetyError):
            send_allowed(off_port, LIVE_ACCOUNT, "SPXNOON-20261112-1")
    off_host = _live_cfg(host="1.2.3.4")
    with pytest.raises(SafetyError, match="localhost"):
        send_allowed(off_host, LIVE_ACCOUNT, "SPXNOON-20261112-1")
    _no_latch(monkeypatch)
    with pytest.raises(SafetyError, match="not latched"):
        send_allowed(live, LIVE_ACCOUNT, "SPXNOON-20261112-1")


class _FakeIB:
    """Enough of ib_async.IB to exercise connect/verify without a socket."""

    def __init__(self, accounts, *, connected=False):
        self.accounts = list(accounts)
        self.connected = connected
        self.calls = []
        self.disconnects = 0

    def connect(self, host, port, **kwargs):
        self.calls.append((host, port, kwargs))
        self.connected = True

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.disconnects += 1
        self.connected = False

    def managedAccounts(self):
        return list(self.accounts)

    def reqMarketDataType(self, _kind):
        return None


def _broker(cfg, accounts):
    """IBBroker without importing ib_async or opening anything."""
    from spx_noon.ib import IBBroker
    broker = IBBroker.__new__(IBBroker)
    broker.cfg = cfg
    broker.ib = _FakeIB(accounts)
    broker._opt_tickers = {}
    broker._opt_contracts = {}
    broker._trades = {}
    broker.spx = None
    broker.verified_account = None
    broker._streams_started = False
    broker.start_streams = lambda: None
    return broker


def test_connect_dials_the_validated_port_and_verifies_before_trading(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)
    for port in LIVE_PORTS:
        cfg = _live_cfg(port=port)
        cfg.validate()
        broker = _broker(cfg, [LIVE_ACCOUNT])
        broker.connect()
        host, dialed, kwargs = broker.ib.calls[0]
        assert host == "127.0.0.1" and dialed == port
        assert kwargs["readonly"] is False
        assert broker.verified_account == LIVE_ACCOUNT
    dry = Config(broker="ibkr", mode="dry")
    dry.validate()
    broker = _broker(dry, ["DU999001"])
    broker.connect()
    assert broker.ib.calls[0][1] == 7497
    assert broker.ib.calls[0][2]["readonly"] is True


def test_connect_closes_the_socket_on_an_account_mismatch(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)
    cfg = _live_cfg()
    cfg.validate()
    for reported in (["U1111111"], [LIVE_ACCOUNT, "DU999001"], []):
        broker = _broker(cfg, reported)
        with pytest.raises(SafetyError):
            broker.connect()
        assert broker.ib.disconnects == 1
        assert broker.ib.connected is False
        assert broker.verified_account is None
    paper = Config(broker="ibkr", mode="paper")
    broker = _broker(paper, ["U1111111"])
    with pytest.raises(SafetyError, match="non-paper"):
        broker.connect()
    assert broker.ib.disconnects == 1


def test_reconnect_reverifies_and_a_mismatch_is_not_a_dropped_connection(monkeypatch):
    from spx_noon.runner import _connect
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)
    cfg = _live_cfg(connect_retries=6)
    cfg.validate()
    broker = _broker(cfg, [LIVE_ACCOUNT])
    broker.connect()
    broker.ib.connected = False
    broker.ib.accounts = [LIVE_ACCOUNT, "DU999001"]
    with pytest.raises(SafetyError, match="paper account"):
        broker.ensure_connected()
    # and the runner stops at the first SafetyError instead of burning six retries
    fresh = _broker(cfg, ["U1111111"])
    slept = []
    fresh.sleep = slept.append
    with pytest.raises(SafetyError):
        _connect(cfg, fresh)
    assert len(fresh.ib.calls) == 1 and slept == []
    # a plain network error still retries
    class _Flaky(_FakeIB):
        def connect(self, host, port, **kwargs):
            self.calls.append((host, port, kwargs))
            if len(self.calls) < 3:
                raise OSError("connection refused")
            self.connected = True

    retried = _broker(cfg, [LIVE_ACCOUNT])
    retried.ib = _Flaky([LIVE_ACCOUNT])
    retried.sleep = lambda _s: None
    _connect(cfg, retried)
    assert len(retried.ib.calls) == 3


def test_an_account_id_never_reaches_a_log_line(monkeypatch, tmp_path):
    import logging

    from spx_noon.config import redact_account
    monkeypatch.setattr(sys, "platform", "darwin")
    _latch(monkeypatch)
    cfg = _live_cfg()
    cfg.validate()
    path = tmp_path / "bot.log"
    handler = logging.FileHandler(path)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        redact_account(cfg.account)
        # ib_async logs the whole order, account included, at INFO on its own logger
        logging.getLogger("ib_async.ib").warning(
            "placeOrder: New order Trade(order=LimitOrder(account=%r))", LIVE_ACCOUNT)
        log_order(cfg, logging.WARNING, "placed BUY acct %s", LIVE_ACCOUNT)
        handler.flush()
    finally:
        root.removeHandler(handler)
        for flt in list(handler.filters):
            handler.removeFilter(flt)
        handler.close()
    text = path.read_text()
    assert LIVE_ACCOUNT not in text
    assert "...4321" in text
    assert "REAL MONEY=YES" in text


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


def test_schwab_fill_comes_from_executions_not_the_limit():
    order = {
        "price": "12.10",
        "filledQuantity": 1,
        "orderLegCollection": [
            {"legId": 1, "instruction": "BUY_TO_OPEN"},
            {"legId": 2, "instruction": "SELL_TO_OPEN"},
        ],
        "orderActivityCollection": [{
            "executionLegs": [
                {"legId": 1, "price": 40.0, "quantity": 1},
                {"legId": 2, "price": 28.2, "quantity": 1},
            ],
        }],
    }
    assert execution_price(order, "BUY") == 11.8


def test_two_schwab_accounts_with_the_same_suffix_are_refused():
    accounts = [
        {"accountNumber": "111705", "hashValue": "aaa"},
        {"accountNumber": "222705", "hashValue": "bbb"},
    ]
    with pytest.raises(RuntimeError, match="exactly one"):
        select_account(accounts, "705")
    assert select_account(accounts[:1], "705")["hashValue"] == "aaa"


def test_guard_refuses_when_not_live():
    guard = OrderGuard(live=False)
    with pytest.raises(GuardError):
        guard.approve("N120000", 12.10)
    with pytest.raises(GuardError):
        guard.authorize("place", "N120000")


PAPER_LABELS = ("com.derek.spxnoon.plist", "com.derek.spxnoon.wrap.plist")
LIVE_LABELS = ("com.derek.spxnoon.live.plist", "com.derek.spxnoon.live.wrap.plist")


def _launchd(tmp_path, *args, account=None):
    """Run deploy/install_launchd.sh against a throwaway HOME with stub launchctl/plutil."""
    stub = tmp_path / "bin"
    stub.mkdir(parents=True)
    for name in ("launchctl", "plutil"):
        tool = stub / name
        tool.write_text("#!/bin/bash\nexit 0\n")
        tool.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(parents=True)
    env = {
        "HOME": str(home),
        "PATH": f"{stub}:/usr/bin:/bin",
        "SHELL": "/bin/bash",
    }
    if account is not None:
        env[LIVE_ACCOUNT_ENV] = account
    done = subprocess.run(
        ["/bin/bash", str(ROOT / "deploy" / "install_launchd.sh"), *args],
        env=env, capture_output=True, text=True,
    )
    agents = home / "Library" / "LaunchAgents"
    installed = sorted(path.name for path in agents.glob("*.plist")) if agents.exists() else []
    return done, installed, agents


def test_no_arg_install_only_touches_the_paper_jobs(tmp_path):
    done, installed, agents = _launchd(tmp_path)
    assert done.returncode == 0, done.stderr
    assert installed == sorted(PAPER_LABELS)
    for name in PAPER_LABELS:
        text = (agents / name).read_text()
        assert "spxnoon.live" not in text
        assert LIVE_ALLOW_ENV not in text
    assert "run.sh</string>" in (agents / PAPER_LABELS[0]).read_text()


def test_install_live_adds_new_labels_and_leaves_the_paper_plists_alone(tmp_path):
    done, installed, agents = _launchd(tmp_path, "live", account=LIVE_ACCOUNT)
    assert done.returncode == 0, done.stderr
    assert installed == sorted(LIVE_LABELS)
    session = (agents / "com.derek.spxnoon.live.plist").read_text()
    assert "<string>live</string>" in session and "run.sh" in session
    assert f"<key>{LIVE_ALLOW_ENV}</key><string>1</string>" in session
    assert LIVE_ACCOUNT in session and "__LIVE_ACCOUNT__" not in session
    wrap = (agents / "com.derek.spxnoon.live.wrap.plist").read_text()
    assert "run_wrap.sh" in wrap and "<string>live</string>" in wrap
    assert "did NOT disable the paper job" in done.stdout
    assert "Candle" in done.stdout and "Fly" in done.stdout


def test_install_live_refuses_a_missing_or_paper_account(tmp_path):
    done, installed, _agents = _launchd(tmp_path, "live")
    assert done.returncode == 2 and installed == []
    assert LIVE_ACCOUNT_ENV in done.stderr
    done, installed, _agents = _launchd(tmp_path / "b", "live", account="")
    assert done.returncode == 2 and installed == []
    done, installed, _agents = _launchd(tmp_path / "c", "live", account="DU999001")
    assert done.returncode == 2 and installed == []
    assert "paper account" in done.stderr
    done, installed, _agents = _launchd(tmp_path / "d", "bogus")
    assert done.returncode == 2 and installed == []


def test_uninstall_covers_both_pairs():
    text = (ROOT / "deploy" / "uninstall_launchd.sh").read_text()
    for label in ("com.derek.spxnoon", "com.derek.spxnoon.wrap",
                  "com.derek.spxnoon.live", "com.derek.spxnoon.live.wrap"):
        assert label in text


def test_live_templates_carry_no_real_account_id():
    for name in ("com.derek.spxnoon.live.plist.template",
                 "com.derek.spxnoon.live.wrap.plist.template"):
        text = (ROOT / "deploy" / name).read_text()
        assert "__LIVE_ACCOUNT__" in text
        assert "__HOME__" in text
        assert LIVE_ACCOUNT not in text


def _code_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_run_scripts_pass_the_mode_and_the_book_but_never_the_latch():
    run_sh = (ROOT / "run.sh").read_text()
    assert "paper|dry|alerts|live" in run_sh
    code = _code_lines(run_sh)
    assert LIVE_ALLOW_ENV not in code and LIVE_ACCOUNT_ENV not in code
    wrap_sh = (ROOT / "run_wrap.sh").read_text()
    assert "--book" in wrap_sh and "live|paper" in wrap_sh
    assert LIVE_ALLOW_ENV not in _code_lines(wrap_sh)


def test_package_does_not_import_candle_or_fly():
    blob = "\n".join(path.read_text() for path in (ROOT / "spx_noon").glob("*.py"))
    lowered = blob.lower()
    assert "spx_breakout" not in lowered
    assert "spx_fly" not in lowered
    strategy = (ROOT / "spx_noon" / "strategy.py").read_text().lower()
    assert "ema" not in strategy
