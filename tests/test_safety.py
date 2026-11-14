"""Paper port, machine broker, the IBKR live gate, and the live approval gate."""
import subprocess
import sys
from pathlib import Path

import pytest

from spx_noon.config import (
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
#: Retired. Nothing in the code reads these; setting them must change nothing.
RETIRED_ENV = ("SPX_NOON_ALLOW_LIVE", "SPX_NOON_LIVE_ACCOUNT")


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


def test_ibkr_live_needs_a_set_non_paper_account_in_the_config(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    for account in ("", "   "):
        with pytest.raises(SafetyError, match="account"):
            _live_cfg(account=account).validate()
    for account in ("DU123456", "D"):
        with pytest.raises(SafetyError, match="paper account"):
            _live_cfg(account=account).validate()
    # the config file plus the TWS session is the whole switch
    path = _write(tmp_path, "live.json",
                  '{"mode": "live", "port": 7496, "account": "%s"}' % LIVE_ACCOUNT)
    cfg = load_config(path)
    assert cfg.broker == "ibkr" and cfg.mode == "live" and cfg.ibkr_live is True
    assert cfg.account == LIVE_ACCOUNT and cfg.dry_run is False


def test_the_retired_live_env_vars_neither_enable_nor_block_live(monkeypatch, tmp_path):
    """Nothing reads SPX_NOON_ALLOW_LIVE or SPX_NOON_LIVE_ACCOUNT any more."""
    monkeypatch.setattr(sys, "platform", "darwin")
    path = _write(tmp_path, "live.json",
                  '{"mode": "live", "port": 7496, "account": "%s"}' % LIVE_ACCOUNT)
    for values in ((), ("1", LIVE_ACCOUNT), ("0", "U0000000"), ("", "")):
        for name in RETIRED_ENV:
            monkeypatch.delenv(name, raising=False)
        for name, value in zip(RETIRED_ENV, values):
            monkeypatch.setenv(name, value)
        cfg = load_config(path)
        assert cfg.ibkr_live is True and cfg.sends_orders() is True
        assert cfg.real_money_flag == "YES"
        assert check_accounts(cfg, [LIVE_ACCOUNT]) == LIVE_ACCOUNT
        send_allowed(cfg, LIVE_ACCOUNT, "SPXNOON-20261112-1")
        # and they never loosen a config that is not live
        dry = Config(broker="ibkr", mode="dry", account=LIVE_ACCOUNT)
        dry.validate()
        assert dry.ibkr_live is False and dry.sends_orders() is False


def test_the_live_env_names_appear_nowhere_in_the_code():
    paths = [ROOT / "run.sh", ROOT / "run_wrap.sh", *(ROOT / "spx_noon").glob("*.py"),
             *(ROOT / "deploy").glob("*"), ROOT / "config.example.json"]
    blob = "\n".join(path.read_text() for path in paths if path.is_file())
    for name in RETIRED_ENV:
        assert name not in blob


def test_ibkr_live_takes_only_the_two_live_ports_on_localhost(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
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


def test_ibkr_live_sends_but_is_not_the_schwab_live_path(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
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


def test_paper_stays_on_7497_and_keeps_its_d_account(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
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


def test_banner_says_real_money_only_for_ibkr_live(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
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
    tasty = Config(broker="tasty", mode="live", account="705")
    assert startup_banner(tasty, "705").endswith("REAL MONEY=NO")


def test_ibkr_order_log_lines_carry_the_flag_and_other_brokers_do_not(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "darwin")
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
    # a D account in live mode is refused even though validate() would have caught it
    with pytest.raises(SafetyError, match="paper"):
        check_accounts(_live_cfg(account="DU999001"), ["DU999001"])
    with pytest.raises(SafetyError, match="account"):
        check_accounts(_live_cfg(account=""), [LIVE_ACCOUNT])
    paper = Config(broker="ibkr", mode="paper", account="DU999001")
    assert check_accounts(paper, ["DU999001"]) == "DU999001"
    with pytest.raises(SafetyError, match="non-paper") as err:
        check_accounts(paper, ["DU999001", "U1111111"])
    assert "U1111111" not in str(err.value) and "1111" in str(err.value)
    with pytest.raises(SafetyError, match="non-paper"):
        check_accounts(Config(broker="ibkr", mode="dry"), ["U1111111"])


def test_live_send_gate(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
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
    blank = _live_cfg(account="")
    with pytest.raises(SafetyError, match="account"):
        send_allowed(blank, "", "SPXNOON-20261112-1")
    # a live config that was turned dry after construction cannot send
    dried = _live_cfg()
    dried.dry_run = True
    with pytest.raises(SafetyError, match="not IBKR live"):
        send_allowed(dried, LIVE_ACCOUNT, "SPXNOON-20261112-1")


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


INSTALLED_LABELS = ("com.derek.spxnoon.plist", "com.derek.spxnoon.wrap.plist")
RETIRED_LABELS = ("com.derek.spxnoon.live", "com.derek.spxnoon.live.wrap")


def _launchd(tmp_path, *args, script="install_launchd.sh", already=()):
    """Run a deploy script against a throwaway HOME with stub launchctl/plutil.

    ``already`` pre-seeds plist names, standing in for an older install.
    """
    stub = tmp_path / "bin"
    stub.mkdir(parents=True)
    booted = stub / "booted.txt"
    for name in ("launchctl", "plutil"):
        tool = stub / name
        tool.write_text("#!/bin/bash\nexit 0\n")
        tool.chmod(0o755)
    # record what launchctl was asked to bootout so removal is checked, not assumed
    (stub / "launchctl").write_text(
        f'#!/bin/bash\nif [ "${{1:-}}" = "bootout" ]; then echo "$3" >> {booted}; fi\nexit 0\n')
    (stub / "launchctl").chmod(0o755)
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for name in already:
        (agents / name).write_text("<plist/>\n")
    env = {
        "HOME": str(home),
        "PATH": f"{stub}:/usr/bin:/bin",
        "SHELL": "/bin/bash",
    }
    done = subprocess.run(
        ["/bin/bash", str(ROOT / "deploy" / script), *args],
        env=env, capture_output=True, text=True,
    )
    installed = sorted(path.name for path in agents.glob("*.plist"))
    done.booted_out = booted.read_text().splitlines() if booted.exists() else []
    return done, installed, agents


def test_no_arg_install_is_the_only_install_and_it_is_the_one_pair(tmp_path):
    done, installed, agents = _launchd(tmp_path)
    assert done.returncode == 0, done.stderr
    assert installed == sorted(INSTALLED_LABELS)
    session = (agents / "com.derek.spxnoon.plist").read_text()
    wrap = (agents / "com.derek.spxnoon.wrap.plist").read_text()
    for text in (session, wrap):
        assert "spxnoon.live" not in text
        assert "__HOME__" not in text
        for name in RETIRED_ENV:
            assert name not in text
    # the 11:45 job runs run.sh with no mode and the 16:20 job run_wrap.sh with no book
    assert "run.sh</string>" in session and "<string>live</string>" not in session
    assert "run_wrap.sh</string>" in wrap and "<string>live</string>" not in wrap


def test_install_rejects_live_and_any_other_argument(tmp_path):
    for index, args in enumerate((("live",), ("bogus",), ("", ), ("live", "wrap"))):
        done, installed, _agents = _launchd(tmp_path / f"case{index}", *args)
        assert done.returncode == 2, done.stdout
        assert installed == []
        assert "usage: install_launchd.sh" in done.stderr


def test_install_boots_out_a_live_pair_an_older_install_left_behind(tmp_path):
    """Two 11:45 jobs on one client id is the thing this consolidation removes."""
    stale = [f"{label}.plist" for label in RETIRED_LABELS]
    done, installed, agents = _launchd(tmp_path, already=stale)
    assert done.returncode == 0, done.stderr
    assert installed == sorted(INSTALLED_LABELS)
    for name in stale:
        assert not (agents / name).exists()
        assert str(agents / name) in done.booted_out
        assert f"removed retired {agents / name}" in done.stdout


def test_install_rejecting_an_argument_leaves_an_older_install_alone(tmp_path):
    """The usage exit happens before anything is booted out or written."""
    stale = [f"{label}.plist" for label in RETIRED_LABELS]
    done, installed, _agents = _launchd(tmp_path, "live", already=stale)
    assert done.returncode == 2
    assert installed == sorted(stale)
    assert done.booted_out == []


def test_uninstall_removes_both_pairs_and_skips_what_is_not_there(tmp_path):
    present = ["com.derek.spxnoon.plist", f"{RETIRED_LABELS[0]}.plist"]
    done, installed, agents = _launchd(tmp_path, script="uninstall_launchd.sh", already=present)
    assert done.returncode == 0, done.stderr
    assert installed == []
    assert sorted(done.booted_out) == sorted(str(agents / name) for name in present)
    assert "com.derek.spxnoon.wrap.plist" not in done.stdout


def test_no_live_plist_templates_remain():
    names = sorted(path.name for path in (ROOT / "deploy").glob("*.template"))
    assert names == ["com.derek.spxnoon.plist.template", "com.derek.spxnoon.wrap.plist.template"]
    blob = "\n".join(path.read_text() for path in (ROOT / "deploy").glob("*"))
    assert "__LIVE_ACCOUNT__" not in blob
    assert LIVE_ACCOUNT not in blob


def test_uninstall_still_cleans_up_the_retired_live_labels():
    text = (ROOT / "deploy" / "uninstall_launchd.sh").read_text()
    for label in ("com.derek.spxnoon", "com.derek.spxnoon.wrap", *RETIRED_LABELS):
        assert label in text


def _code_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _launcher(tmp_path, script, *args):
    """Run run.sh / run_wrap.sh with a stub interpreter that echoes its arguments."""
    base = tmp_path / "base"
    (base / ".venv" / "bin").mkdir(parents=True)
    python = base / ".venv" / "bin" / "python"
    python.write_text('#!/bin/bash\necho "ARGS: $*"\n')
    python.chmod(0o755)
    done = subprocess.run(
        ["/bin/bash", str(ROOT / script), *args],
        env={"HOME": str(tmp_path), "SPX_NOON_HOME": str(base),
             "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash"},
        capture_output=True, text=True,
    )
    return done, base


@pytest.mark.skipif(not Path("/usr/bin/caffeinate").exists(), reason="macOS launcher")
def test_the_wrap_launcher_forwards_a_book_only_when_one_is_asked_for(tmp_path):
    """The 16:20 job runs this bare, so it must not force --book paper."""
    done, _base = _launcher(tmp_path / "bare", "run_wrap.sh")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ARGS: -m spx_noon wrap"
    for book in ("paper", "live"):
        done, _base = _launcher(tmp_path / book, "run_wrap.sh", book, "--now")
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == f"ARGS: -m spx_noon wrap --book {book} --now"


@pytest.mark.skipif(not Path("/usr/bin/caffeinate").exists(), reason="macOS launcher")
def test_the_session_launcher_forwards_a_mode_only_when_one_is_asked_for(tmp_path):
    """The 11:45 job runs this bare, so config.json mode is what runs."""
    import datetime as _dt
    if _dt.date.today().isoweekday() > 5:
        pytest.skip("run.sh does not launch on a weekend")

    def logged(done, base):
        assert done.returncode == 0, done.stderr
        log = next((base / "state").glob("run_*.log")).read_text()
        return [line for line in log.splitlines() if line.startswith("ARGS: ")]

    done, base = _launcher(tmp_path / "bare", "run.sh")
    assert logged(done, base) == [f"ARGS: -m spx_noon --config {base}/config.json"]
    for mode in ("paper", "dry", "alerts", "live"):
        done, base = _launcher(tmp_path / mode, "run.sh", mode)
        assert logged(done, base) == [
            f"ARGS: -m spx_noon --config {base}/config.json --mode {mode}"]


def test_run_scripts_override_the_mode_and_the_book_for_one_launch():
    run_sh = (ROOT / "run.sh").read_text()
    assert "paper|dry|alerts|live" in run_sh
    wrap_sh = (ROOT / "run_wrap.sh").read_text()
    assert "--book" in wrap_sh and "live|paper" in wrap_sh
    for code in (_code_lines(run_sh), _code_lines(wrap_sh)):
        for name in RETIRED_ENV:
            assert name not in code
        # neither script forces a mode or a book when launchd runs it bare
        assert "--mode live" not in code and "--book paper" not in code


def test_package_does_not_import_candle_or_fly():
    blob = "\n".join(path.read_text() for path in (ROOT / "spx_noon").glob("*.py"))
    lowered = blob.lower()
    assert "spx_breakout" not in lowered
    assert "spx_fly" not in lowered
    strategy = (ROOT / "spx_noon" / "strategy.py").read_text().lower()
    assert "ema" not in strategy
